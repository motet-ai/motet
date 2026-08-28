"""
Motet - Canonical Transcript Replay (impl-070)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Helpers for replaying canonical conversation_transcript memories into
    Message lists: get_conversation_history_from_transcripts (used by prepare_context
    and conversation_get), merge_conversation_history (dedupe merge for prepare_context),
    and message_to_history_item (API history item shape for conversation_get).

    Before rendering, offloaded tool-call arguments are hydrated from
    ArtifactStore so provider replay receives unmodified valid JSON.

    Ordering: transcripts are sorted by metadata.sequence (atomic per-conversation
    counter from transcript_storage); Redis is required so sequences are always
    available. Rows without a sequence sort to position 0. User messages
    are deduplicated only when identical duplicates are adjacent, so parallel
    agent turns don't produce repeated bubbles while preserving legitimate
    same-content user turns across time.
    Ordering is resolved on the backend in one pass: within each user turn,
    non-root assistants are emitted before root assistants so replay matches the
    streamed panel flow (sub-agents first, final synthesis last). Assistant
    messages that carry tool_calls keep their immediately following tool-result
    messages glued during that reorder so providers never see orphan role=tool
    turns (Chat Completions / DeepSeek / Moonshot).

    Merge dedupe (issue #138): assistant keys include agent_id so multi-agent
    transcripts stay distinct. A missing agent_id on the *incoming* (current)
    side is treated as a wildcard so client-echoed turns that cannot round-trip
    provenance still collapse against the stored copy.

Dependencies:
    -.transcript_codec: deserialize_transcript_items
    -.transcript_rendering: render_transcript_items_to_messages
    - motet.memory: recall_conversation

Usage:
    from motet.core.conversations.transcript_replay import get_conversation_history_from_transcripts

    # Prepare context: messages only
    conversation_history = [msg for _, msg in get_conversation_history_from_transcripts(motet, conversation_id)]

    # API: messages with created_at for response
    for created_at, msg in get_conversation_history_from_transcripts(motet, conversation_id):
        hist.append({"content": msg.content, "role": msg.role, "created_at": format_ts(created_at)})
"""

from __future__ import annotations

import structlog
from typing import TYPE_CHECKING, Any, List, Tuple

logger = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from ..types import Message
else:
    Message = Any


def _message_has_tool_calls(msg: Any) -> bool:
    """True when an assistant message declares one or more tool_calls."""
    from motet.core.models.adapters.tool_call_codec import message_has_tool_calls

    return message_has_tool_calls(msg)


def _order_turn_messages(out: List[Tuple[Any, "Message", bool]]) -> List[Tuple[Any, "Message", bool]]:
    """Within each user turn, place root assistant entries after sub-agent entries.

    Backend canonical ordering contract for replay:
    ``user -> sub-agents -> root assistant``.
    A root assistant is identified by ``root_turn``/``root_agent_id`` metadata
    resolved in ``get_conversation_history_from_transcripts``.

    Assistant messages that carry ``tool_calls`` keep any immediately following
    ``role=tool`` messages attached while reordering. Sequence reservation can
    place a root synthesis transcript before later sub-agent tool transcripts;
    without gluing, reorder would emit ``assistant(tool_calls)`` then the root
    text assistant and leave the tool results stranded (provider 400).
    """
    if not out:
        return out

    result: List[Tuple[Any, "Message", bool]] = []
    pending_pre_user_assistants: List[Tuple[Any, "Message", bool]] = []
    i = 0
    n = len(out)

    while i < n:
        role = getattr(out[i][1], "role", None)
        if role == "assistant" and not result:
            # Legacy edge case: assistant-only sub-agent rows can precede the
            # first user row. Attach them to that upcoming user turn.
            pending_pre_user_assistants.append(out[i])
            i += 1
            continue
        if role != "user":
            result.append(out[i])
            i += 1
            continue

        user_content = (getattr(out[i][1], "content", "") or "").strip()
        result.append(out[i])
        i += 1

        # Each entry is (assistant_tuple, attached_tool_tuples).
        root_entries: List[
            Tuple[Tuple[Any, "Message", bool], List[Tuple[Any, "Message", bool]]]
        ] = [(e, []) for e in pending_pre_user_assistants if e[2]]
        non_root_entries: List[
            Tuple[Tuple[Any, "Message", bool], List[Tuple[Any, "Message", bool]]]
        ] = [(e, []) for e in pending_pre_user_assistants if not e[2]]
        pending_pre_user_assistants = []
        while i < n:
            erole = getattr(out[i][1], "role", None)
            if erole == "assistant":
                assistant_entry = out[i]
                i += 1
                attached_tools: List[Tuple[Any, "Message", bool]] = []
                if _message_has_tool_calls(assistant_entry[1]):
                    while i < n and getattr(out[i][1], "role", None) == "tool":
                        attached_tools.append(out[i])
                        i += 1
                bucket = root_entries if assistant_entry[2] else non_root_entries
                bucket.append((assistant_entry, attached_tools))
            elif erole == "user" and (getattr(out[i][1], "content", "") or "").strip() == user_content:
                # Nested rows can repeat the same user text inside one logical turn.
                i += 1
            else:
                break

        for assistant_entry, attached_tools in non_root_entries + root_entries:
            result.append(assistant_entry)
            result.extend(attached_tools)

    # No user ever appeared; keep any buffered assistant rows in original order.
    if pending_pre_user_assistants:
        result.extend(pending_pre_user_assistants)
    return result


def get_conversation_history_from_transcripts(
    motet: Any,
    conversation_id: str,
    *,
    limit: int = 250,
) -> List[Tuple[Any, "Message"]]:
    """
    Recall and render canonical conversation_transcript memories to (created_at, Message) list.

    Each stored memory holds only this turn's delta (new user + tool calls + assistant);
    the first memory includes system + first user + first assistant. Messages are
    concatenated in order to form a single linear history.

    Returns chronological list of (created_at, message) where created_at is the
    transcript memory's created_at (same for all messages from that turn). Callers
    can use messages only (prepare_context) or format created_at for API (conversation_get).
    """
    from .transcript_codec import deserialize_transcript_items
    from .transcript_rendering import render_transcript_items_to_messages
    from ..tools.arguments_offload import hydrate_transcript_tool_arguments

    # Internal shape: (created_at, message, root_assistant_candidate)
    out: List[Tuple[Any, Message, bool]] = []
    if not motet or not getattr(motet, "memory", None) or not hasattr(motet.memory, "recall_conversation"):
        return []

    memories = motet.memory.recall_conversation(
        conversation_id=conversation_id,
        types=["conversation_transcript"],
        limit=limit,
    )
    if not memories:
        return []

    def _sequence_key(m: Any) -> int:
        md = getattr(m, "metadata", {}) or {}
        seq = md.get("sequence")
        if seq is None:
            return 0
        try:
            return int(seq)
        except (TypeError, ValueError):
            logger.warning("transcript_replay_invalid_sequence", sequence=seq)
            return 0

    memories = sorted(memories, key=_sequence_key)
    provider_name = "openai"
    if getattr(motet, "stack", None) and getattr(motet.stack, "config", None):
        provider_name = getattr(motet.stack.config, "model_provider", None) or provider_name
    provider_name = str(provider_name)

    artifact_store = getattr(motet, "artifact_store", None)

    def _fetch_tool_arguments(artifact_id: str) -> Any:
        if artifact_store is None or not hasattr(artifact_store, "get"):
            return None
        return artifact_store.get(
            artifact_id,
            tenant_id=getattr(motet, "tenant_id", None),
            principal_id=getattr(motet, "principal_id", None),
            motet_id=getattr(motet, "motet_id", None),
        )

    for m in memories:
        created_at = getattr(m, "created_at", None)
        md = getattr(m, "metadata", {}) or {}
        raw_items = md.get("items")
        if not raw_items:
            continue
        try:
            items = deserialize_transcript_items(raw_items)
            items = hydrate_transcript_tool_arguments(
                items,
                fetch_arguments=_fetch_tool_arguments,
            )
            turn_aid = md.get("agent_id")
            turn_aid_str = str(turn_aid).strip() if turn_aid else None
            root_agent_id = str(md.get("root_agent_id") or "").strip()
            parent_agent_id = str(md.get("parent_agent_id") or "").strip()
            root_turn_raw = md.get("root_turn")
            msgs = render_transcript_items_to_messages(
                items,
                provider_name=provider_name,
                turn_agent_id=turn_aid_str or None,
            )
            for msg in msgs:
                role = getattr(msg, "role", None)
                msg_agent_id = str(getattr(msg, "agent_id", "") or "").strip()
                if not msg_agent_id:
                    msg_agent_id = turn_aid_str or ""
                if parent_agent_id and role == "assistant":
                    msg.parent_agent_id = parent_agent_id
                if root_turn_raw is None:
                    root_assistant_candidate = bool(
                        role == "assistant"
                        and root_agent_id
                        and msg_agent_id
                        and root_agent_id == msg_agent_id
                    )
                else:
                    root_assistant_candidate = bool(role == "assistant" and bool(root_turn_raw))
                out.append((created_at, msg, root_assistant_candidate))
        except Exception as e:
            logger.debug("transcript_replay_entry_skipped", error=str(e))
            continue

    out = _order_turn_messages(out)
    return [(created_at, msg) for created_at, msg, _ in out]


def _normalize_agent_id(msg: Any) -> Any:
    """Return agent_id for keying; empty string is treated as missing (None)."""
    aid = getattr(msg, "agent_id", None)
    if aid is None:
        return None
    if isinstance(aid, str) and not aid.strip():
        return None
    return aid


def _make_message_key(msg: Any) -> Tuple[Any, ...]:
    """Create a stable key for message deduplication (role, content, name, tool_calls/tool_call_id)."""
    from motet.core.models.adapters.tool_call_codec import tool_calls_from_message

    tool_calls = tool_calls_from_message(msg)
    tool_call_id = getattr(msg, "tool_call_id", None)
    if getattr(msg, "role", None) == "assistant" and tool_calls:
        tool_calls_repr = tuple((tc.call_id, tc.tool_name) for tc in tool_calls)
        return (
            getattr(msg, "role", None),
            getattr(msg, "content", None),
            getattr(msg, "name", None),
            tool_calls_repr,
            _normalize_agent_id(msg),
        )
    if getattr(msg, "role", None) == "tool" and tool_call_id:
        return (getattr(msg, "role", None), getattr(msg, "content", None), getattr(msg, "name", None), tool_call_id)
    if getattr(msg, "role", None) == "assistant":
        return (
            getattr(msg, "role", None),
            getattr(msg, "content", None),
            getattr(msg, "name", None),
            _normalize_agent_id(msg),
        )
    return (getattr(msg, "role", None), getattr(msg, "content", None), getattr(msg, "name", None))


def _message_keys_match(hist_key: Tuple[Any, ...], current_key: Tuple[Any, ...]) -> bool:
    """True if hist_key matches current_key for merge dedupe.

    Exact match always wins. For assistant keys, a missing ``agent_id`` on the
    *current* (incoming) side is a wildcard: clients that echo history without
    provenance still dedupe against the stored transcript copy, while two
    stored assistants that both carry distinct agent_ids remain distinct.
    """
    if hist_key == current_key:
        return True
    if (
        hist_key
        and current_key
        and hist_key[0] == "assistant"
        and current_key[0] == "assistant"
        and len(hist_key) == len(current_key)
        and current_key[-1] is None
        and hist_key[:-1] == current_key[:-1]
    ):
        return True
    return False


def merge_conversation_history(
    current_messages: List[Any],
    history_messages: List[Any],
) -> List[Any]:
    """
    Prepend history_messages to current_messages, skipping any history message that
    is already present in current_messages (by role, content, name, tool_calls/tool_call_id,
    and agent_id for assistant turns).

    Used by prepare_context to merge replayed transcript history with the current turn.
    Incoming assistant messages with no agent_id match a stored copy with the same
    role/content/name (and tool_calls) regardless of stored agent_id; each incoming
    key is consumed once so multi-agent same-content turns are not over-collapsed.
    """
    available_keys: List[Tuple[Any, ...]] = [_make_message_key(m) for m in current_messages]
    to_prepend: List[Any] = []
    for hist_msg in history_messages:
        key = _make_message_key(hist_msg)
        match_idx = next(
            (i for i, ck in enumerate(available_keys) if _message_keys_match(key, ck)),
            None,
        )
        if match_idx is not None:
            available_keys.pop(match_idx)
            continue
        to_prepend.append(hist_msg)
    return to_prepend + list(current_messages)


def message_to_history_item(msg: Any, created_at: Any) -> dict | None:
    """
    Convert a Message and created_at to the API history item dict (content, role, created_at, attachments, agent_id, parent_agent_id).
    Returns None for assistant messages that are only tool-call placeholders (empty content + tool_calls)
    so the UI can skip them.
    """
    created_str = (
        created_at.isoformat()
        if hasattr(created_at, "isoformat")
        else str(created_at) if created_at is not None else ""
    )
    item: dict = {
        "content": getattr(msg, "content", "") or "",
        "role": getattr(msg, "role", "user"),
        "created_at": created_str,
    }
    attachments = []
    seen_ids: set = set()
    # Add from content_parts first, but skip image parts so we don't double-show the same
    # image: the message can have both content_parts (e.g. MediaPart with derived artifact_id)
    # and attachments (original upload). We only emit images from attachments below.
    for part in getattr(msg, "content_parts", None) or []:
        aid = getattr(part, "artifact_id", None)
        mime = getattr(part, "mime_type", None)
        if not aid or not mime or aid in seen_ids:
            continue
        mime_str = (mime or "").lower()
        if mime_str.startswith("image/"):
            continue  # Skip image parts; they are represented in msg.attachments (avoids duplicate render)
        seen_ids.add(aid)
        attachments.append({
            "artifact_id": aid,
            "content_type": mime,
            "filename": getattr(part, "media_type", "attachment"),
            "bytes": 0,
        })
    def _safe_int_bytes(value: Any) -> int:
        try:
            return int(value or 0)
        except Exception:
            return 0

    # Determine once whether this message has at least one real image attachment.
    # If true, placeholder image entries should always be dropped regardless of ordering.
    has_real_image_attachment = False
    for att in getattr(msg, "attachments", None) or []:
        if not isinstance(att, dict):
            continue
        ctype = str(att.get("content_type") or att.get("content-type") or "").lower()
        filename = str(att.get("filename") or "").strip().lower()
        bytes_val = _safe_int_bytes(att.get("bytes", 0))
        if ctype.startswith("image/") and not (bytes_val == 0 and filename in ("", "image")):
            has_real_image_attachment = True
            break
    for att in getattr(msg, "attachments", None) or []:
        if not isinstance(att, dict):
            continue
        aid = att.get("artifact_id")
        ctype = att.get("content_type") or att.get("content-type")
        if not aid or aid in seen_ids:
            continue
        filename = att.get("filename") or "attachment"
        bytes_val = _safe_int_bytes(att.get("bytes", 0))
        # Skip placeholder image entries (0 bytes, generic "image" name) when we already
        # have a real image for this message — avoids double render when backend stores
        # both a derived/placeholder and the original upload.
        ctype_lc = str(ctype or "").lower()
        is_placeholder_image = (
            ctype_lc.startswith("image/")
            and bytes_val == 0
            and (filename or "").strip().lower() in ("", "image")
        )
        if is_placeholder_image and has_real_image_attachment:
            continue
        seen_ids.add(aid)
        attachments.append({
            "artifact_id": aid,
            "content_type": ctype or "application/octet-stream",
            "filename": filename,
            "bytes": bytes_val,
        })
    if attachments:
        item["attachments"] = attachments
    aid = getattr(msg, "agent_id", None)
    if aid:
        item["agent_id"] = aid
    parent_aid = getattr(msg, "parent_agent_id", None)
    if parent_aid:
        item["parent_agent_id"] = parent_aid
    if (
        item["role"] == "assistant"
        and not (item.get("content") or "").strip()
        and _message_has_tool_calls(msg)
    ):
        return None
    return item


__all__ = [
    "get_conversation_history_from_transcripts",
    "merge_conversation_history",
    "message_to_history_item",
]
