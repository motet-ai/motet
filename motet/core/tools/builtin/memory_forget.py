"""
Motet - Memory Forget Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Agent memory forget tool. Deletes targeted memories from KV and the
    long-term vector index. Requires memory_ids, conversation_id, or
    filter_tag (same selectors as core.memory_tag). Does not wrap HTTP
    operator clear.

Dependencies:
    - pydantic: Tool parameter schema
    - MotetContext: MemoryManager.forget
    - structlog: Structured error logging

Usage:
    from motet.core.tools.builtin.memory_forget import run

    result = run({"memory_ids": ["mem_123"]})

Notes:
    - Not on the default store/recall pin list. Separate forget-intent pins
      admit this tool when the user says "forget that" / "please forget".
    - HTTP POST /api/v1/memories/clear stays operator-only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog
from pydantic import BaseModel, Field, ValidationError

from ..registry import ToolRegistry

logger = structlog.get_logger(__name__)


class Params(BaseModel):
    """Parameters for deleting existing memory items."""

    memory_ids: Optional[List[str]] = Field(
        default=None,
        description="Memory IDs to delete. Required unless conversation_id or filter_tag is set.",
    )
    conversation_id: Optional[str] = Field(
        default=None,
        description=(
            "Delete memories stamped with this conversation. Combined with "
            "filter_tag, only items matching both are deleted."
        ),
    )
    filter_tag: Optional[str] = Field(
        default=None,
        description=(
            "Delete memories that already have this tag. Combined with "
            "conversation_id, only items matching both are deleted."
        ),
    )


def _get_motet_context_optional() -> Any:
    try:
        from motet.core.commands.decorator import get_motet_context

        return get_motet_context()
    except Exception:
        return None


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Forget targeted memories. Requires ids or a conversation/tag filter."""
    try:
        parsed = Params.model_validate(params or {})
    except ValidationError as exc:
        return {"error": str(exc)}

    motet = _get_motet_context_optional()
    if motet is None or getattr(motet, "memory", None) is None:
        return {"error": "Motet context is required to forget memory"}

    memory_ids = list(parsed.memory_ids or []) or None
    conversation_id = parsed.conversation_id
    filter_tag = parsed.filter_tag
    if not memory_ids and not conversation_id and not filter_tag:
        return {"error": "memory_ids, conversation_id, or filter_tag is required"}

    try:
        result = motet.memory.forget(
            memory_ids=memory_ids,
            conversation_id=conversation_id,
            filter_tag=filter_tag,
            motet_context=motet,
        )
    except Exception as exc:
        logger.error(
            "memory_forget_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return {"error": str(exc)}

    if isinstance(result, dict) and result.get("error"):
        return {"error": result["error"]}

    deleted = int((result or {}).get("deleted") or 0) if isinstance(result, dict) else 0
    affected = list((result or {}).get("ids") or []) if isinstance(result, dict) else []
    vector_deleted = (
        int((result or {}).get("vector_deleted") or 0) if isinstance(result, dict) else 0
    )
    if conversation_id:
        message = f"Forgot {deleted} memory item(s) in conversation scope"
    elif filter_tag:
        message = f"Forgot {deleted} memory item(s) matching filter"
    else:
        message = f"Forgot {deleted} memory item(s)"
    return {
        "status": "success",
        "deleted": deleted,
        "ids": affected[:50],
        "vector_deleted": vector_deleted,
        "message": message,
    }


def _fmt(res: Dict[str, Any]) -> str:
    if "error" in res:
        return f"memory_forget(error={res['error']})"
    return f"memory_forget(deleted={res.get('deleted')})"


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.memory_forget",
        description=(
            "Delete existing memories from short-term and long-term storage. "
            "Requires memory_ids, conversation_id, or filter_tag. Prefer "
            "core.memory_recall to look memories up by meaning first. Does not "
            "clear a tenant or memory type."
        ),
        func=run,
        tool_schema=Params,
        priority=6,
        observation_formatter=_fmt,
        category="memory",
        keywords=["forget", "delete memory", "unremember", "erase"],
    )


__all__ = ["register"]
