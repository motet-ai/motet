"""
Motet - Web Search Tool

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Web search tool for the Motet distributed framework.
    Prefer the current LLM's native web search (OpenAI, Anthropic, Grok, DeepSeek, Muse Spark)
    when the model supports it and returns URL-bearing results; otherwise use the
    ``ddgs`` metasearch client for real SERP results. DuckDuckGo Instant
    Answers remains a last-resort fallback for entity lookups.

Dependencies:
    - json: Data serialization and processing
    - re: Regular expressions for content processing
    - httpx: HTTP client for Instant Answers fallback
    - ddgs: Metasearch client for general web results (optional import)
    - pydantic: Data validation and model definitions
    - typing: Type hints and annotations
    - motet.core.commands.decorator: get_motet_context (optional)
    - motet.core.models.registry: get_model_spec for built-in web_search check

Usage:
    from motet.core.tools.builtin.web_search import run_web_search

    result = run_web_search({
        "query": "latest AI news",
        "max_results": 5,
    })

    # Optional model identity for the LLM-native path when parent metadata
    # does not already carry model_provider / model_name:
    result = run_web_search({
        "query": "latest AI news",
        "max_results": 5,
        "provider": "openai",
        "model_name": "gpt-4o-mini",
    })

Notes:
    - Path order: LLM-native (URL-bearing) → ddgs text search → Instant Answers.
    - ``web_search_path`` is ``llm``, ``ddgs``, or ``duckduckgo_instant`` for
      observability.
    - ``MOTET_WEB_SEARCH_BACKEND`` can force ``ddgs`` or ``instant_answers`` to
      skip earlier backends (tests / constrained environments).
    - Moonshot uses pass-through only.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field

from ...workers.concurrency_primitives import WorkerLocal
from ..cache_control import attach_snapshot_cache_control
from ..protocol import err
from ..registry import ToolRegistry

logger = structlog.get_logger(__name__)


def _get_motet_context_optional() -> Any:
    """Return current MotetContext if available (when tool runs inside tool_execution)."""
    try:
        from motet.core.commands.decorator import get_motet_context

        return get_motet_context()
    except Exception:
        return None


def _mutable_motet_metadata(motet: Any) -> Dict[str, Any]:
    """
    Return a mutable metadata dict attached to the live MotetContext/command.

    ``motet.metadata`` uses ``or {}``, which yields a *new* empty dict when the
    stored metadata is ``{}`` (falsy). Prefer the command context reference.
    """
    cmd = getattr(motet, "_command", None)
    if cmd is not None:
        ctx = getattr(cmd, "distributed_context", None)
        if ctx is not None:
            meta = getattr(ctx, "metadata", None)
            if not isinstance(meta, dict):
                ctx.metadata = {}
                meta = ctx.metadata
            return meta
    fb = getattr(motet, "_metadata_fallback", None)
    if not isinstance(fb, dict):
        try:
            motet._metadata_fallback = {}
            fb = motet._metadata_fallback
        except Exception:
            return {}
    return fb


def _stamp_model_metadata(motet: Any, provider: str, model_name: str) -> None:
    """Stamp model identity onto motet metadata when missing (does not overwrite)."""
    if motet is None:
        return
    provider = (provider or "").strip()
    model_name = (model_name or "").strip()
    if not provider and not model_name:
        return
    meta = _mutable_motet_metadata(motet)
    if provider and not str(meta.get("model_provider") or "").strip():
        meta["model_provider"] = provider
    if model_name and not str(meta.get("model_name") or "").strip():
        meta["model_name"] = model_name


def _resolve_model_identity(
    motet: Any,
    params: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """Resolve provider/model from params overrides, then motet.metadata."""
    params = params or {}
    metadata: Dict[str, Any] = {}
    if motet is not None:
        raw = getattr(motet, "metadata", None)
        if isinstance(raw, dict):
            metadata = raw
        else:
            # Prefer live command metadata when property returned a throwaway {}
            metadata = _mutable_motet_metadata(motet)
    provider = (
        params.get("provider")
        or params.get("model_provider")
        or metadata.get("model_provider")
        or ""
    )
    model_name = params.get("model_name") or metadata.get("model_name") or ""
    return str(provider).strip(), str(model_name).strip()


def _current_model_supports_web_search(
    motet: Any,
    provider: str = "",
    model_name: str = "",
) -> bool:
    """Return True if the resolved model has native web search built-in."""
    try:
        from ...models.registry import get_model_spec

        if not provider or not model_name:
            provider, model_name = _resolve_model_identity(motet)
        if not provider or not model_name:
            return False
        spec = get_model_spec(provider, model_name)
        supported = getattr(spec, "supported_builtin_tools", None) if spec else None
        if not supported:
            return False
        return any("web_search" in (t or "") for t in supported)
    except Exception:
        return False


def _current_model_web_search_passthrough(
    motet: Any,
    provider: str = "",
    model_name: str = "",
) -> bool:
    """True when native web search is pass-through-only (e.g. Moonshot)."""
    if not provider or not model_name:
        provider, model_name = _resolve_model_identity(motet)
    if not _current_model_supports_web_search(motet, provider, model_name):
        return False
    return provider.strip().lower() == "moonshot"


def _llm_web_search_usable(
    motet: Any,
    provider: str = "",
    model_name: str = "",
) -> bool:
    """True when a single model_inference turn can run native web search."""
    if not provider or not model_name:
        provider, model_name = _resolve_model_identity(motet)
    if not _current_model_supports_web_search(motet, provider, model_name):
        return False
    if _current_model_web_search_passthrough(motet, provider, model_name):
        return False
    return True


_WEB_SEARCH_LLM_PROMPT = (
    "Search the web for: {query}. Use your web search capability and return the findings concisely. "
    "Include key facts and sources if available."
)


def _results_have_urls(results: List[Dict[str, Any]]) -> bool:
    """Return True if at least one result has a non-empty url."""
    return any(isinstance(r, dict) and str(r.get("url") or "").strip() for r in results)


def _normalize_success(
    query: str,
    results: List[Dict[str, Any]],
    *,
    path: str,
    main_content: str = "",
) -> Dict[str, Any]:
    """Build the canonical web_search success payload."""
    summary_parts = [str(r.get("content") or "")[:200] for r in results if r.get("content")]
    summary = (
        " | ".join(summary_parts)
        if summary_parts
        else (f"Found {len(results)} results for '{query}'" if results else f"No specific results found for '{query}'.")
    )
    if not main_content:
        full_parts = [str(r.get("content") or "") for r in results if r.get("content")]
        main_content = " | ".join(full_parts) if full_parts else summary
    return attach_snapshot_cache_control(
        "core.web_search",
        {
            "status": "success",
            "query": query,
            "results": results,
            "summary": summary,
            "total_results": len(results),
            "main_content": main_content,
            "data": results,
            "web_search_path": path,
        },
    )


def _try_llm_web_search(
    query: str,
    max_results: int,
    motet: Any,
    provider: str = "",
    model_name: str = "",
) -> Optional[Dict[str, Any]]:
    """
    Run a single model_inference turn so the current LLM uses native web search.

    Returns a normalized payload when URL-bearing results (or citation-backed
    content) are available; otherwise None so callers can fall through to ddgs.
    """
    try:
        from motet.core.commands.builtin.model import model_inference
        from motet.core.commands.command_data_classes import ModelInferenceData
        from motet.core.commands.response_models import CommandExecutionError

        from ...models.adapters.provider_builtin_tools import get_unified_web_search_schema
        from ...types import Message, RequestContext
    except Exception:
        return None

    if not provider or not model_name:
        provider, model_name = _resolve_model_identity(motet)
    if not provider or not model_name:
        return None

    user_content = _WEB_SEARCH_LLM_PROMPT.format(query=query)
    metadata = getattr(motet, "metadata", None) or {}
    request_context = RequestContext(
        tenant_id=getattr(motet, "tenant_id", None),
        principal_id=getattr(motet, "principal_id", None),
        motet_id=getattr(motet, "motet_id", None),
        task_id=getattr(motet, "task_id", None),
        model_profile_name=metadata.get("model_profile_name") if isinstance(metadata, dict) else None,
    )
    inference_data = ModelInferenceData(
        messages=[Message(role="user", content=user_content)],
        model_settings={
            "provider": provider,
            "model_name": model_name,
            "temperature": 0.2,
            "max_tokens": 2000,
        },
        request_context=request_context,
        tools=[get_unified_web_search_schema()],
    )

    try:
        result = motet.do(model_inference, data=inference_data)
    except CommandExecutionError:
        return None
    except Exception as e:
        logger.warning(
            "web_search_llm_path_failed",
            query=query,
            provider=provider,
            model_name=model_name,
            error=str(e),
            error_type=type(e).__name__,
        )
        return None

    if not isinstance(result, dict):
        return None
    content = (result.get("content") or "").strip()
    citations = result.get("citations") or []

    results: List[Dict[str, Any]] = []
    if citations:
        for c in citations[:max_results]:
            if not isinstance(c, dict):
                continue
            snippets = c.get("snippets")
            snippet = ""
            if isinstance(snippets, list) and snippets:
                snippet = str(snippets[0] or "")
            results.append(
                {
                    "type": "citation",
                    "title": c.get("title") or c.get("name") or "Source",
                    "content": c.get("snippet") or c.get("content") or c.get("text") or snippet,
                    "source": c.get("source") or "LLM web search",
                    "url": c.get("url") or c.get("link") or "",
                }
            )

    # Prefer URL-bearing citations for downstream fetch pipelines (e.g. deep-research).
    # Fall through to ddgs when the LLM only returned answer text without URLs.
    if not _results_have_urls(results):
        if content and not results:
            logger.info(
                "web_search_llm_path_no_urls",
                query=query,
                provider=provider,
                model_name=model_name,
                has_content=True,
            )
        return None

    return _normalize_success(query, results[:max_results], path="llm", main_content=content)


def _search_ddgs(query: str, max_results: int) -> Optional[Dict[str, Any]]:
    """Run ddgs text metasearch and normalize to the web_search shape."""
    try:
        from ddgs import DDGS  # type: ignore[import-not-found]
    except ImportError:
        logger.warning(
            "web_search_ddgs_unavailable",
            query=query,
            hint="Install the 'ddgs' package for general web search results",
        )
        return None

    try:
        backend = (os.getenv("MOTET_WEB_SEARCH_DDGS_BACKEND") or "auto").strip() or "auto"
        raw_hits = DDGS().text(query, max_results=max_results, backend=backend)
    except Exception as e:
        logger.warning(
            "web_search_ddgs_failed",
            query=query,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        return None

    results: List[Dict[str, Any]] = []
    for hit in raw_hits or []:
        if not isinstance(hit, dict):
            continue
        url = (hit.get("href") or hit.get("url") or hit.get("link") or "").strip()
        title = (hit.get("title") or hit.get("name") or "").strip()
        body = (hit.get("body") or hit.get("snippet") or hit.get("content") or "").strip()
        if not url:
            continue
        results.append(
            {
                "type": "search_result",
                "title": title or url,
                "content": body or title,
                "source": hit.get("source") or "ddgs",
                "url": url,
            }
        )
        if len(results) >= max_results:
            break

    if not results:
        return None
    return _normalize_success(query, results, path="ddgs")


# HTTP client configuration for Instant Answers fallback
HTTP_LIMITS = httpx.Limits(max_connections=10, max_keepalive_connections=5)
_http_client_local = WorkerLocal()

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def _get_http_client() -> httpx.Client:
    """Get or create a per-worker synchronous HTTP client."""
    if not hasattr(_http_client_local, "client") or _http_client_local.client is None:
        _http_client_local.client = httpx.Client(
            limits=HTTP_LIMITS,
            headers=BROWSER_HEADERS,
            follow_redirects=True,
            timeout=30.0,
        )
    elif _http_client_local.client.is_closed:
        try:
            _http_client_local.client.close()
        except Exception:
            pass
        _http_client_local.client = httpx.Client(
            limits=HTTP_LIMITS,
            headers=BROWSER_HEADERS,
            follow_redirects=True,
            timeout=30.0,
        )
    return _http_client_local.client


def _search_instant_answers(query: str, max_results: int) -> Optional[Dict[str, Any]]:
    """
    Last-resort DuckDuckGo Instant Answers API (entity/QA lookups).

    Not a general SERP — kept only after ddgs fails or is unavailable.
    """
    try:
        client = _get_http_client()
        encoded_query = quote_plus(query)
        ddg_url = (
            f"https://api.duckduckgo.com/?q={encoded_query}"
            f"&format=json&no_html=1&skip_disambig=1"
        )
        response = client.get(ddg_url)
        response.raise_for_status()
        data = response.json()

        results: List[Dict[str, Any]] = []
        if data.get("Abstract"):
            results.append(
                {
                    "type": "abstract",
                    "title": data.get("AbstractText", ""),
                    "content": data.get("Abstract", ""),
                    "source": data.get("AbstractSource", ""),
                    "url": data.get("AbstractURL", ""),
                }
            )
        if data.get("Answer"):
            results.append(
                {
                    "type": "answer",
                    "title": "Direct Answer",
                    "content": data.get("Answer", ""),
                    "source": data.get("AnswerType", ""),
                    "url": "",
                }
            )
        if data.get("Definition"):
            results.append(
                {
                    "type": "definition",
                    "title": "Definition",
                    "content": data.get("Definition", ""),
                    "source": data.get("DefinitionSource", ""),
                    "url": data.get("DefinitionURL", ""),
                }
            )
        for topic in data.get("RelatedTopics", [])[:3]:
            if isinstance(topic, dict) and topic.get("Text"):
                text = topic.get("Text", "")
                results.append(
                    {
                        "type": "related",
                        "title": text.split(" - ")[0] if " - " in text else "Related",
                        "content": text,
                        "source": "DuckDuckGo",
                        "url": topic.get("FirstURL", ""),
                    }
                )

        # Lightweight HTML scrape only when Instant Answers is empty.
        if not results:
            for search_term in (
                f"{query} site:wikipedia.org",
                f"{query} official website",
                f"{query} information",
            ):
                try:
                    search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(search_term)}"
                    search_response = client.get(search_url)
                    if search_response.status_code != 200:
                        continue
                    link_pattern = re.compile(
                        r'<a[^>]+href="(https?://[^"]+)"[^>]*>([^<]*)</a>',
                        re.IGNORECASE,
                    )
                    for m in link_pattern.finditer(search_response.text):
                        url, title = m.group(1), (m.group(2) or "").strip()
                        if "duckduckgo.com" in url or not title or len(title) < 3:
                            continue
                        results.append(
                            {
                                "type": "search_result",
                                "title": title[:200],
                                "content": title[:500],
                                "source": "DuckDuckGo",
                                "url": url,
                            }
                        )
                        if len(results) >= max_results:
                            break
                    if results:
                        break
                except Exception:
                    continue

        results = results[:max_results]
        if not results:
            return None
        return _normalize_success(query, results, path="duckduckgo_instant")
    except Exception as e:
        logger.warning(
            "web_search_instant_answers_failed",
            query=query,
            error=str(e),
            error_type=type(e).__name__,
        )
        return None


class WebSearchParams(BaseModel):
    """Schema for web search tool parameters."""

    model_config = ConfigDict(extra="allow")
    query: Optional[str] = Field(
        default=None,
        description="Search query to find information about",
    )
    max_results: int = Field(
        default=5,
        description="Maximum number of search results to return",
        ge=1,
        le=10,
    )
    provider: Optional[str] = Field(
        default=None,
        description="Optional LLM provider for native web_search when metadata lacks model_provider",
    )
    model_name: Optional[str] = Field(
        default=None,
        description="Optional LLM model name for native web_search when metadata lacks model_name",
    )
    model_provider: Optional[str] = Field(
        default=None,
        description="Alias for provider (model_provider)",
    )


def run_web_search(params: Dict[str, Any]) -> Dict[str, Any]:
    """Search the web: LLM-native (URL-bearing) → ddgs → Instant Answers."""
    # Moonshot $web_search returns server-side result in tool-call arguments.
    if params.get("search_result") is not None:
        return {"status": "success", "result": json.dumps(params)}

    motet = _get_motet_context_optional()
    provider, model_name = _resolve_model_identity(motet, params)
    if motet and (provider or model_name):
        _stamp_model_metadata(motet, provider, model_name)

    if motet and _current_model_web_search_passthrough(motet, provider, model_name):
        return {"status": "success", "result": json.dumps(params)}

    query = params.get("query")
    max_results = int(params.get("max_results", 5))
    if not query:
        return err("query is required")

    forced_backend = (os.getenv("MOTET_WEB_SEARCH_BACKEND") or "").strip().lower()
    # Normalize aliases
    if forced_backend in ("duckduckgo", "instant", "ia"):
        forced_backend = "instant_answers"
    logger.info(
        "web_search_start",
        query=query,
        max_results=max_results,
        provider=provider or None,
        model_name=model_name or None,
        forced_backend=forced_backend or None,
    )

    try_llm = forced_backend in ("", "auto", "llm")
    try_ddgs = forced_backend in ("", "auto", "ddgs", "llm")
    try_instant = forced_backend in ("", "auto", "ddgs", "llm", "instant_answers")

    # 1) LLM-native path when model identity is available and usable
    if try_llm and motet and _llm_web_search_usable(motet, provider, model_name):
        llm_result = _try_llm_web_search(
            query, max_results, motet, provider=provider, model_name=model_name
        )
        if llm_result is not None:
            return llm_result

    # 2) ddgs metasearch (real SERP results with URLs)
    if try_ddgs:
        ddgs_result = _search_ddgs(query, max_results)
        if ddgs_result is not None:
            return ddgs_result

    # 3) Instant Answers last resort (entity/QA; often empty for long queries)
    if try_instant:
        instant = _search_instant_answers(query, max_results)
        if instant is not None:
            return instant

    return _normalize_success(
        query,
        [],
        path=forced_backend or "ddgs",
        main_content=f"No specific results found for '{query}'.",
    )


def create_browser_search_command(query: str, task_id: str) -> Dict[str, Any]:
    """Create browser-based DuckDuckGo search parameters for distributed execution."""
    search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    return {
        "url": search_url,
        "method": "GET",
        "javascript_enabled": True,
        "wait_for_selector": ".result, .web-result, .result__body",
        "timeout": 45,
        "extract_strategy": "auto",
        "include_links": True,
        "headless": True,
    }


def register(registry: ToolRegistry) -> None:
    """Register the web search tool with the tool registry."""
    registry.register(
        name="core.web_search",
        func=run_web_search,
        description=(
            "Search the web for current information. Useful for finding recent data, "
            "news, facts, and general information."
        ),
        category="search",
        triggers=["search:", "web:", "find:", "lookup:"],
        priority=6,
        data_types=["web", "search", "information", "current", "news", "facts"],
        keywords=[
            "search",
            "find",
            "lookup",
            "web",
            "internet",
            "current",
            "recent",
            "information",
            "data",
            "facts",
            "news",
            "popular",
            "attractions",
            "events",
            "activities",
            "tourism",
            "travel",
            "guide",
            "what",
            "where",
            "when",
            "how",
            "boston",
            "city",
            "location",
        ],
        tool_schema=WebSearchParams,
    )
