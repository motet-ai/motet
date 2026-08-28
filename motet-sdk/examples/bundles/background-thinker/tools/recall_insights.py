"""
Motet SDK - Background Thinker Example: Recall Insights Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-03

Description:
Recall previously generated background thinking insights from memory
by topic. Completes the knowledge loop: the reflect command writes
insights in principal-scoped memory, and this tool retrieves them on
demand during conversation.

Demonstrates @motet.tool for custom bundle tool registration (ADR-0089)
and principal-scoped memory recall from a tool context.

Dependencies:
- motet_sdk: @motet.tool decorator, get_motet_context
- pydantic: tool parameter schema

Usage:
The agent calls this tool when the user asks about background thinking:
  "What have you been thinking about quantum computing?"
  → background-thinker.recall_insights(topic="quantum computing")

Notes:
- Registered as background-thinker.recall_insights via the bundle loader.
- Prefers recall_principal (same scope reflect writes), then tagged hybrid
  recall with "background-thinker" + "insight" tags.
- Topic filtering is done by MemoryManager (query coverage, head-biased).
- Returns raw insight content — for a synthesized summary, use the
  check_insights command instead.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from motet_sdk import get_motet_context, motet

_MIN_TOPIC_RELEVANCE = 0.8


class RecallInsightsParams(BaseModel):
    """Input for recall_insights tool."""

    topic: str = Field(..., description="Topic or keywords to search for in background insights")
    limit: int = Field(default=5, ge=1, le=20, description="Maximum number of insights to return")


def _fmt(res: Dict[str, Any]) -> str:
    count = res.get("result_count", 0)
    topic = res.get("topic", "?")
    return f"recall_insights(topic={topic!r}, found={count})"


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
        if "background-thinker" not in tags or "insight" not in tags:
            if dumped.get("type") != "background_insight":
                continue
        raw_meta = dumped.get("metadata")
        meta: Dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        rows.append(
            {
                "content": dumped.get("content", ""),
                "iteration": meta.get("iteration", "?"),
                "topic": meta.get("topic", ""),
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
        types=["background_insight"],
        query=topic,
        tags=["background-thinker", "insight"],
        min_relevance=_MIN_TOPIC_RELEVANCE,
        motet_context=ctx,
    )
    formatted = _format_results(list(raw or []), limit)
    if formatted:
        return formatted
    raw = memory.recall_principal(
        principal_id=principal_id,
        limit=limit,
        query=topic,
        tags=["background-thinker", "insight"],
        min_relevance=_MIN_TOPIC_RELEVANCE,
        motet_context=ctx,
    )
    return _format_results(list(raw or []), limit)


@motet.tool(
    description=(
        "Recall background thinking insights from memory.  Use when the user "
        "asks what the system has been thinking about a topic that has active "
        "or past background thinking.  Accepts 'topic' (string, required) and "
        "'limit' (int, default 5)."
    ),
    name="recall_insights",
    schema=RecallInsightsParams,
    observation_formatter=_fmt,
    category="background-thinking",
    cost_class="low",
    keywords=["think", "insight", "reflect", "background", "memory", "recall"],
)
def recall_insights(params: Dict[str, Any]) -> Dict[str, Any]:
    """Recall background thinking insights from memory by topic."""
    parsed = RecallInsightsParams(**(params or {}))

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
            tags=["background-thinker", "insight"],
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
