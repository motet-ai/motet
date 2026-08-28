"""
Motet - Canonical Transcript Storage (impl-070)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Persist one turn's canonical transcript to conversation-scoped memory.
    Used by finalize_turn, append_turn, and spawn_agents child replies.
    Single place for: recall tool invocations, build transcript items, serialize, store.

    Turn ordering is deterministic via a per-conversation monotonic ``sequence``
    reserved at turn start and passed into finalize_turn. Storage writes a single
    completed ``conversation_transcript`` row for the turn.

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


def store_subagent_reply(
    motet: Any,
    assistant_response: str,
    *,
    agent_id: str,
    root_agent_id: str,
    parent_agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Persist one nested agent's write-up on the parent conversation.

    Reply text only (no tool-invocation rows, no user echo). ``root_turn=False``
    so replay places the row before the conversation-primary assistant.
    Thinking is not stored. ``parent_agent_id`` is the immediate parent loop;
    it defaults to ``root_agent_id`` when omitted.
    """
    return store_turn_transcript(
        motet,
        [],
        assistant_response,
        agent_id=agent_id,
        root_turn=False,
        root_agent_id=root_agent_id,
        parent_agent_id=parent_agent_id or root_agent_id,
        include_tool_invocations=False,
    )


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
) -> Dict[str, Any]:
    """
    Persist this turn as one conversation_transcript memory (impl-070).

    Recalls tool invocations for this task, builds canonical TranscriptItems,
    serializes, and stores. Caller must ensure motet.memory has store
    and recall_conversation.

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
        conversation_tags = [f"{CONVERSATION_SCOPE_TAG_PREFIX}{motet.conversation_id}"] if motet.conversation_id else []
        invs: List[Any] = []
        if include_tool_invocations:
            tool_memories = motet.memory.recall_conversation(
                conversation_id=motet.conversation_id,
                types=["tool_invocation"],
                limit=500,
            )
            invs = parse_and_dedupe_tool_invocation_memories(
                tool_memories,
                task_id=motet.task_id,
                log_started_only=True,
                log_context={
                    "conversation_id": motet.conversation_id,
                    "task_id": motet.task_id,
                },
            )

        # Persist turn-delta messages explicitly, avoiding cross-turn counters:
        # - first stored turn: include system message(s) + current user message
        # - subsequent turns: include only current user message
        has_existing_transcript = False
        if motet.conversation_id:
            prev_transcripts = motet.memory.recall_conversation(
                conversation_id=motet.conversation_id,
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

        resolved_author = resolve_transcript_agent_id(motet, explicit=agent_id)

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
                    conversation_id=motet.conversation_id,
                    task_id=motet.task_id,
                    marker_id=pending_marker.get("marker_id"),
                    source=pending_marker.get("source"),
                    tool_shortlist_size=len(pending_marker.get("tool_shortlist") or []),
                )
            elif isinstance(pending_action_carry, dict) and pending_action_carry:
                pending_marker = dict(pending_action_carry)
                logger.info(
                    "pending_action_carried_forward",
                    conversation_id=motet.conversation_id,
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

        if transcript_sequence is not None:
            seq = int(transcript_sequence)
        else:
            assert motet.redis is not None, "Redis is required for transcript sequence allocation"
            seq = allocate_transcript_sequence(
                motet.conversation_id,
                motet.redis,
                tenant_id=getattr(motet, "tenant_id", None),
            )

        redis_client = getattr(motet, "redis", None)
        commit_logical = f"{_TRANSCRIPT_COMMIT_PREFIX}{motet.conversation_id}:transcript_commit:{seq}"
        commit_key = _transcript_redis_key(commit_logical, getattr(motet, "tenant_id", None))
        lock_key = f"{commit_key}:lock"
        payload_hash = hashlib.sha256(
            json.dumps(transcript_payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
        ).hexdigest()

        if redis_client is not None and motet.conversation_id:
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
                        conversation_id=motet.conversation_id,
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
                    conversation_id=motet.conversation_id,
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
                    conversation_id=motet.conversation_id,
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
            "conversation_id": motet.conversation_id,
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
            if redis_client is not None and motet.conversation_id:
                redis_client.set(commit_key, payload_hash, ex=_TRANSCRIPT_SEQ_TTL_SECONDS)
        finally:
            if redis_client is not None and motet.conversation_id:
                try:
                    redis_client.delete(lock_key)
                except Exception:
                    pass
    except Exception as e:
        # Fail-closed: transcript storage failure must never break the turn for the user.
        # Log so failures are visible, but do not re-raise — finalize_turn continues regardless.
        logger.warning(
            "store_turn_transcript_failed",
            conversation_id=getattr(motet, "conversation_id", None),
            task_id=getattr(motet, "task_id", None),
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        result["canonical_transcript_error"] = str(e)
    return result


__all__ = [
    "allocate_transcript_sequence",
    "store_subagent_reply",
    "store_turn_transcript",
    "resolve_transcript_agent_id",
]
