"""
Motet SDK - Deep Research Example: Single Web Search Command

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-03

Description:
Execute a single web search query via core.web_search.  Designed to be
fanned out in parallel from gather_sources via motet.apply().

Dependencies:
- motet_sdk: command decorator and MotetContext typing
- pydantic: structured command inputs

Usage:
Called by deep-research.gather_sources using motet.apply:
  deep-research.search_source(query="quantum computing 2026 breakthroughs")

Notes:
- Returns structured success/failure payloads so the pipeline aggregation
  can proceed even when some searches fail.
- Optional provider/model_name are forwarded to core.web_search so the
  LLM-native search path can run when the model supports it; ddgs remains
  the URL-bearing fallback for long research queries.
- Tool results arrive context-processed, which namespaces a tool's output keys
  ("web_search.results") and label-prefixes scalar values
  ("web_search_path: ddgs"). _search_items / _search_path show the
  normalization every bundle needs when reading tool output.
"""

from __future__ import annotations

import ast
from typing import Any, Dict, List, Optional

from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, WorkerCapability, motet


class SearchSourceData(BaseCommandData):
    """Input for deep-research.search_source."""

    query: str = Field(..., description="Search query to execute")
    max_results: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum search results to return",
    )
    provider: Optional[str] = Field(
        default=None,
        description="Optional LLM provider forwarded to core.web_search",
    )
    model_name: Optional[str] = Field(
        default=None,
        description="Optional LLM model name forwarded to core.web_search",
    )


def _coerce_list(value: Any) -> List[Dict[str, Any]]:
    """Convert a tool output value into a list of dict items."""
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    if isinstance(value, str) and value.strip():
        # "data" arrives as a Python repr of the item list, not JSON.
        try:
            parsed = ast.literal_eval(value.strip())
        except (ValueError, SyntaxError):
            return []
        if isinstance(parsed, list):
            return [v for v in parsed if isinstance(v, dict)]
    return []


def _search_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Pull the result items out of a core.web_search payload.

    Context processing namespaces each tool's output, so the live shape is
    "web_search.results"; the flat key and the repr-stringified "data" blob
    cover callers that bypass context processing.
    """
    if not isinstance(payload, dict):
        return []
    for key in ("web_search.results", "results", "data"):
        items = _coerce_list(payload.get(key))
        if items:
            return items
    return []


def _search_path(payload: Dict[str, Any]) -> Optional[str]:
    """Report which backend answered the search (native LLM search vs ddgs)."""
    direct = payload.get("web_search_path")
    if isinstance(direct, str) and direct:
        return direct
    dotted = payload.get("web_search.web_search_path")
    if isinstance(dotted, str) and dotted:
        # Context-processed scalars are label-prefixed: "web_search_path: ddgs".
        _, _, tail = dotted.partition(":")
        return tail.strip() or dotted
    return None


@motet.command(
    timeout_seconds=45,
    required_capabilities=[WorkerCapability.TOOL_EXECUTION],
)
def search_source(data: SearchSourceData, motet: MotetContext) -> Dict[str, Any]:
    """Execute one web search and return structured results."""
    try:
        tool_params: Dict[str, Any] = {
            "query": data.query,
            "max_results": data.max_results,
        }
        if data.provider:
            tool_params["provider"] = data.provider
        if data.model_name:
            tool_params["model_name"] = data.model_name

        tool_result = motet.tools.execute("core.web_search", tool_params)
        if not isinstance(tool_result, dict):
            tool_result = {"raw_result": tool_result}

        results: List[Dict[str, str]] = []
        for item in _search_items(tool_result):
            if isinstance(item, dict) and item.get("url"):
                results.append(
                    {
                        "url": item["url"],
                        "title": item.get("title", item.get("name", "")),
                        "snippet": item.get(
                            "content",
                            item.get("snippet", item.get("summary", item.get("text", ""))),
                        ),
                    }
                )

        return {
            "ok": True,
            "query": data.query,
            "results": results,
            "result_count": len(results),
            "web_search_path": _search_path(tool_result),
        }
    except Exception as exc:
        return {
            "ok": False,
            "query": data.query,
            "results": [],
            "result_count": 0,
            "error": str(exc),
        }
