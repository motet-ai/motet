"""
Motet SDK - Deep Research Example: Recall Research Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-03

Description:
Recall previously completed deep research reports from memory by topic.
Completes the research lifecycle: the research workflow stores reports in
principal-scoped memory, and this tool retrieves them without re-running
the full pipeline.

Demonstrates @motet.tool for custom bundle tool registration (ADR-0089)
and principal-scoped memory recall (reports intentionally survive across
conversations).

Dependencies:
- motet_sdk: @motet.tool decorator, MotetContext
- pydantic: tool parameter schema

Usage:
The agent can call this tool when the user asks about previously
researched topics:
  "What did we find about quantum computing?"
  → deep-research.recall_research(topic="quantum computing")

Notes:
- Registered as deep-research.recall_research via the bundle loader.
- Synthesize stores with scope_type="principal" and tag "deep-research".
- Topic filtering is done by MemoryManager.recall_principal (query coverage,
  head-biased) — this tool just formats the rows.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from motet_sdk import get_motet_context, motet

# Stricter than hybrid_retrieve's 0.5 default: topic recall should require the
# memory to name essentially the whole topic.
_MIN_TOPIC_RELEVANCE = 0.8


class RecallResearchParams(BaseModel):
    """Input for recall_research tool."""

    topic: str = Field(..., description="Topic or keywords to search for in past research reports")
    limit: int = Field(default=3, ge=1, le=10, description="Maximum number of reports to return")


def _fmt(res: Dict[str, Any]) -> str:
    count = res.get("result_count", 0)
    topic = res.get("topic", "?")
    return f"recall_research(topic={topic!r}, found={count})"


def _as_dict(item: Any) -> Dict[str, Any]:
    if hasattr(item, "model_dump"):
        dumped = item.model_dump()
        return dumped if isinstance(dumped, dict) else {"content": str(item)}
    if isinstance(item, dict):
        return item
    return {"content": str(item)}


def _format_results(items: List[Any], limit: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in items:
        dumped = _as_dict(item)
        tags = set(dumped.get("tags") or [])
        if "deep-research" not in tags and dumped.get("type") != "research_report":
            if "research" not in tags:
                continue
        raw_meta = dumped.get("metadata")
        meta: Dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        rows.append(
            {
                "content": dumped.get("content", ""),
                "metadata": meta,
                "tags": dumped.get("tags", []),
                "created_at": str(dumped.get("created_at", "")),
                "memory_id": dumped.get("id") or dumped.get("memory_id"),
                "scope_type": dumped.get("scope_type"),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _recall_principal(ctx: Any, topic: str, limit: int) -> Optional[List[Dict[str, Any]]]:
    principal_id = getattr(ctx, "principal_id", None)
    memory = getattr(ctx, "memory", None)
    if not principal_id or memory is None or not hasattr(memory, "recall_principal"):
        return None
    raw = memory.recall_principal(
        principal_id=principal_id,
        limit=limit,
        types=["research_report"],
        query=topic,
        tags=["deep-research"],
        min_relevance=_MIN_TOPIC_RELEVANCE,
        motet_context=ctx,
    )
    formatted = _format_results(list(raw or []), limit)
    if formatted:
        return formatted
    # Some stores may omit type; retry without the type filter.
    raw = memory.recall_principal(
        principal_id=principal_id,
        limit=limit,
        query=topic,
        tags=["deep-research"],
        min_relevance=_MIN_TOPIC_RELEVANCE,
        motet_context=ctx,
    )
    return _format_results(list(raw or []), limit)


@motet.tool(
    description=(
        "Recall a previously completed deep research report from memory. "
        "Use when the user asks about a topic that was already researched. "
        "Accepts 'topic' (string, required) and 'limit' (int, default 3)."
    ),
    name="recall_research",
    schema=RecallResearchParams,
    observation_formatter=_fmt,
    category="research",
    cost_class="low",
    keywords=["research", "recall", "memory", "report", "findings"],
)
def recall_research(params: Dict[str, Any]) -> Dict[str, Any]:
    """Recall previously stored research findings by topic from memory."""
    parsed = RecallResearchParams(**(params or {}))

    try:
        ctx = get_motet_context()
    except Exception:
        ctx = None

    if not ctx or not getattr(ctx, "memory", None):
        return {
            "topic": parsed.topic,
            "results": [],
            "result_count": 0,
            "error": "Memory not available in current context.",
        }

    try:
        principal_hits = _recall_principal(ctx, parsed.topic, parsed.limit)
        if principal_hits is not None and principal_hits:
            return {
                "topic": parsed.topic,
                "results": principal_hits,
                "result_count": len(principal_hits),
                "recall_path": "principal",
            }

        items = ctx.memory.recall(
            query=parsed.topic,
            tags=["deep-research"],
            limit=parsed.limit,
            min_relevance=_MIN_TOPIC_RELEVANCE,
        )
        results = _format_results(list(items or []), parsed.limit)
        return {
            "topic": parsed.topic,
            "results": results,
            "result_count": len(results),
            "recall_path": "hybrid_tagged",
        }
    except Exception as exc:
        return {
            "topic": parsed.topic,
            "results": [],
            "result_count": 0,
            "error": f"Memory recall failed: {exc}",
        }
