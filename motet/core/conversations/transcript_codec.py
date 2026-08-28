"""
Motet - Canonical Transcript Codec

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Utilities for serializing and deserializing canonical conversation transcripts for
    cross-turn replay (impl-070), aligned with (tool invocation transcripts)
    and (provider-agnostic protocol).

    We store a canonical transcript as an ordered list of TranscriptItem objects:
    - Message
    - ToolCallRequest
    - ToolCallResult

    The codec produces JSON-serializable dicts with a small discriminator field so that
    the transcript can be persisted inside MemoryItem.metadata and reconstructed later.
    build_transcript_items_for_turn() builds the ordered TranscriptItem list for one
    turn (messages + tool invocations + assistant reply) for finalize_turn.

Dependencies:
    - motet.core.types: Message, ToolCallRequest, ToolCallResult, TranscriptItem
    - motet.core.tools.tool_transcripts: ToolInvocation, ToolInvocationStatus
    - pydantic: model_dump / model_validate

Usage:
    from motet.core.conversations.transcript_codec import (
        serialize_transcript_items, deserialize_transcript_items,
        build_transcript_items_for_turn,
    )

    payload = serialize_transcript_items(items)
    items2 = deserialize_transcript_items(payload)

Notes:
    - Fail-closed: invalid items are skipped during deserialization.
    - This codec is provider-agnostic; provider-specific rendering happens in transcript_rendering.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog

from ..types import Message, ToolCallRequest, ToolCallResult, TranscriptItem
from ..tools.tool_transcripts import ToolInvocation, ToolInvocationStatus

logger = structlog.get_logger(__name__)


_TYPE_KEY = "_type"
_TYPE_MESSAGE = "message"
_TYPE_TOOL_CALL_REQUEST = "tool_call_request"
_TYPE_TOOL_CALL_RESULT = "tool_call_result"


def serialize_transcript_item(item: TranscriptItem) -> Dict[str, Any]:
    """Serialize a single TranscriptItem to a JSON-serializable dict."""
    if isinstance(item, Message):
        return {_TYPE_KEY: _TYPE_MESSAGE, **item.model_dump(mode="json", exclude_none=True)}
    if isinstance(item, ToolCallRequest):
        return {_TYPE_KEY: _TYPE_TOOL_CALL_REQUEST, **item.model_dump(mode="json", exclude_none=True)}
    if isinstance(item, ToolCallResult):
        return {_TYPE_KEY: _TYPE_TOOL_CALL_RESULT, **item.model_dump(mode="json", exclude_none=True)}
    # Should not happen given TranscriptItem union, but keep fail-closed behavior.
    return {_TYPE_KEY: "unknown"}


def serialize_transcript_items(items: List[TranscriptItem]) -> List[Dict[str, Any]]:
    """Serialize a list of TranscriptItem objects to JSON-serializable dicts."""
    out: List[Dict[str, Any]] = []
    for it in items or []:
        try:
            out.append(serialize_transcript_item(it))
        except Exception as e:
            logger.warning("transcript_item_serialize_failed", error=str(e), item_type=type(it).__name__)
    return out


def deserialize_transcript_item(raw: Dict[str, Any]) -> TranscriptItem | None:
    """Deserialize a TranscriptItem dict (as produced by serialize_transcript_item)."""
    if not isinstance(raw, dict):
        return None

    t = raw.get(_TYPE_KEY)
    payload = {k: v for k, v in raw.items() if k != _TYPE_KEY}
    try:
        if t == _TYPE_MESSAGE:
            return Message.model_validate(payload)
        if t == _TYPE_TOOL_CALL_REQUEST:
            return ToolCallRequest.model_validate(payload)
        if t == _TYPE_TOOL_CALL_RESULT:
            return ToolCallResult.model_validate(payload)
    except Exception as e:
        logger.warning("transcript_item_deserialize_failed", error=str(e), item_type=str(t))
        return None
    return None


def deserialize_transcript_items(raw_items: Any) -> List[TranscriptItem]:
    """Deserialize a list of TranscriptItem dicts back into canonical objects (best-effort)."""
    if not isinstance(raw_items, list):
        return []

    out: List[TranscriptItem] = []
    for raw in raw_items:
        it = deserialize_transcript_item(raw)
        if it is not None:
            out.append(it)
    return out


def build_transcript_items_for_turn(
    messages: List[Any],
    invocations: List[ToolInvocation],
    assistant_response: Optional[str] = None,
    *,
    agent_id: Optional[str] = None,
    pending_action: Optional[Dict[str, Any]] = None,
) -> List[TranscriptItem]:
    """
    Build the ordered list of TranscriptItems for one turn (messages + tool calls + assistant reply).

    Used by finalize_turn to produce the payload stored as conversation_transcript.
    The caller provides the per-turn message subset to persist (for example: system on first
    turn + current user message), and this function appends tool invocations and assistant text.

    When ``agent_id`` is set (qualified registry id), it is stored on the final assistant
    ``Message`` (ADR-0083 attribution for canonical transcript replay).

    When ``pending_action`` is set (ADR-0121 marker dict), it is stored under
    ``metadata["pending_action"]`` on the final assistant ``Message`` so the next
    turn's routing can detect the pending confirmation.
    """
    items: List[TranscriptItem] = []
    for msg in (messages or []):
        if isinstance(msg, Message):
            items.append(msg)
        elif isinstance(msg, dict):
            try:
                items.append(Message.model_validate(msg))
            except Exception:
                continue  # skip malformed message during deserialization

    if invocations:
        invocations = sorted(
            invocations,
            key=lambda i: (i.started_at.timestamp() if i.started_at else 0.0, i.tool_call_index or 0),
        )
        for inv in invocations:
            items.append(
                ToolCallRequest(
                    call_id=inv.tool_call_id,
                    tool_name=inv.tool_name,
                    arguments_json=inv.arguments_json,
                    arguments=None,
                    arguments_artifact_id=getattr(inv, "arguments_artifact_id", None),
                    tool_call_group_id=inv.tool_call_group_id,
                    tool_call_index=inv.tool_call_index,
                )
            )
            if inv.status == ToolInvocationStatus.SUCCESS:
                tool_output: Dict[str, Any] = {
                    "preview": inv.preview_observation,
                    "artifact_id": inv.artifact_id,
                    "provider": inv.provider,
                }
                # ADR-0113: persist artifact-backed media (e.g. generated images) so the
                # tool result references real artifacts on replay instead of a null id.
                if inv.result_media:
                    tool_output["media"] = inv.result_media
                items.append(
                    ToolCallResult(
                        call_id=inv.tool_call_id,
                        tool_name=inv.tool_name,
                        output=tool_output,
                        is_error=False,
                        tool_call_group_id=inv.tool_call_group_id,
                        tool_call_index=inv.tool_call_index,
                    )
                )
            else:
                items.append(
                    ToolCallResult(
                        call_id=inv.tool_call_id,
                        tool_name=inv.tool_name,
                        output={
                            "error": inv.error_summary or "Tool did not complete successfully.",
                            "provider": inv.provider,
                        },
                        is_error=True,
                        error_type=inv.status.value if hasattr(inv.status, "value") else str(inv.status),
                        error_message=inv.error_summary,
                        tool_call_group_id=inv.tool_call_group_id,
                        tool_call_index=inv.tool_call_index,
                    )
                )

    if assistant_response:
        assistant_metadata: Dict[str, Any] = {}
        if pending_action:
            assistant_metadata["pending_action"] = dict(pending_action)
        items.append(
            Message(
                role="assistant",
                content=assistant_response,
                agent_id=agent_id,
                metadata=assistant_metadata,
            )
        )
    return items


__all__ = [
    "build_transcript_items_for_turn",
    "deserialize_transcript_item",
    "deserialize_transcript_items",
    "serialize_transcript_item",
    "serialize_transcript_items",
]
