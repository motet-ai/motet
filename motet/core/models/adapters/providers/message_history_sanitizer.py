"""
Motet - Provider Message History Sanitizer

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Shared provider-boundary sanitizer for message histories that may include
    assistant tool-call messages and tool result messages. Ensures every assistant
    message containing tool_calls has matching tool responses before forwarding
    to model providers that enforce strict ordering (e.g., Moonshot/OpenAI/DeepSeek).

    Also the single source of truth for the trailing-turn invariant: a history
    handed to a model must end on a user turn or tool result, never an assistant
    turn. Anthropic reads a trailing assistant turn as an assistant-message
    prefill, which models from Opus 4.5 onward reject outright; Motet's canonical
    protocol has no prefill concept, so the shape always means a turn
    was assembled without user input. ``needs_user_turn`` lets assemblers repair
    the history; ``assert_trailing_user_turn`` lets adapters fail with the real
    cause instead of an opaque provider 400.

Dependencies:
    - typing for generic message handling

Usage:
    from motet.core.models.adapters.providers.message_history_sanitizer import (
        sanitize_orphan_tool_call_messages,
        needs_user_turn,
        assert_trailing_user_turn,
    )
    safe_messages, stats = sanitize_orphan_tool_call_messages(messages)
    if needs_user_turn(history):
        history.append(Message(role="user", content=input_text))
    assert_trailing_user_turn(rendered, provider="anthropic", model=model_name)

Notes:
    - Operates on both Message objects and dict-shaped messages.
    - Removes malformed assistant tool-call spans and unmatched tool messages.
    - Looks past transparent system/developer noise between an assistant tool_calls
      message and its tool results, then re-emits tools immediately after the
      assistant so Chat Completions providers accept the block.
    - Preserves valid assistant+tool blocks (with repaired adjacency).
    - system/developer messages are non-conversational for the trailing-turn
      invariant: trailing memory/context system messages never mask an assistant tail.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# Roles that may sit between an assistant tool_calls message and its tool
# results without belonging to the tool-call transcript (memory recall, hooks).
_TRANSPARENT_ROLES = frozenset({"system", "developer"})


def _get_role(msg: Any) -> str:
    """Best-effort role extraction from Message-like or dict messages."""
    if isinstance(msg, dict):
        return str(msg.get("role") or "")
    return str(getattr(msg, "role", "") or "")


def _get_tool_calls(msg: Any) -> Any:
    """Best-effort tool_calls extraction from Message-like or dict messages.

    ADR-0137 / #225: read ``tool_calls_canonical`` only.
    """
    from motet.core.models.adapters.tool_call_codec import tool_calls_from_message

    calls = tool_calls_from_message(msg)
    return calls or None


def _get_tool_call_id(msg: Any) -> str:
    """Best-effort tool_call_id extraction from Message-like or dict messages."""
    if isinstance(msg, dict):
        raw = msg.get("tool_call_id")
    else:
        raw = getattr(msg, "tool_call_id", None)
    return str(raw or "")


def _extract_tool_call_ids(tool_calls: Any) -> List[str]:
    """Extract normalized call IDs from tool_calls payloads."""
    if not isinstance(tool_calls, list):
        return []
    out: List[str] = []
    for call in tool_calls:
        if isinstance(call, dict):
            raw = call.get("call_id") or call.get("id") or call.get("tool_call_id")
        else:
            raw = (
                getattr(call, "call_id", None)
                or getattr(call, "id", None)
                or getattr(call, "tool_call_id", None)
            )
        call_id = str(raw or "").strip()
        if call_id:
            out.append(call_id)
    return out


def last_conversational_role(messages: List[Any]) -> Optional[str]:
    """
    Role of the last conversational (non-system/developer) message, or None.

    Works on Message objects and dict-shaped messages alike.
    """
    for msg in reversed(messages or []):
        role = _get_role(msg)
        if role in ("system", "developer"):
            continue
        return role or None
    return None


def needs_user_turn(messages: List[Any]) -> bool:
    """
    True when the history has no conversational tail to answer.

    A history is answerable when its last conversational turn is user input or a
    tool result. Checking only whether a user message exists *somewhere* is not
    enough: a turn assembled with no input of its own (e.g. a scheduled agent
    turn) replays stored history that already contains older user messages but
    ends on the assistant's reply.
    """
    return last_conversational_role(messages) not in ("user", "tool")


def assert_trailing_user_turn(messages: List[Any], *, provider: str, model: str) -> None:
    """
    Reject a rendered history whose last turn is an assistant message (or empty).

    Raises ValueError naming the actual cause. Use at provider boundaries where a
    trailing assistant turn is read as an assistant prefill and hard-rejected
    (Anthropic, Opus 4.5+).
    """
    if not messages:
        raise ValueError(
            f"{provider} request for {model} has no user/assistant messages; "
            "the turn was assembled without any conversational input."
        )
    if _get_role(messages[-1]) == "assistant":
        roles = [_get_role(m) for m in messages]
        raise ValueError(
            f"{provider} request for {model} ends with an assistant message, which the API "
            "treats as an assistant prefill. The conversation must end with a user turn — "
            "this usually means the turn carried no user message and only replayed history. "
            f"Rendered roles: {roles}"
        )


def sanitize_orphan_tool_call_messages(messages: List[Any]) -> Tuple[List[Any], Dict[str, int]]:
    """
    Repair or remove tool-call transcript spans that providers would reject.

    - Keeps assistant(tool_calls) blocks whose tool results are present (looking
      past transparent system/developer noise) and re-emits tools immediately
      after the assistant so adjacency is provider-valid.
    - Drops incomplete assistant(tool_calls) blocks and any partial tool results
      collected for them.
    - Drops orphan ``role=tool`` messages that are not part of a kept block.

    Returns:
        (sanitized_messages, stats)
        stats contains:
          - removed_assistant_calls
          - removed_tool_messages
    """
    sanitized: List[Any] = []
    removed_assistant_calls = 0
    removed_tool_messages = 0

    i = 0
    while i < len(messages):
        current = messages[i]
        role = _get_role(current)
        tool_calls = _get_tool_calls(current)

        if role == "tool":
            # Orphan tool result: no preceding kept assistant tool_calls block.
            removed_tool_messages += 1
            i += 1
            continue

        if role != "assistant" or not tool_calls:
            sanitized.append(current)
            i += 1
            continue

        call_ids = _extract_tool_call_ids(tool_calls)
        if not call_ids:
            sanitized.append(current)
            i += 1
            continue

        j = i + 1
        transparent: List[Any] = []
        trailing_tools: List[Any] = []
        responded_ids: set[str] = set()

        # Memory/hook system messages may land between the assistant call and
        # its tool results; treat them as transparent for matching, then place
        # them after the repaired tool block so providers see valid adjacency.
        while j < len(messages) and _get_role(messages[j]) in _TRANSPARENT_ROLES:
            transparent.append(messages[j])
            j += 1

        while j < len(messages) and _get_role(messages[j]) == "tool":
            tool_msg = messages[j]
            trailing_tools.append(tool_msg)
            tool_call_id = _get_tool_call_id(tool_msg)
            if tool_call_id:
                responded_ids.add(tool_call_id)
            j += 1

        missing = [call_id for call_id in call_ids if call_id not in responded_ids]
        if missing:
            removed_assistant_calls += 1
            removed_tool_messages += len(trailing_tools)
            sanitized.extend(transparent)
            i = j
            continue

        sanitized.append(current)
        sanitized.extend(trailing_tools)
        sanitized.extend(transparent)
        i = j

    return sanitized, {
        "removed_assistant_calls": removed_assistant_calls,
        "removed_tool_messages": removed_tool_messages,
    }
