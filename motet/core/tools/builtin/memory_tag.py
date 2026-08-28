"""
Motet - Memory Tag Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-21

Description:
    Agent memory tagging tool. Add, remove, or set tags on existing memories.
    Requires memory_ids, conversation_id, or filter_tag (issue #217).

Dependencies:
    - pydantic: Tool parameter schema
    - MotetContext: motet.memory.tag
    - structlog: Structured error logging

Usage:
    from motet.core.tools.builtin.memory_tag import run

    result = run({
        "memory_ids": ["mem_123"],
        "tags": ["important"],
        "op": "add",
    })

Notes:
    - Default agent retrieve path is core.memory_recall, not tagging.
    - HTTP POST /api/v1/memories/tag calls MemoryManager.retag directly.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog
from pydantic import BaseModel, Field, ValidationError

from ..registry import ToolRegistry

logger = structlog.get_logger(__name__)


class Params(BaseModel):
    """Parameters for retagging existing memory items."""

    memory_ids: Optional[List[str]] = Field(
        default=None,
        description="Memory IDs to retag. Required unless conversation_id or filter_tag is set.",
    )
    conversation_id: Optional[str] = Field(
        default=None,
        description=(
            "Retag memories stamped with this conversation. Combined with "
            "filter_tag, only items matching both are retagged."
        ),
    )
    filter_tag: Optional[str] = Field(
        default=None,
        description=(
            "Retag memories that already have this tag. Combined with "
            "conversation_id, only items matching both are retagged."
        ),
    )
    tags: List[str] = Field(default_factory=list, min_length=1, description="Tags to add, remove, or set")
    op: str = Field(default="add", description="add | remove | set")


def _get_motet_context_optional() -> Any:
    try:
        from motet.core.commands.decorator import get_motet_context

        return get_motet_context()
    except Exception:
        return None


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Retag targeted memories. Requires ids or a conversation/tag filter."""
    try:
        parsed = Params.model_validate(params or {})
    except ValidationError as exc:
        return {"error": str(exc)}

    motet = _get_motet_context_optional()
    if motet is None or getattr(motet, "memory", None) is None:
        return {"error": "Motet context is required to tag memory"}

    tags = list(parsed.tags)
    op = parsed.op.lower()
    if op not in {"add", "remove", "set"}:
        return {"error": "op must be one of add|remove|set"}

    memory_ids = list(parsed.memory_ids or []) or None
    conversation_id = parsed.conversation_id
    filter_tag = parsed.filter_tag
    if not memory_ids and not conversation_id and not filter_tag:
        return {"error": "memory_ids, conversation_id, or filter_tag is required"}

    try:
        result = motet.memory.tag(
            tags=tags,
            op=op,
            memory_ids=memory_ids,
            conversation_id=conversation_id,
            filter_tag=filter_tag,
            motet_context=motet,
        )
    except Exception as exc:
        logger.error(
            "memory_tag_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return {"error": str(exc)}

    if isinstance(result, dict) and result.get("error"):
        return {"error": result["error"]}

    updated = int((result or {}).get("updated") or 0) if isinstance(result, dict) else 0
    affected = list((result or {}).get("ids") or []) if isinstance(result, dict) else []
    if conversation_id:
        message = f"Tagged {updated} memory item(s) in conversation scope"
    elif filter_tag:
        message = f"Tagged {updated} memory item(s) matching filter"
    else:
        message = f"Tagged {updated} memory item(s)"
    return {
        "status": "success",
        "updated": updated,
        "ids": affected[:50],
        "message": message,
    }


def _fmt(res: Dict[str, Any]) -> str:
    if "error" in res:
        return f"memory_tag(error={res['error']})"
    return f"memory_tag(updated={res.get('updated')})"


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.memory_tag",
        description=(
            "Add, remove, or set tags on existing memories. Requires memory_ids, "
            "conversation_id, or filter_tag. Prefer core.memory_recall to look "
            "memories up by meaning."
        ),
        func=run,
        tool_schema=Params,
        priority=6,
        observation_formatter=_fmt,
        category="memory",
        keywords=["tag", "retag", "labels"],
    )


__all__ = ["register"]
