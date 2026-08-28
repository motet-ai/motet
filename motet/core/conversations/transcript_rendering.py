"""
Motet - Canonical Transcript Rendering (impl-070)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Provider-aware rendering of canonical TranscriptItem sequences into Message lists
    suitable for model context replay.

    This is the "render per provider at call time" half of impl-070:
    - Storage persists a canonical TranscriptItem list (Message/ToolCallRequest/ToolCallResult).
    - Rendering converts that list into canonical Message sequences
        (``Message.tool_calls_canonical``). Model adapters then translate
        to provider wire formats (OpenAI Responses, Anthropic, etc.).

Dependencies:
    - motet.core.types: Message, ToolCallRequest, ToolCallResult, TranscriptItem
    - json: formatting tool outputs for Message content

Usage:
    from motet.core.conversations.transcript_rendering import render_transcript_items_to_messages

    messages = render_transcript_items_to_messages(items)

Notes:
    - Replay output is canonical: ``Message.tool_calls_canonical`` holds
      ``ToolCallRequest`` values.
    - Fail-closed: unknown items are skipped.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..types import Message, ToolCallRequest, ToolCallResult, TranscriptItem


def _tool_result_content(output: Any) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    try:
        return json.dumps(output, ensure_ascii=False, default=str)
    except Exception:
        return str(output)


def render_transcript_items_to_messages(
    items: List[TranscriptItem],
    *,
    provider_name: Optional[str] = None,
    turn_agent_id: Optional[str] = None,
) -> List[Message]:
    """
    Render canonical TranscriptItem list into canonical Message objects.

    Assistant tool calls land on ``Message.tool_calls_canonical``.
    Model adapters translate these to provider wire format (OpenAI, Anthropic, etc.).

    Args:
        items: Canonical TranscriptItem list to render.
        provider_name: Reserved for future provider-specific rendering (e.g. Anthropic
            tool-use format). Currently unused; output is always canonical Message objects
            regardless of provider.
        turn_agent_id: When set, applied to assistant messages rebuilt from tool-call buffers
            and to assistant messages missing ``agent_id`` (ADR-0083 replay).
    """
    out: List[Message] = []

    # Buffer consecutive tool call requests to emit a single assistant tool_calls message.
    buffered_calls: List[ToolCallRequest] = []
    buffered_group_id: str | None = None

    def flush_calls() -> None:
        nonlocal buffered_calls, buffered_group_id
        if not buffered_calls:
            return
        assistant_kw: Dict[str, Any] = {
            "role": "assistant",
            "content": "",
            "tool_calls_canonical": list(buffered_calls),
        }
        if turn_agent_id:
            assistant_kw["agent_id"] = turn_agent_id
        out.append(Message(**assistant_kw))
        buffered_calls = []
        buffered_group_id = None

    for it in items or []:
        if isinstance(it, Message):
            flush_calls()
            msg = it
            if (
                turn_agent_id
                and getattr(msg, "role", None) == "assistant"
                and not getattr(msg, "agent_id", None)
            ):
                msg = msg.model_copy(update={"agent_id": turn_agent_id})
            out.append(msg)
            continue

        if isinstance(it, ToolCallRequest):
            # Group by tool_call_group_id when present; flush and start a new group only when
            # the group_id changes to a different non-None value. Requests without a group_id
            # (gid=None) are treated as implicitly belonging to the current batch — this handles
            # the parallel tool-call case where all requests arrive ungrouped but should be
            # emitted as a single assistant tool_calls message.
            gid = it.tool_call_group_id
            if buffered_calls and gid and buffered_group_id and gid != buffered_group_id:
                flush_calls()
            if buffered_group_id is None:
                buffered_group_id = gid
            buffered_calls.append(it)
            continue

        if isinstance(it, ToolCallResult):
            flush_calls()
            out.append(
                Message(
                    role="tool",
                    content=_tool_result_content(it.output),
                    tool_call_id=it.call_id,
                    name=it.tool_name,
                )
            )
            continue

        # Unknown item type (fail-closed)
        continue

    flush_calls()
    return out


__all__ = ["render_transcript_items_to_messages"]
