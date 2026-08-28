"""
Motet SDK - Background Thinker Example: Shared Memory Helpers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-03

Description:
Shared principal-scoped memory helpers for background-thinker commands.
Insights and schedule-tracking rows are stored with scope_type="principal"
so they survive across conversations; these helpers prefer recall_principal
(with query relevance) and fall back to tagged hybrid recall.

Dependencies:
- typing only (MotetContext is duck-typed)

Usage:
  from . import _memory as mem
  items = mem.recall_insights(motet, topic="quantum computing", limit=5)
  schedule_id = mem.find_schedule_id(motet, topic="quantum computing")

Notes:
- Underscore modules are skipped by the command loader but importable via
  ``from . import _memory`` (bundle package hierarchy).
- Topic filtering is MemoryManager.recall_principal / hybrid_retrieve
  (query coverage, head-biased); this module only formats rows.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

_MIN_TOPIC_RELEVANCE = 0.8


def _as_dict(item: Any) -> Dict[str, Any]:
    if hasattr(item, "model_dump"):
        dumped = item.model_dump()
        return dumped if isinstance(dumped, dict) else {"content": str(item)}
    if isinstance(item, dict):
        return item
    return {"content": str(item)}


def _format_insight(dumped: Dict[str, Any]) -> Dict[str, Any]:
    raw_meta = dumped.get("metadata")
    meta: Dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    return {
        "content": dumped.get("content", ""),
        "iteration": meta.get("iteration", "?"),
        "topic": meta.get("topic", ""),
        "created_at": str(dumped.get("created_at", "")),
        "memory_id": dumped.get("id") or dumped.get("memory_id"),
        "scope_type": dumped.get("scope_type"),
        "tags": dumped.get("tags", []),
        "metadata": meta,
        "type": dumped.get("type"),
    }


def _format_insights(items: List[Any], limit: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in items:
        dumped = _as_dict(item)
        tags = set(dumped.get("tags") or [])
        if "background-thinker" not in tags or "insight" not in tags:
            if dumped.get("type") != "background_insight":
                continue
        rows.append(_format_insight(dumped))
        if len(rows) >= limit:
            break
    return rows


def recall_insights(motet: Any, topic: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Recall principal-scoped background insights for a topic."""
    memory = getattr(motet, "memory", None)
    if memory is None:
        return []

    principal_id = getattr(motet, "principal_id", None)
    if principal_id and hasattr(memory, "recall_principal"):
        raw = memory.recall_principal(
            principal_id=principal_id,
            limit=limit,
            types=["background_insight"],
            query=topic,
            tags=["background-thinker", "insight"],
            min_relevance=_MIN_TOPIC_RELEVANCE,
            motet_context=motet,
        )
        formatted = _format_insights(list(raw or []), limit)
        if formatted:
            return formatted
        raw = memory.recall_principal(
            principal_id=principal_id,
            limit=limit,
            query=topic,
            tags=["background-thinker", "insight"],
            min_relevance=_MIN_TOPIC_RELEVANCE,
            motet_context=motet,
        )
        formatted = _format_insights(list(raw or []), limit)
        if formatted:
            return formatted

    try:
        items = memory.recall(
            query=topic,
            tags=["background-thinker", "insight"],
            limit=limit,
            min_relevance=_MIN_TOPIC_RELEVANCE,
        )
    except Exception:
        return []
    return _format_insights(list(items or []), limit)


def find_schedule_id(motet: Any, topic: str) -> Optional[str]:
    """Look up a schedule_id stored by start_thinking for the given topic."""
    memory = getattr(motet, "memory", None)
    if memory is None:
        return None

    topic_l = topic.lower()
    principal_id = getattr(motet, "principal_id", None)
    candidates: List[Any] = []
    if principal_id and hasattr(memory, "recall_principal"):
        try:
            candidates = list(
                memory.recall_principal(
                    principal_id=principal_id,
                    limit=50,
                    types=["schedule_tracking"],
                    query=topic,
                    tags=["background-thinker-schedule"],
                    min_relevance=0.5,
                    motet_context=motet,
                )
                or []
            )
        except Exception:
            candidates = []
        if not candidates:
            try:
                candidates = list(
                    memory.recall_principal(
                        principal_id=principal_id,
                        limit=50,
                        types=["schedule_tracking"],
                        motet_context=motet,
                    )
                    or []
                )
            except Exception:
                candidates = []

    if not candidates:
        try:
            candidates = list(
                memory.recall(
                    query=f"background thinking schedule {topic}",
                    tags=["background-thinker-schedule"],
                    limit=10,
                )
                or []
            )
        except Exception:
            return None

    for item in candidates:
        dumped = _as_dict(item)
        tags = set(dumped.get("tags") or [])
        if "background-thinker-schedule" not in tags and dumped.get("type") != "schedule_tracking":
            continue
        raw_meta = dumped.get("metadata")
        meta: Dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        if str(meta.get("topic") or "").lower() == topic_l:
            sid = meta.get("schedule_id")
            if sid:
                return str(sid)
    return None
