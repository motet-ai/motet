"""
Motet - Memory Recall Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Agent-facing query recall over Motet hybrid/semantic memory (issue #217).
    Wraps MemoryManager.hybrid_retrieve / the memory_recall command. This is
    the default agent retrieve path.

Dependencies:
    - pydantic: Tool parameter schema
    - MotetContext: identity and MemoryManager / MotetMemoryHelper facade

Usage:
    from motet.core.tools.builtin.memory_recall import run

    result = run({"query": "what do I know about the user's kids?"})

Notes:
    - Automatic MemoryRecallProvider already injects hybrid results each turn.
      Call this when that injection is not enough or the user asked to look up
      a specific fact.
    - Do not implicitly force conversation scope on a query; pass
      conversation_id only when the caller asked to narrow.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ValidationError

from motet.core.types import serialize_memory_items

from ..registry import ToolRegistry


class Params(BaseModel):
    """Parameters for query-based hybrid memory recall."""

    query: str = Field(
        ...,
        description="Natural-language question or topic to look up in memory",
    )
    tags: List[str] = Field(
        default_factory=list,
        description="Optional tag filter; not required when searching by meaning",
    )
    limit: int = Field(default=5, ge=1, le=50, description="Maximum memories to return")
    min_relevance: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Minimum hybrid keyword relevance (ignored for semantic/recent)",
    )
    conversation_id: Optional[str] = Field(
        default=None,
        description="Optional conversation scope. Omit to search across the principal.",
    )


def _get_motet_context_optional() -> Any:
    try:
        from motet.core.commands.decorator import get_motet_context

        return get_motet_context()
    except Exception:
        return None


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Recall memories by natural-language query via hybrid retrieve."""
    try:
        parsed = Params.model_validate(params or {})
    except ValidationError as exc:
        return {"error": str(exc)}

    motet = _get_motet_context_optional()
    if motet is None or getattr(motet, "memory", None) is None:
        return {"error": "Motet context is required to recall memory"}

    query = parsed.query.strip()
    if not query:
        return {"error": "query is required and cannot be empty"}

    tags = [t for t in parsed.tags if t]
    conversation_id = parsed.conversation_id

    try:
        recalled = motet.memory.recall(
            query=query,
            limit=parsed.limit,
            tags=tags,
            min_relevance=parsed.min_relevance,
            conversation_id=conversation_id,
        )
    except Exception as exc:
        return {"error": str(exc)}

    if isinstance(recalled, dict):
        if recalled.get("error"):
            return {"error": recalled["error"]}
        items = serialize_memory_items(recalled.get("items") or recalled)
    else:
        items = serialize_memory_items(recalled)

    return {"status": "success", "items": items, "count": len(items)}


def _fmt(res: Dict[str, Any]) -> str:
    if "error" in res:
        return f"memory_recall(error={res['error']})"
    items = res.get("items") or []
    if not items:
        return "memory_recall(n=0)"
    lines: List[str] = []
    for item in items[:5]:
        content = str(item.get("content") or "")
        snippet = content[:200].replace("\n", " ")
        itype = item.get("type") or "item"
        lines.append(f"[{itype}] {snippet}")
    body = "\n".join(lines)[:600]
    return f"memory_recall(n={len(items)})\n{body}"


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.memory_recall",
        description=(
            "Look up stored memories by meaning (hybrid/semantic recall). "
            "Use a natural-language query such as 'user's kids' — do not invent "
            "tags. Chat already injects relevant memories each turn; call this "
            "when that is not enough or the user asked what you remember. "
            "Do not use core.note; it does not read or write memory."
        ),
        func=run,
        tool_schema=Params,
        priority=6,
        observation_formatter=_fmt,
        category="memory",
        keywords=["recall", "remember", "memory", "lookup", "what do I know"],
    )


__all__ = ["register"]
