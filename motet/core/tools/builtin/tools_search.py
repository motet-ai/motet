"""
Motet - Tools Search Builtin

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Tools search builtin for the Motet distributed framework.
    Primary path: embedding / hybrid ranking via FunctionDiscoveryVectorStore
    (on-demand search for meta-tool progressive disclosure). Lexical
    substring/regex scan of registry.describe() (+ workflow schemas) is the
    fallback when the store is unavailable, when the caller requests regex
    search, or when mode=lexical.

    Search observations include full JSON schemas by default so the model can
    invoke matches via ``core.tool_call`` without admitting those schemas into
    the tools prefix.
    Results cover both registry tools and ``workflow_*`` entries unless the
    agent's ToolFilter sets no_workflows / exclude_workflows. The
    ``include_workflows`` constructor flag is hidden from the LLM schema
    (``x-imf-hide-from-llm``) so the model cannot opt out of the workflow
    slice; tests and internal callers may still pass it.
    Semantic ranking is split by type: top ``limit`` tools plus up to 3
    workflows (omitted when they have no score signal versus the tool floor).
    The assembled list puts the first tool, then workflows, then remaining
    tools so a Playwright sibling wall cannot hide a composed workflow.
    Results are filtered by ToolFilter metadata when present.

Dependencies:
    - re: Regular expression matching and pattern search
    - pydantic: Data validation and model definitions
    - typing: Type hints and annotations
    - Tool registry and protocol system
    - WorkflowRegistry: canonical workflow tool schemas
    - FunctionDiscoveryVectorStore: hybrid semantic ranking
    - motet.core.tools.meta_tool_policy: ToolFilter disclosure filter

Usage:
    from motet.core.tools.builtin.tools_search import run

    # Semantic search when the discovery store is live (schemas included)
    result = run(registry, {"query": "multi-step web research", "limit": 10})

    # Force lexical substring scan
    result = run(registry, {"query": "http_get", "mode": "lexical"})

Notes:
    - Ranked results include ``similarity_score`` when the semantic path runs
    - Workflow hits use name ``workflow_<id>`` and category ``workflow``
    - Defaults ``include_schema=True`` for progressive disclosure
    - Omits tools/workflows excluded by the agent's ToolFilter
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import structlog
from pydantic import BaseModel, Field

from ..meta_tool_policy import (
    filter_described_tools,
    tool_filter_metadata_from_context,
)
from ..protocol import ok, err
from ..registry import ToolRegistry

logger = structlog.get_logger(__name__)

# Observation quota: tools take ``limit``; workflows are a smaller reserved
# slice so an MCP family cannot hide a composed workflow (task f8f7fd60…).
_WORKFLOW_RESULT_CAP = 3
_WORKFLOW_SCORE_FLOOR_RATIO = 0.5


class ToolsSearchParams(BaseModel):
    query: str = Field(
        description=(
            "Short task intent phrase for hybrid search (preferred), or a "
            "substring/regex when mode/regex require it. Prefer what you are "
            "trying to do, e.g. 'navigate to a website and take a screenshot'. "
            "Avoid keyword lists like 'browse website fetch URL read web page'."
        ),
    )
    fields: List[str] = Field(
        default_factory=lambda: ["name", "description", "category"],
        description=(
            "Fields for lexical search only: name, description, category, triggers. "
            "Ignored on the semantic path."
        ),
    )
    regex: bool = Field(
        default=False,
        description=(
            "Interpret query as regex if true (forces lexical path). "
            "Else intent phrase / substring."
        ),
    )
    mode: str = Field(
        default="auto",
        description=(
            "auto: semantic ranking when FunctionDiscoveryVectorStore is available, "
            "else lexical. semantic: require the store (error if unavailable). "
            "lexical: substring/regex registry scan only."
        ),
    )
    include_workflows: bool = Field(
        default=True,
        description=(
            "Internal only: include the separately ranked workflow_* slice. "
            "Hidden from the LLM schema. Agent policy uses ToolFilter."
        ),
        json_schema_extra={"x-imf-hide-from-llm": True},
    )
    include_schema: bool = Field(
        default=True,
        description=(
            "Include each match's JSON schema in the observation (default True). "
            "Schemas in the observation let core.tool_call invoke the tool "
            "or workflow without adding it to this turn's tools array."
        ),
    )
    include_x_imf: bool = Field(default=True)
    limit: Optional[int] = Field(
        default=10,
        description="Maximum matches to return (default 10).",
    )


def _get_motet_context_optional() -> Any:
    try:
        from motet.core.commands.decorator import get_motet_context

        return get_motet_context()
    except Exception:
        return None


def _matches(item: Dict[str, Any], params: ToolsSearchParams) -> bool:
    q = params.query
    if not q:
        return False
    vals: List[str] = []
    for f in params.fields:
        if f == "triggers":
            vs = item.get("triggers") or []
            vals.extend([str(v) for v in vs])
        else:
            vals.append(str(item.get(f, "")))
    text = "\n".join(vals)
    if params.regex:
        try:
            return re.search(q, text, re.IGNORECASE) is not None
        except Exception:
            return False
    return q.lower() in text.lower()


def _strip_optional_fields(
    item: Dict[str, Any], *, include_schema: bool, include_x_imf: bool
) -> Dict[str, Any]:
    if include_schema and include_x_imf:
        return item
    out = dict(item)
    if not include_schema:
        out.pop("schema", None)
    if not include_x_imf:
        out.pop("x-imf", None)
    return out


def _described_by_name(registry: ToolRegistry) -> Dict[str, Dict[str, Any]]:
    try:
        items = registry.describe()
    except Exception:
        return {}
    by_name: Dict[str, Dict[str, Any]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("name")
        if isinstance(name, str) and name:
            by_name[name] = it
    return by_name


def _result_limit(params: ToolsSearchParams) -> int:
    if params.limit is None:
        return 10
    return max(0, params.limit)


def _workflow_id_from_tool_name(name: str) -> str:
    return name[9:] if name.startswith("workflow_") else name


def _workflow_described_by_name() -> Dict[str, Dict[str, Any]]:
    """Build describe-shaped dicts for agent-callable workflows."""
    try:
        from ...workflow import WorkflowRegistry
    except Exception:
        return {}

    try:
        schemas = WorkflowRegistry.export_canonical_schemas() or []
    except Exception as exc:
        logger.warning(
            "tools_search_workflow_export_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return {}

    by_name: Dict[str, Dict[str, Any]] = {}
    for schema in schemas:
        try:
            name = getattr(schema, "name", None)
            if not isinstance(name, str) or not name:
                continue
            workflow_id = _workflow_id_from_tool_name(name)
            json_schema = getattr(schema, "json_schema", None) or {}
            if hasattr(json_schema, "model_dump"):
                json_schema = json_schema.model_dump()
            live = WorkflowRegistry.get(workflow_id)
            keywords = live.discovery_keywords() if live is not None else []
            by_name[name] = {
                "name": name,
                "description": getattr(schema, "description", "") or "",
                "category": "workflow",
                "triggers": [],
                "priority": 0,
                "schema": dict(json_schema) if isinstance(json_schema, dict) else {},
                "data_types": [],
                "keywords": keywords,
                "expose_to_agents": True,
                "type": "workflow",
                "workflow_id": workflow_id,
            }
        except Exception as exc:
            logger.debug(
                "tools_search_workflow_describe_skip",
                error=str(exc),
                error_type=type(exc).__name__,
            )
    return by_name


def _observation_items(
    assembled: List[Dict[str, Any]], params: ToolsSearchParams
) -> List[Dict[str, Any]]:
    return [
        _strip_optional_fields(
            it,
            include_schema=params.include_schema,
            include_x_imf=params.include_x_imf,
        )
        for it in assembled
    ]


def _discovery_store(motet: Any) -> Any:
    if motet is None:
        return None
    store = getattr(motet, "function_discovery_store", None)
    if store is None:
        return None
    try:
        if hasattr(store, "is_initialized") and not store.is_initialized():
            return None
    except Exception:
        return None
    return store


def _attach_score(item: Dict[str, Any], score: Any) -> Dict[str, Any]:
    out = dict(item)
    if score is not None:
        try:
            out["similarity_score"] = float(score)
        except (TypeError, ValueError):
            pass
    return out


def _is_workflow_item(item: Dict[str, Any]) -> bool:
    if str(item.get("type") or "") == "workflow":
        return True
    return str(item.get("name") or "").startswith("workflow_")


def _item_score(item: Dict[str, Any]) -> Optional[float]:
    raw = item.get("similarity_score")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def assemble_typed_results(
    tools: List[Dict[str, Any]],
    workflows: List[Dict[str, Any]],
    *,
    limit: int,
    workflow_cap: int = _WORKFLOW_RESULT_CAP,
    score_floor_ratio: float = _WORKFLOW_SCORE_FLOOR_RATIO,
) -> List[Dict[str, Any]]:
    """
    Build the tools_search observation from separately ranked type lists.

    Returns top ``limit`` tools plus up to ``workflow_cap`` workflows. Workflows
    with a similarity score below ``score_floor_ratio`` of the best tool score
    are dropped (no-score lexical hits are kept). Layout is first tool, then
    workflows, then the remaining tools.
    """
    if limit <= 0:
        return []
    tools_out = [it for it in tools if not _is_workflow_item(it)][:limit]
    wf_cap = max(0, min(workflow_cap, limit))
    workflows_out = [it for it in workflows if _is_workflow_item(it)]

    if tools_out and workflows_out:
        tool_scores = [s for s in (_item_score(t) for t in tools_out) if s is not None]
        best_tool = max(tool_scores) if tool_scores else None
        if best_tool is not None and best_tool > 0:
            floor = best_tool * score_floor_ratio
            kept: List[Dict[str, Any]] = []
            for wf in workflows_out:
                score = _item_score(wf)
                if score is None or score >= floor:
                    kept.append(wf)
            workflows_out = kept
    workflows_out = workflows_out[:wf_cap]

    if not tools_out:
        return workflows_out
    if not workflows_out:
        return tools_out
    return [tools_out[0]] + workflows_out + tools_out[1:]


def _enrich_hits(
    hits: List[Any],
    *,
    tool_by_name: Dict[str, Dict[str, Any]],
    workflow_by_name: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for hit in hits or []:
        if not isinstance(hit, dict):
            continue
        hit_type = hit.get("type") or "tool"
        name = hit.get("name")
        if not isinstance(name, str) or not name or name in seen:
            continue
        if hit_type == "tool":
            described = tool_by_name.get(name)
        elif hit_type == "workflow":
            described = workflow_by_name.get(name)
            if described is None and isinstance(hit.get("workflow_id"), str):
                described = workflow_by_name.get(f"workflow_{hit['workflow_id']}")
                if described is not None:
                    name = described["name"]
        else:
            continue
        if described is None:
            continue
        seen.add(name)
        item = _attach_score(described, hit.get("similarity_score"))
        if "type" not in item:
            item["type"] = hit_type
        out.append(item)
    return out


def _semantic_search(
    registry: ToolRegistry,
    params: ToolsSearchParams,
    *,
    motet: Any,
    filter_meta: Optional[Dict[str, Any]],
) -> Optional[List[Dict[str, Any]]]:
    """
    Rank tools/workflows via FunctionDiscoveryVectorStore; enrich with schemas.

    Returns None when the store cannot serve this query (caller should fall back).
    """
    store = _discovery_store(motet)
    if store is None:
        return None

    limit = _result_limit(params)
    if limit == 0:
        return []

    search_kwargs = {
        "query": params.query,
        "tenant_id": getattr(motet, "tenant_id", None),
        "motet_id": getattr(motet, "motet_id", None),
        "principal_id": getattr(motet, "principal_id", None),
    }
    # Over-fetch so ToolFilter drops and missing describe entries do not starve the limit.
    tool_top_k = max(limit * 3, 20)
    workflow_top_k = max(min(_WORKFLOW_RESULT_CAP, limit) * 3, 10)

    try:
        tool_hits = store.search_functions(
            top_k=tool_top_k,
            search_types=["tool"],
            **search_kwargs,
        )
    except Exception as exc:
        logger.warning(
            "tools_search_semantic_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            query_len=len(params.query or ""),
            exc_info=True,
        )
        return None

    workflow_hits: List[Any] = []
    if params.include_workflows:
        try:
            workflow_hits = store.search_functions(
                top_k=workflow_top_k,
                search_types=["workflow"],
                **search_kwargs,
            ) or []
        except Exception as exc:
            logger.warning(
                "tools_search_workflow_semantic_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                query_len=len(params.query or ""),
                exc_info=True,
            )
            workflow_hits = []

    tool_by_name = _described_by_name(registry)
    workflow_by_name = (
        _workflow_described_by_name() if params.include_workflows else {}
    )
    tool_items = filter_described_tools(
        _enrich_hits(tool_hits or [], tool_by_name=tool_by_name, workflow_by_name={}),
        filter_meta,
    )
    workflow_items = filter_described_tools(
        _enrich_hits(
            workflow_hits,
            tool_by_name={},
            workflow_by_name=workflow_by_name,
        ),
        filter_meta,
    )
    assembled = assemble_typed_results(tool_items, workflow_items, limit=limit)
    return _observation_items(assembled, params)


def _lexical_search(
    registry: ToolRegistry,
    params: ToolsSearchParams,
    *,
    filter_meta: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    try:
        items = list(registry.describe())
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc

    if params.include_workflows:
        items.extend(_workflow_described_by_name().values())

    items = filter_described_tools(items, filter_meta)
    tools: List[Dict[str, Any]] = []
    workflows: List[Dict[str, Any]] = []
    for it in items:
        if not _matches(it, params):
            continue
        enriched = dict(it)
        if _is_workflow_item(enriched):
            enriched.setdefault("type", "workflow")
            workflows.append(enriched)
        else:
            enriched.setdefault("type", "tool")
            tools.append(enriched)
    assembled = assemble_typed_results(tools, workflows, limit=_result_limit(params))
    return _observation_items(assembled, params)


def run(registry: ToolRegistry, params: Dict[str, Any]) -> Dict[str, Any]:
    """Search tools and workflows by query (semantic when available; lexical fallback)."""
    try:
        p = ToolsSearchParams(**(params or {}))
    except Exception as exc:
        return err(f"validation error: {exc}")

    mode = (p.mode or "auto").strip().lower()
    if mode not in {"auto", "semantic", "lexical"}:
        return err("mode must be one of: auto, semantic, lexical")

    motet = _get_motet_context_optional()
    filter_meta = tool_filter_metadata_from_context(motet)

    # Regex / explicit lexical always use the registry (+ workflow) scan.
    force_lexical = p.regex or mode == "lexical"
    if not force_lexical and mode in {"auto", "semantic"}:
        ranked = _semantic_search(registry, p, motet=motet, filter_meta=filter_meta)
        if ranked is not None:
            return ok(ranked)
        if mode == "semantic":
            return err(
                "semantic search unavailable: FunctionDiscoveryVectorStore "
                "is not initialized in this worker context"
            )

    try:
        return ok(_lexical_search(registry, p, filter_meta=filter_meta))
    except RuntimeError as exc:
        return err(str(exc))


def _parse(line: str, trig: str) -> Dict[str, Any]:
    rest = line[len(trig) :].strip()
    if not rest:
        return {}
    # Support query=..., regex=true, fields=name,description
    params: Dict[str, Any] = {}
    for tok in [t for t in re.split(r"[ ,]+", rest) if t]:
        if "=" in tok:
            k, v = tok.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k == "fields":
                params[k] = (
                    [s.strip() for s in v.split(";") if s.strip()]
                    if ";" in v
                    else [s.strip() for s in v.split(",") if s.strip()]
                )
            elif v.lower() in {"true", "false"}:
                params[k] = v.lower() == "true"
            else:
                params[k] = v
    if "query" not in params:
        params["query"] = rest
    return params


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.tools_search",
        description=(
            "Search Motet's live catalog of tools and workflows by short "
            "task-intent phrase (hybrid embedding + keyword ranking, same "
            "store as shortlist discovery). Query with what you are trying to "
            "do (e.g. 'navigate to a website and take a screenshot'), not "
            "a keyword list "
            "('browse website fetch URL read web page'). Falls back to "
            "name/description substring match when the discovery store is "
            "unavailable. Returns the top matching tools plus up to 3 "
            "workflows (omitted when they have no signal versus the tools), "
            "with full JSON schemas and similarity_score (when ranked). "
            "Workflow hits are named "
            "workflow_<id>. To invoke a match that is not in this turn's tool "
            "list, call core.tool_call with the returned name and parameters "
            "matching its schema. Prefer a direct call when the tool is "
            "already listed."
        ),
        func=lambda p, _r=registry: run(_r, p),
        tool_schema=ToolsSearchParams,
        triggers=["tools_search:", "tool_search:", "search_tools:"],
        parse_params=_parse,
        category="system",
        contextualize_observation=False,  # Don't truncate - schemas must stay intact
        default_timeout_seconds=5.0,
        suggested_max_calls=2,
        cost_class="low",
        keywords=["search", "discover", "find", "tools", "capabilities", "mcp", "workflow"],
    )
