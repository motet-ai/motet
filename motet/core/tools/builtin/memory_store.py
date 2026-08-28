"""
Motet - Memory Store Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-21

Description:
    Agent-facing memory write tool. Persists an explicit "remember this" note
    through MotetContext so conversation_id / agent_id stamp onto the item and
    hybrid recall can find it. Defaults to long-term indexing (issue #217).
    Does not store via core.note (that tool is a no-op comment).

Dependencies:
    - pydantic: Tool parameter schema
    - MotetContext: conversation / agent identity and MemoryManager facade
    - MemoryScopeType: CONVERSATION vs PRINCIPAL scoping

Usage:
    from motet.core.tools.builtin.memory_store import run

    result = run({
        "content": "The user prefers tabs over spaces",
        "type": "note",
        "tags": ["preference"],
    })

Notes:
    - Requires MotetContext (agent / tool_execution path). HTTP store uses
      the memory_store command, not this tool.
    - persist=True (default) queues LTM vector indexing.
    - Automatic chat recall already injects hybrid results; use this only
      when the user asked to remember something.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ValidationError

from ..registry import ToolRegistry


class Params(BaseModel):
    """Parameters for persisting an explicit remember-style memory."""

    content: str = Field(..., description="The fact or note to remember")
    type: str = Field(
        default="note",
        description="Memory type: note, fact, reference, user_preference, user_profile",
    )
    tags: List[str] = Field(
        default_factory=list,
        description="Optional tags for later filtering (not required for recall)",
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional key-value metadata",
    )
    importance: Optional[str] = Field(
        default=None,
        description="Optional importance: low, medium, high",
    )
    persist: bool = Field(
        default=True,
        description=(
            "Index into long-term memory for later hybrid/semantic recall. "
            "Leave true when the user said remember / don't forget."
        ),
    )


def _get_motet_context_optional() -> Any:
    try:
        from motet.core.commands.decorator import get_motet_context

        return get_motet_context()
    except Exception:
        return None


def _resolve_scope(memory_type: str, conversation_id: Optional[str]) -> Any:
    from ...types import MemoryScopeType

    if memory_type in {"user_preference", "user_profile", "profile"}:
        return MemoryScopeType.PRINCIPAL
    if conversation_id:
        return MemoryScopeType.CONVERSATION
    return MemoryScopeType.PRINCIPAL


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Store a remember-style memory with conversation identity and LTM persist."""
    try:
        parsed = Params.model_validate(params or {})
    except ValidationError as exc:
        return {"error": str(exc)}

    motet = _get_motet_context_optional()
    if motet is None or getattr(motet, "memory", None) is None:
        return {"error": "Motet context is required to store memory"}

    content = parsed.content.strip()
    if not content:
        return {"error": "content is required and cannot be empty"}

    memory_type = parsed.type
    tags = list(parsed.tags or [])
    metadata = dict(parsed.metadata or {})
    if parsed.importance:
        metadata["importance"] = parsed.importance
    persist = parsed.persist

    conversation_id = getattr(motet, "conversation_id", None)
    scope = _resolve_scope(memory_type, conversation_id)

    try:
        result = motet.memory.store(
            content=content,
            type=memory_type,
            tags=tags,
            metadata=metadata,
            working=True,
            long_term=bool(persist),
            motet_context=motet,
            scope=scope,
        )
    except Exception as exc:
        return {"error": str(exc)}

    if isinstance(result, dict) and result.get("error"):
        return {"error": result["error"]}

    memory_id = None
    stored_in: List[str] = []
    if isinstance(result, dict):
        memory_id = result.get("memory_id") or result.get("id")
        stored_in = list(result.get("stored_in") or [])

    return {
        "status": "success",
        "memory_id": memory_id or "unknown",
        "type": memory_type,
        "tags": tags,
        "persist": bool(persist),
        "conversation_id": conversation_id,
        "stored_in": stored_in,
        "message": (
            f"Memory stored with ID {memory_id or 'unknown'}"
            f"{' and queued for long-term recall' if persist else ''}"
        ),
    }


def _fmt(res: Dict[str, Any]) -> str:
    if "error" in res:
        return f"memory_store(error={res['error']})"
    memory_id = res.get("memory_id", "unknown")
    mem_type = res.get("type", "note")
    tags = res.get("tags") or []
    tag_str = f", tags={','.join(tags)}" if tags else ""
    persist = res.get("persist")
    persist_str = f", persist={persist}" if persist is not None else ""
    return f"memory_store(id={memory_id}, type={mem_type}{tag_str}{persist_str})"


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.memory_store",
        description=(
            "Remember a fact or preference for later. Stamps this conversation "
            "and indexes long-term memory so hybrid recall can find it. Use when "
            "the user says remember / don't forget. Chat already injects relevant "
            "memories each turn — do not store routine conversation. "
            "core.note is a no-op comment and does not persist."
        ),
        func=run,
        tool_schema=Params,
        priority=6,
        observation_formatter=_fmt,
        category="memory",
        keywords=["remember", "memory", "store", "save", "preference"],
    )


__all__ = ["register"]
