"""
Motet - Canonical Transcript Storage (impl-070)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-30

Description:
    Persist one turn's canonical transcript to conversation-scoped memory.
    Used by finalize_turn, append_turn, and spawn_agents child first turns.
    Single place for: recall this agent's tool invocations, build transcript
    items, serialize, store. ``tool_summaries`` is always written (empty when
    this agent ran no tools) so conversation GET does not invent rail steps.

    Turn ordering is deterministic via a per-conversation monotonic ``sequence``
    reserved at turn start and passed into finalize_turn. Storage writes a single
    completed ``conversation_transcript`` row for the turn. Optional
    ``conversation_id`` overrides ``motet.conversation_id`` so a spawn child
    turn can be written on its isolated conversation. Optional ``spawn_children``
    is display-only card-pointer metadata on the parent row when the caller
    passes it.

Dependencies:
    - motet.core.types: MemoryScopeType
    - motet.core.tools.transcript_service: parse_and_dedupe_tool_invocation_memories
    - motet.core.conversations.transcript_codec: build_transcript_items_for_turn, serialize_transcript_items

Usage:
    from motet.core.conversations.transcript_storage import store_turn_transcript

    result = store_turn_transcript(motet, messages, assistant_response)
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, List, Optional
from uuid import uuid4

import structlog

from ..types import MemoryScopeType, Message

logger = structlog.get_logger(__name__)

_TRANSCRIPT_SEQ_TTL_SECONDS = 30 * 24 * 3600  # 30 days


_TOOL_SUMMARY_PREVIEW_MAX = 160
_AGENT_TAG_PREFIX = "agent:"


def _display_thinking_text(value: Any) -> Optional[str]:
    """Non-empty reasoning string for row metadata, or None."""
    text = str(value or "").strip()
    return text or None


def coerce_cost_usd(value: Any) -> Optional[float]:
    """Priced USD amount for display, or None when unknown (not free)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        amount = float(value)
        if amount > 0:
            return amount
    return None


def coerce_tool_summaries(value: Any) -> List[Dict[str, Any]]:
    """Sanitize display tool summaries. Empty input → []."""
    return list(_display_tool_summaries(value) or [])


_SPAWN_PREVIEW_MAX = 160
_SPAWN_TITLE_MAX = 80


def conversation_title_from_text(text: str, *, max_len: int = _SPAWN_TITLE_MAX) -> str:
    """One-line conversation title from a spawn brief or first user message."""
    one_line = " ".join((text or "").split())
    if not one_line:
        return "New Chat"
    if len(one_line) > max_len:
        return one_line[:max_len].rstrip() + "…"
    return one_line


def coerce_spawn_children(value: Any) -> List[Dict[str, Any]]:
    """Sanitize parent-turn spawn card pointers. Empty input → []."""
    return list(_display_spawn_children(value) or [])


def overlay_spawn_child_pointer(
    existing: Dict[str, Any], incoming: Dict[str, Any]
) -> Dict[str, Any]:
    """Later filled fields win so a completed card replaces an early mint pointer."""
    merged = dict(existing)
    for key, value in incoming.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, list) and not value:
            continue
        merged[key] = value
    return merged


def _display_spawn_children(value: Any) -> Optional[List[Dict[str, Any]]]:
    """Card pointers for spawn children on a parent transcript row, or None."""
    if not isinstance(value, list) or not value:
        return None
    out: List[Dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        child_cid = str(raw.get("child_conversation_id") or "").strip()
        agent_id = str(raw.get("agent_id") or "").strip()
        title = conversation_title_from_text(str(raw.get("title") or ""))
        if not child_cid:
            continue
        row: Dict[str, Any] = {
            "child_conversation_id": child_cid,
            "title": title,
        }
        if agent_id:
            row["agent_id"] = agent_id
        preview = raw.get("preview")
        if isinstance(preview, str) and preview.strip():
            row["preview"] = preview.strip()[:_SPAWN_PREVIEW_MAX]
        cost = coerce_cost_usd(raw.get("cost_usd"))
        if cost is not None:
            row["cost_usd"] = cost
        thinking = _display_thinking_text(raw.get("thinking_text"))
        if thinking:
            row["thinking_text"] = thinking
        summaries = _display_tool_summaries(raw.get("tool_summaries"))
        if summaries:
            row["tool_summaries"] = summaries
        out.append(row)
    return out or None


def _display_tool_summaries(value: Any) -> Optional[List[Dict[str, Any]]]:
    """Name/status/preview rows for conversation reload, or None."""
    if not isinstance(value, list) or not value:
        return None
    out: List[Dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("tool_name") or "").strip()
        if not name:
            continue
        status = str(raw.get("status") or "success").strip() or "success"
        row: Dict[str, Any] = {"tool_name": name, "status": status}
        preview = raw.get("preview")
        if isinstance(preview, str) and preview.strip():
            row["preview"] = preview.strip()[:_TOOL_SUMMARY_PREVIEW_MAX]
        step = _summary_step(raw.get("step"))
        if step is not None:
            row["step"] = step
        duration_ms = _summary_duration_ms(raw.get("duration_ms"))
        if duration_ms is not None:
            row["duration_ms"] = duration_ms
        out.append(row)
    return out or None


def _summary_step(value: Any) -> Optional[int]:
    """Positive loop iteration for sidebar Step N, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


def _summary_duration_ms(value: Any) -> Optional[int]:
    """Non-negative tool wall time in milliseconds, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None
_TRANSCRIPT_COMMIT_PREFIX = "conversation:"
_TRANSCRIPT_COMMIT_LOCK_SECONDS = 30


def _transcript_redis_key(logical_key: str, tenant_id: Optional[str]) -> str:
    from ..distributed.tenant_keys import tenant_key

    tid = (tenant_id or "").strip()
    return tenant_key(tid, logical_key) if tid else logical_key


def allocate_transcript_sequence(
    conversation_id: str,
    redis_client: Any,
    tenant_id: Optional[str] = None,
) -> int:
    """Reserve the next monotonic sequence number for a conversation transcript.

    Uses Redis INCR for atomicity. TTL is set on the key to prevent unbounded
    accumulation across conversations. When tenant_id is set, the key is
    tenant-prefixed; an unprefixed legacy counter is renamed first so sequence
    numbers stay continuous (ADR-0095 Phase 2).
    """
    logical = f"conversation:{conversation_id}:transcript_seq"
    seq_key = _transcript_redis_key(logical, tenant_id)
    if tenant_id and seq_key != logical:
        try:
            if not redis_client.exists(seq_key) and redis_client.exists(logical):
                redis_client.rename(logical, seq_key)
        except Exception:
            pass
    seq = int(redis_client.incr(seq_key))
    if seq == 1:
        redis_client.expire(seq_key, _TRANSCRIPT_SEQ_TTL_SECONDS)
    return seq


def _memory_item_agent_id(memory: Any) -> str:
    """Qualified agent id stamped on a tool_invocation memory, or empty."""
    meta = getattr(memory, "metadata", None) or {}
    if isinstance(meta, dict):
        raw = str(meta.get("agent_id") or "").strip()
        if raw:
            return raw
    for tag in getattr(memory, "tags", None) or []:
        text = str(tag or "").strip()
        if text.startswith(_AGENT_TAG_PREFIX) and text[len(_AGENT_TAG_PREFIX) :]:
            return text[len(_AGENT_TAG_PREFIX) :]
    return ""


def _tool_invocations_for_agent(memories: Any, agent_id: Optional[str]) -> List[Any]:
    """Keep invocations that belong to this transcript's authoring agent.

    In-thread children share the parent conversation and task. Without this
    filter, a no-tool panelist row would ingest the parent's tool_invocation
    memories. When ``agent_id`` is missing, keep the unfiltered list.
    """
    rows = list(memories or [])
    author = str(agent_id or "").strip()
    if not author:
        return rows
    return [row for row in rows if _memory_item_agent_id(row) == author]


def resolve_transcript_agent_id(
    motet: Any,
    *,
    explicit: Optional[str] = None,
) -> Optional[str]:
    """
    Qualified registry id for the agent that produced this transcript turn (ADR-0083).

    Uses explicit value when provided; otherwise ``motet.metadata`` keys
    ``agent_id`` / ``configured_agent_id`` / ``configured_agent_qualified_id``.
    """
    from ..agents import resolve_agent_id

    raw = (explicit or "").strip() if explicit else ""
    if not raw and motet:
        meta = getattr(motet, "metadata", None)
        if isinstance(meta, dict):
            for key in ("agent_id", "configured_agent_id", "configured_agent_qualified_id"):
                v = meta.get(key)
                if v and str(v).strip():
                    raw = str(v).strip()
                    break
    if not raw:
        return None
    try:
        return resolve_agent_id(raw)
    except Exception:
        return raw


def store_turn_transcript(
    motet: Any,
    messages: List[Any],
    assistant_response: str,
    *,
    agent_id: Optional[str] = None,
    root_turn: Optional[bool] = None,
    root_agent_id: Optional[str] = None,
    parent_agent_id: Optional[str] = None,
    transcript_sequence: Optional[int] = None,
    pending_action_carry: Optional[Dict[str, Any]] = None,
    include_tool_invocations: bool = True,
    thinking_text: Optional[str] = None,
    tool_summaries: Optional[List[Dict[str, Any]]] = None,
    cost_usd: Optional[float] = None,
    conversation_id: Optional[str] = None,
    spawn_children: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Persist this turn as one conversation_transcript memory (impl-070).

    Recalls tool invocations for this task, builds canonical TranscriptItems,
    serializes, and stores. Caller must ensure motet.memory has store
    and recall_conversation. ``conversation_id`` overrides ``motet.conversation_id``
    when writing an isolated spawn-child turn. ``spawn_children`` is stored
    as display-only card pointers when the caller passes them.

    ADR-0121 Phase 1 writer: when the assistant response ends with a question
    (tail-question heuristic), a heuristic ``pending_action`` marker carrying
    the question text and this turn's tool shortlist is attached to the root
    assistant Message. Otherwise, when the caller passes
    ``pending_action_carry`` (an unconsumed deferred proposal, already
    incremented and cap-checked by the reader), that marker is re-attached so
    a deferral does not silently bury the pending proposal. Only root turns
    write markers.

    Returns:
        Dict with canonical_transcript_stored, conversation_stored, items_stored (1),
        or canonical_transcript_error on inner failure.
    """
    result: Dict[str, Any] = {
        "canonical_transcript_stored": False,
        "conversation_stored": False,
        "items_stored": 0,
    }
    if not getattr(motet, "memory", None) or not hasattr(motet.memory, "store"):
        return result
    if not hasattr(motet.memory, "recall_conversation"):
        return result

    try:
        from ..tools.transcript_service import parse_and_dedupe_tool_invocation_memories
        from .transcript_codec import build_transcript_items_for_turn, serialize_transcript_items

        from ..memory.constants import CONVERSATION_SCOPE_TAG_PREFIX
        cid = str(conversation_id or getattr(motet, "conversation_id", None) or "").strip() or None
        conversation_tags = [f"{CONVERSATION_SCOPE_TAG_PREFIX}{cid}"] if cid else []
        resolved_author = resolve_transcript_agent_id(motet, explicit=agent_id)
        invs: List[Any] = []
        if include_tool_invocations:
            tool_memories = _tool_invocations_for_agent(
                motet.memory.recall_conversation(
                    conversation_id=cid,
                    types=["tool_invocation"],
                    limit=500,
                ),
                resolved_author,
            )
            invs = parse_and_dedupe_tool_invocation_memories(
                tool_memories,
                task_id=motet.task_id,
                log_started_only=True,
                log_context={
                    "conversation_id": cid,
                    "task_id": motet.task_id,
                    "agent_id": resolved_author,
                },
            )

        # Persist turn-delta messages explicitly, avoiding cross-turn counters:
        # - first stored turn: include system message(s) + current user message
        # - subsequent turns: include only current user message
        has_existing_transcript = False
        if cid:
            prev_transcripts = motet.memory.recall_conversation(
                conversation_id=cid,
                types=["conversation_transcript"],
                limit=20,
            )
            has_existing_transcript = any(
                isinstance(getattr(t, "metadata", None), dict)
                and bool((getattr(t, "metadata", {}) or {}).get("items"))
                for t in (prev_transcripts or [])
            )

        normalized_messages: List[Message] = []
        for raw_msg in messages or []:
            if isinstance(raw_msg, Message):
                normalized_messages.append(raw_msg)
                continue
            if isinstance(raw_msg, dict):
                try:
                    normalized_messages.append(Message.model_validate(raw_msg))
                except Exception:
                    continue  # skip malformed message during normalization

        current_user: Message | None = None
        for msg in reversed(normalized_messages):
            if msg.role == "user":
                current_user = msg
                break

        turn_messages: List[Message] = []
        if not has_existing_transcript:
            turn_messages.extend([m for m in normalized_messages if m.role == "system"])
        include_current_user = True
        if root_turn is not None:
            include_current_user = bool(root_turn)
        if current_user is not None and include_current_user:
            turn_messages.append(current_user)

        # ADR-0121 Phase 1 writer: heuristic marker when this turn asks a
        # tail question; otherwise re-attach an unconsumed deferred proposal
        # passed by the reader. A fresh proposal always wins over carry.
        pending_marker: Optional[Dict[str, Any]] = None
        if root_turn is not False:
            from .pending_action import build_heuristic_marker

            tool_shortlist = [inv.tool_name for inv in invs if getattr(inv, "tool_name", None)]
            pending_marker = build_heuristic_marker(assistant_response, tool_shortlist)
            if pending_marker is not None:
                logger.info(
                    "pending_action_written",
                    conversation_id=cid,
                    task_id=motet.task_id,
                    marker_id=pending_marker.get("marker_id"),
                    source=pending_marker.get("source"),
                    tool_shortlist_size=len(pending_marker.get("tool_shortlist") or []),
                )
            elif isinstance(pending_action_carry, dict) and pending_action_carry:
                pending_marker = dict(pending_action_carry)
                logger.info(
                    "pending_action_carried_forward",
                    conversation_id=cid,
                    task_id=motet.task_id,
                    marker_id=pending_marker.get("marker_id"),
                    carried_forward=pending_marker.get("carried_forward"),
                )

        items = build_transcript_items_for_turn(
            turn_messages,
            invs,
            assistant_response,
            agent_id=resolved_author,
            pending_action=pending_marker,
        )
        transcript_payload = serialize_transcript_items(items)
        display_thinking = _display_thinking_text(thinking_text)
        display_summaries = _display_tool_summaries(tool_summaries) or []
        display_cost = coerce_cost_usd(cost_usd)
        display_spawn_children = _display_spawn_children(spawn_children)

        if transcript_sequence is not None:
            seq = int(transcript_sequence)
        else:
            assert motet.redis is not None, "Redis is required for transcript sequence allocation"
            seq = allocate_transcript_sequence(
                cid,
                motet.redis,
                tenant_id=getattr(motet, "tenant_id", None),
            )

        redis_client = getattr(motet, "redis", None)
        commit_logical = f"{_TRANSCRIPT_COMMIT_PREFIX}{cid}:transcript_commit:{seq}"
        commit_key = _transcript_redis_key(commit_logical, getattr(motet, "tenant_id", None))
        lock_key = f"{commit_key}:lock"
        hash_body: Dict[str, Any] = {
            "items": transcript_payload,
            "tool_summaries": display_summaries,
        }
        if display_thinking:
            hash_body["thinking_text"] = display_thinking
        if display_cost is not None:
            hash_body["cost_usd"] = display_cost
        if display_spawn_children:
            hash_body["spawn_children"] = display_spawn_children
        payload_hash = hashlib.sha256(
            json.dumps(hash_body, sort_keys=True, ensure_ascii=True).encode("utf-8")
        ).hexdigest()

        if redis_client is not None and cid:
            existing_raw = redis_client.get(commit_key)
            if not existing_raw and commit_key != commit_logical:
                existing_raw = redis_client.get(commit_logical)
            if existing_raw:
                existing_text = (
                    existing_raw.decode("utf-8", errors="replace")
                    if isinstance(existing_raw, (bytes, bytearray))
                    else str(existing_raw)
                )
                if existing_text == payload_hash:
                    logger.info(
                        "transcript_sequence_duplicate_replay",
                        conversation_id=cid,
                        sequence=seq,
                        task_id=motet.task_id,
                    )
                    result["canonical_transcript_stored"] = True
                    result["conversation_stored"] = True
                    result["sequence"] = seq
                    result["duplicate_replay"] = True
                    return result
                logger.warning(
                    "transcript_sequence_conflict",
                    conversation_id=cid,
                    sequence=seq,
                    task_id=motet.task_id,
                    existing_hash=existing_text,
                    incoming_hash=payload_hash,
                )
                result["canonical_transcript_error"] = f"transcript sequence conflict at {seq}"
                result["sequence"] = seq
                result["sequence_conflict"] = True
                return result

            lock_token = str(uuid4())
            lock_acquired = bool(
                redis_client.set(lock_key, lock_token, nx=True, ex=_TRANSCRIPT_COMMIT_LOCK_SECONDS)
            )
            if not lock_acquired:
                logger.warning(
                    "transcript_sequence_lock_contention",
                    conversation_id=cid,
                    sequence=seq,
                    task_id=motet.task_id,
                )
                result["canonical_transcript_error"] = f"transcript sequence lock contention at {seq}"
                result["sequence"] = seq
                result["sequence_conflict"] = True
                return result

        meta_body: Dict[str, Any] = {
            "schema_version": "1.0",
            "task_id": motet.task_id,
            "conversation_id": cid,
            "timestamp": time.time(),
            "sequence": seq,
            "status": "completed",
            "items": transcript_payload,
        }
        # Issue #139: attribute the writing principal for audit (ownership is
        # enforced separately in conversations.ownership).
        principal_id = str(getattr(motet, "principal_id", "") or "").strip()
        if principal_id:
            meta_body["principal_id"] = principal_id
        if root_turn is not None:
            meta_body["root_turn"] = bool(root_turn)
        if root_agent_id:
            meta_body["root_agent_id"] = str(root_agent_id).strip()
        if parent_agent_id:
            meta_body["parent_agent_id"] = str(parent_agent_id).strip()
        if resolved_author:
            meta_body["agent_id"] = resolved_author
        if display_thinking:
            meta_body["thinking_text"] = display_thinking
        meta_body["tool_summaries"] = display_summaries
        if display_cost is not None:
            meta_body["cost_usd"] = display_cost
        if display_spawn_children:
            meta_body["spawn_children"] = display_spawn_children
        try:
            motet.memory.store(
                content="Canonical transcript for replay.",
                type="conversation_transcript",
                tags=conversation_tags,
                metadata=meta_body,
                item_id=str(uuid4()),
                working=False,
                scope=MemoryScopeType.CONVERSATION,
            )
            result["canonical_transcript_stored"] = True
            result["conversation_stored"] = True
            result["items_stored"] = 1
            result["sequence"] = seq
            if redis_client is not None and cid:
                redis_client.set(commit_key, payload_hash, ex=_TRANSCRIPT_SEQ_TTL_SECONDS)
        finally:
            if redis_client is not None and cid:
                try:
                    redis_client.delete(lock_key)
                except Exception:
                    pass
    except Exception as e:
        # Fail-closed: transcript storage failure must never break the turn for the user.
        # Log so failures are visible, but do not re-raise — finalize_turn continues regardless.
        logger.warning(
            "store_turn_transcript_failed",
            conversation_id=cid if "cid" in locals() else getattr(motet, "conversation_id", None),
            task_id=getattr(motet, "task_id", None),
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        result["canonical_transcript_error"] = str(e)
    return result


__all__ = [
    "allocate_transcript_sequence",
    "coerce_cost_usd",
    "coerce_spawn_children",
    "overlay_spawn_child_pointer",
    "coerce_tool_summaries",
    "conversation_title_from_text",
    "store_turn_transcript",
    "resolve_transcript_agent_id",
]
