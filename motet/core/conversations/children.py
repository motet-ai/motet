"""
Motet - Child Conversation Lifecycle

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-31

Description:
    The lifecycle for a child conversation created by a fan-out: mint an
    isolated ``iso-…`` id with parent/root pointers, claim and register it so
    the conversation list and GET history can open it, write the instruction
    as the child's first user message (the "brief") before the child runs,
    persist the child's reply on the child conversation afterwards, and build
    the card pointer the parent turn keeps in place of nested rows. Spawn
    children stay listed under the parent chat agent and record
    ``turn_agent_id`` plus a spawn tool-cage contract so follow-up runs as
    ``core.subagent``. The pointer also carries the child's thinking and tool
    summaries so the parent chat can restore the right-rail panels after
    reload. Conversation GET fills those fields from the child transcript
    when the stored pointer omitted them.

    This is conversation-domain logic, deliberately separate from the
    reasoning loop: callers bracket their child ``agent_loop`` (or any other
    command) with ``create_child_conversation`` before dispatch and
    ``complete_child_conversation`` after fan-in. ``core.spawn_agents`` is the
    first caller; workflow steps that isolate a conversation can reuse the
    same helpers to become visible, openable conversations.

Dependencies:
    - motet.core.conversations.lineage: opaque isolated ids and parent/root
      pointer recording
    - motet.core.conversations.ownership: claim the child for the acting
      principal so GET history authorizes
    - motet.core.conversations.registry: sidebar list rows with parentage
    - motet.core.conversations.transcript_storage: brief and reply rows on the
      child conversation (explicit ``conversation_id`` override)

Usage:
    from motet.core.conversations.children import (
        create_child_conversation,
        complete_child_conversation,
        hydrate_spawn_children,
        parent_registry_scope,
    )

    registry_agent, surface_id = parent_registry_scope(motet, parent_agent_id)
    child = create_child_conversation(
        motet,
        instruction=task.instruction,
        registry_agent_id=registry_agent,
        pointer_agent_id=f"{parent_agent_id}.spawn-1",
        surface_id=surface_id,
        kind="spawn",
        turn_agent_id="core.subagent",
        spawn_contract={"discover": False, "tools": ["core.web_search"]},
    )
    # ... run the child agent_loop on child.conversation_id ...
    pointer = complete_child_conversation(
        motet,
        child_cid=child.conversation_id,
        reply_text=reply,
        instruction=task.instruction,
        registry_agent_id=registry_agent,
        pointer_agent_id=child.pointer_agent_id,
        surface_id=surface_id,
        brief_written=child.brief_written,
        cost_usd=cost,
    )

Notes:
    - Registration and the brief are fail-soft: a Redis hiccup downgrades the
      child to "invisible until completed" rather than failing the fan-out.
      ``complete_child_conversation`` returns ``None`` on failure so callers
      can decide whether to synthesize a pointer some other way.
    - The brief is written *before* the child runs so a live card click in
      Chat Explorer shows the instruction while the child is still working.
    - Transcript writes pass an explicit ``conversation_id``; the memory
      manager files the row under that id even though the caller's command
      context still points at the parent conversation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ChildConversation:
    """One minted child conversation: ids, registry scope, and brief state."""

    conversation_id: str
    parent_conversation_id: Optional[str]
    root_conversation_id: Optional[str]
    title: str
    registry_agent_id: str
    pointer_agent_id: str
    turn_agent_id: str
    surface_id: Optional[str]
    brief_written: bool

    @property
    def pointer(self) -> Dict[str, Any]:
        """Early parent-turn card pointer (no preview or cost yet)."""
        return child_pointer(
            child_cid=self.conversation_id,
            agent_id=self.pointer_agent_id,
            title=self.title,
            preview="",
            cost_usd=None,
            turn_agent_id=self.turn_agent_id,
        )


def parent_registry_scope(motet: Any, fallback_agent_id: str) -> tuple[str, Optional[str]]:
    """Parent chat agent and surface for child registry rows."""
    meta = getattr(motet, "metadata", None)
    meta = meta if isinstance(meta, dict) else {}
    agent_id = str(
        meta.get("configured_agent_qualified_id")
        or meta.get("configured_agent_id")
        or fallback_agent_id
        or ""
    ).strip() or "core.default"
    surface_raw = meta.get("surface_id")
    surface_id = str(surface_raw).strip() if isinstance(surface_raw, str) and surface_raw.strip() else None
    return agent_id, surface_id


def _memory_metadata(mem: Any) -> Dict[str, Any]:
    """Transcript-row metadata from a MemoryItem or a serialized dict."""
    if isinstance(mem, dict):
        md = mem.get("metadata")
    else:
        md = getattr(mem, "metadata", None)
    if md is None:
        return {}
    dump = getattr(md, "model_dump", None)
    if callable(dump):
        try:
            md = dump()
        except Exception:
            return {}
    return md if isinstance(md, dict) else {}


def _fields_from_metadata(md: Dict[str, Any]) -> Dict[str, Any]:
    """thinking / tool summaries / cost from one stored transcript row."""
    from motet.core.conversations.transcript_storage import (
        coerce_cost_usd,
        coerce_tool_summaries,
    )

    fields: Dict[str, Any] = {}
    thinking = str(md.get("thinking_text") or "").strip()
    if thinking:
        fields["thinking_text"] = thinking
    if "tool_summaries" in md:
        summaries = coerce_tool_summaries(md.get("tool_summaries"))
        if summaries:
            fields["tool_summaries"] = summaries
    cost = coerce_cost_usd(md.get("cost_usd"))
    if cost is not None:
        fields["cost_usd"] = cost
    return fields


def _merge_display_fields(target: Dict[str, Any], incoming: Dict[str, Any]) -> None:
    """Keep the first filled thinking / summaries / cost (newest row wins)."""
    if incoming.get("thinking_text") and not target.get("thinking_text"):
        target["thinking_text"] = incoming["thinking_text"]
    if incoming.get("tool_summaries") and not target.get("tool_summaries"):
        target["tool_summaries"] = incoming["tool_summaries"]
    if incoming.get("cost_usd") is not None and target.get("cost_usd") is None:
        target["cost_usd"] = incoming["cost_usd"]


def _assistant_display_fields(motet: Any, child_cid: str) -> Dict[str, Any]:
    """thinking / tool summaries / cost from the child's stored transcript row."""
    cid = (child_cid or "").strip()
    recall = getattr(getattr(motet, "memory", None), "recall_conversation", None)
    if not cid or not callable(recall):
        return {}
    try:
        try:
            memories = recall(
                conversation_id=cid,
                types=["conversation_transcript"],
                limit=20,
                motet_context=motet,
            )
        except TypeError:
            memories = recall(
                conversation_id=cid,
                types=["conversation_transcript"],
                limit=20,
            )
    except Exception as exc:
        logger.warning(
            "spawn_child_display_load_failed",
            conversation_id=cid,
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        memories = []

    fields: Dict[str, Any] = {}
    for mem in memories or []:
        _merge_display_fields(fields, _fields_from_metadata(_memory_metadata(mem)))
        if fields.get("thinking_text") and fields.get("tool_summaries"):
            break
    if fields.get("thinking_text"):
        return fields

    from motet.core.conversations.conversation_state import load_history
    from motet.core.conversations.transcript_replay import message_to_history_item

    try:
        rows = load_history(motet, cid, limit=50)
    except Exception as exc:
        logger.warning(
            "spawn_child_display_replay_failed",
            conversation_id=cid,
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return fields
    for _created_at, msg in reversed(rows):
        if getattr(msg, "role", None) != "assistant":
            continue
        item = message_to_history_item(msg, "")
        if not item:
            continue
        _merge_display_fields(
            fields,
            {
                key: item[key]
                for key in ("thinking_text", "tool_summaries", "cost_usd")
                if key in item
            },
        )
        if fields.get("thinking_text") and fields.get("tool_summaries"):
            break
    return fields


def hydrate_spawn_children(motet: Any, cards: Any) -> List[Dict[str, Any]]:
    """Fill thin parent-turn cards from each child's stored first-turn display.

    Parent persist can omit thinking and tool summaries (early mint pointer,
    or a join result that lacked those keys). The child transcript still has
    them. Conversation GET uses this so reload matches the live parent rail.
    """
    from motet.core.conversations.transcript_storage import coerce_spawn_children

    rows = coerce_spawn_children(cards)
    if not rows:
        return []
    out: List[Dict[str, Any]] = []
    for row in rows:
        need_thinking = not str(row.get("thinking_text") or "").strip()
        need_summaries = not row.get("tool_summaries")
        need_cost = row.get("cost_usd") is None
        if not (need_thinking or need_summaries or need_cost):
            out.append(row)
            continue
        display = _assistant_display_fields(
            motet, str(row.get("child_conversation_id") or "")
        )
        merged = dict(row)
        if need_thinking and display.get("thinking_text"):
            merged["thinking_text"] = display["thinking_text"]
        if need_summaries and display.get("tool_summaries"):
            merged["tool_summaries"] = display["tool_summaries"]
        if need_cost and display.get("cost_usd") is not None:
            merged["cost_usd"] = display["cost_usd"]
        out.append(merged)
    return out


def spawn_contract_for_followup(
    row: Optional[Dict[str, Any]],
    qualified_id: str,
) -> Optional[Dict[str, Any]]:
    """Return the stored spawn cage when this turn is the child's turn agent."""
    if not isinstance(row, dict):
        return None
    contract = row.get("spawn_contract")
    if not isinstance(contract, dict):
        return None
    from motet.core.agents import CORE_SUBAGENT_ID

    turn_id = str(row.get("turn_agent_id") or "").strip() or CORE_SUBAGENT_ID
    if str(qualified_id or "").strip() != turn_id:
        return None
    return contract


def child_pointer(
    *,
    child_cid: str,
    agent_id: str,
    title: str,
    preview: str,
    cost_usd: Any,
    thinking_text: Optional[str] = None,
    tool_summaries: Optional[Any] = None,
    turn_agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Parent-turn card pointer for one child conversation.

    Preview and cost are for the card. Thinking and tool summaries restore
    the parent right-rail panels after reload. All of it is display-only.
    """
    from motet.core.conversations.transcript_storage import (
        coerce_cost_usd,
        coerce_tool_summaries,
        conversation_title_from_text,
    )

    pointer: Dict[str, Any] = {
        "child_conversation_id": child_cid,
        "agent_id": agent_id,
        "title": conversation_title_from_text(title),
    }
    turn_id = (turn_agent_id or "").strip()
    if turn_id:
        pointer["turn_agent_id"] = turn_id
    preview_text = " ".join((preview or "").split())
    if preview_text:
        pointer["preview"] = preview_text
    cost = coerce_cost_usd(cost_usd)
    if cost is not None:
        pointer["cost_usd"] = cost
    thinking = (thinking_text or "").strip()
    if thinking:
        pointer["thinking_text"] = thinking
    summaries = coerce_tool_summaries(tool_summaries)
    if summaries:
        pointer["tool_summaries"] = summaries
    return pointer


def register_child_conversation(
    motet: Any,
    *,
    child_cid: str,
    title: str,
    agent_id: str,
    surface_id: Optional[str],
    turn_agent_id: Optional[str] = None,
    spawn_contract: Optional[Dict[str, Any]] = None,
) -> bool:
    """Claim and register a child so GET history and the list can open it."""
    motet_id = str(getattr(motet, "motet_id", None) or "").strip()
    tenant_id = str(getattr(motet, "tenant_id", None) or "").strip()
    principal_id = str(getattr(motet, "principal_id", None) or "").strip()
    if not (motet_id and tenant_id and principal_id):
        return False
    from motet.core.conversations.ownership import authorize_conversation_access_sync
    from motet.core.conversations.registry import register_or_touch_conversation_sync
    from motet.core.conversations.transcript_storage import conversation_title_from_text

    try:
        authorize_conversation_access_sync(
            motet_id=motet_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            conversation_id=child_cid,
            bind_if_unclaimed=True,
        )
        register_or_touch_conversation_sync(
            motet_id,
            tenant_id,
            principal_id,
            child_cid,
            title=conversation_title_from_text(title),
            agent_id=agent_id,
            surface_id=surface_id,
            parent_conversation_id=str(getattr(motet, "conversation_id", None) or "").strip() or None,
            root_conversation_id=(
                str((getattr(motet, "metadata", None) or {}).get("root_conversation_id") or "").strip()
                or str(getattr(motet, "conversation_id", None) or "").strip()
                or None
            ),
            turn_agent_id=turn_agent_id,
            spawn_contract=spawn_contract,
        )
        return True
    except Exception as exc:
        logger.warning(
            "child_conversation_register_failed",
            conversation_id=child_cid,
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return False


def persist_child_brief(
    motet: Any,
    *,
    child_cid: str,
    instruction: str,
    agent_id: str,
) -> bool:
    """Write the instruction as the child's first user message. Empty assistant."""
    text = (instruction or "").strip()
    if not text or not getattr(motet, "memory", None):
        return False
    from motet.core.conversations.transcript_storage import store_turn_transcript
    from motet.core.types import Message

    try:
        store_turn_transcript(
            motet,
            [Message(role="user", content=text)],
            "",
            conversation_id=child_cid,
            agent_id=agent_id,
            root_turn=True,
            include_tool_invocations=False,
        )
        return True
    except Exception as exc:
        logger.warning(
            "child_conversation_brief_failed",
            conversation_id=child_cid,
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return False


def create_child_conversation(
    motet: Any,
    *,
    instruction: str,
    registry_agent_id: str,
    pointer_agent_id: str,
    surface_id: Optional[str] = None,
    kind: str = "spawn",
    turn_agent_id: Optional[str] = None,
    spawn_contract: Optional[Dict[str, Any]] = None,
) -> ChildConversation:
    """Mint, claim, register, and brief one child conversation before it runs.

    Requires a non-empty ``motet.conversation_id`` (the parent). Registration
    and the brief are fail-soft; the returned ``ChildConversation`` always
    carries a usable isolated id.
    """
    from motet.core.conversations.lineage import mint_isolated_conversation

    parent_cid = str(getattr(motet, "conversation_id", None) or "").strip()
    meta = getattr(motet, "metadata", None)
    meta = meta if isinstance(meta, dict) else {}
    root_hint = str(meta.get("root_conversation_id") or "").strip() or None
    iso = mint_isolated_conversation(
        parent_cid,
        tenant_id=str(getattr(motet, "tenant_id", None) or "").strip() or None,
        kind=kind,
        root_conversation_id=root_hint,
    )
    from motet.core.agents import CORE_SUBAGENT_ID

    resolved_turn = (turn_agent_id or "").strip() or (
        CORE_SUBAGENT_ID if kind == "spawn" else ""
    )
    register_child_conversation(
        motet,
        child_cid=iso.conversation_id,
        title=instruction,
        agent_id=registry_agent_id,
        surface_id=surface_id,
        turn_agent_id=resolved_turn or None,
        spawn_contract=spawn_contract,
    )
    brief_written = persist_child_brief(
        motet,
        child_cid=iso.conversation_id,
        instruction=instruction,
        agent_id=registry_agent_id,
    )
    return ChildConversation(
        conversation_id=iso.conversation_id,
        parent_conversation_id=iso.parent_conversation_id,
        root_conversation_id=iso.root_conversation_id,
        title=instruction,
        registry_agent_id=registry_agent_id,
        pointer_agent_id=pointer_agent_id,
        turn_agent_id=resolved_turn,
        surface_id=surface_id,
        brief_written=brief_written,
    )


def complete_child_conversation(
    motet: Any,
    *,
    child_cid: str,
    reply_text: str,
    instruction: str = "",
    registry_agent_id: str,
    pointer_agent_id: str,
    surface_id: Optional[str] = None,
    brief_written: bool = False,
    thinking_text: Optional[str] = None,
    tool_summaries: Optional[Any] = None,
    cost_usd: Optional[Any] = None,
    include_tool_invocations: bool = True,
    turn_agent_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Persist the child's reply on its conversation and return the card pointer.

    When the brief was not written earlier, the instruction is included inline
    as the child's first user message. Returns ``None`` (after logging) when
    the transcript write fails, so a fan-out can degrade to pointer-only.
    """
    from motet.core.conversations.transcript_storage import store_turn_transcript
    from motet.core.types import Message

    text = (reply_text or "").strip()
    if not text or not getattr(motet, "memory", None):
        return None
    from motet.core.agents import CORE_SUBAGENT_ID

    brief = (instruction or "").strip() or text
    resolved_turn = (turn_agent_id or "").strip() or CORE_SUBAGENT_ID
    try:
        store_turn_transcript(
            motet,
            [] if brief_written else [Message(role="user", content=brief)],
            text,
            conversation_id=child_cid,
            agent_id=resolved_turn,
            root_turn=not brief_written,
            include_tool_invocations=include_tool_invocations,
            thinking_text=(thinking_text or "").strip() or None,
            tool_summaries=tool_summaries or None,
            cost_usd=cost_usd,
        )
        register_child_conversation(
            motet,
            child_cid=child_cid,
            title=brief,
            agent_id=registry_agent_id,
            surface_id=surface_id,
            turn_agent_id=resolved_turn,
        )
        return child_pointer(
            child_cid=child_cid,
            agent_id=pointer_agent_id,
            title=brief,
            preview=text,
            cost_usd=cost_usd,
            thinking_text=thinking_text,
            tool_summaries=tool_summaries,
            turn_agent_id=resolved_turn,
        )
    except Exception as exc:
        logger.warning(
            "child_conversation_reply_failed",
            agent_id=pointer_agent_id,
            conversation_id=child_cid,
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return None
