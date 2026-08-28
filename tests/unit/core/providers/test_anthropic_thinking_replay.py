"""
Motet - Anthropic Thinking Block Replay Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-23

Description:
    Unit tests for multi-turn thinking block replay in the Anthropic Messages adapter
    (ADR-0064 R10). Anthropic returns signed `thinking` (and opaque `redacted_thinking`)
    content blocks when extended/adaptive thinking is enabled; replaying them verbatim
    ahead of the assistant turn's text/tool_use blocks preserves chain-of-thought
    continuity across tool iterations.

    Validates:
    - _extract_thinking_replay_blocks keeps signed thinking + redacted_thinking blocks
      and drops unsigned thinking blocks
    - _format_messages_for_anthropic replays persisted blocks first in assistant turns
      when include_thinking_blocks=True, omits them when False
    - foreign-shaped reasoning_blocks (e.g. OpenAI encrypted reasoning items) are never
      rendered into Anthropic history

Dependencies:
    - pytest
    - motet.core.models.adapters.providers.anthropic_messages
    - motet.core.types.Message

Usage:
    pytest tests/unit/core/providers/test_anthropic_thinking_replay.py

Notes:
    - Live end-to-end verification (capture -> persist -> replay against the real API)
      is covered by the opt-in live adapter matrix and manual loop probes.
"""

from __future__ import annotations

from motet.core.models.adapters.providers.anthropic_messages import (
    _extract_thinking_replay_blocks,
    _format_messages_for_anthropic,
    _valid_anthropic_thinking_blocks,
)
from motet.core.types import Message, RequestContext

_SIGNED_THINKING = {"type": "thinking", "thinking": "Let me work this out...", "signature": "sig-abc"}
_REDACTED_THINKING = {"type": "redacted_thinking", "data": "opaque-blob"}


def test_extract_keeps_signed_thinking_and_redacted_blocks() -> None:
    raw = {
        "content": [
            _SIGNED_THINKING,
            _REDACTED_THINKING,
            {"type": "text", "text": "answer"},
            {"type": "tool_use", "id": "toolu_1", "name": "t", "input": {}},
        ]
    }
    blocks = _extract_thinking_replay_blocks(raw)
    assert blocks == [_SIGNED_THINKING, _REDACTED_THINKING]


def test_extract_drops_unsigned_thinking_blocks() -> None:
    raw = {"content": [{"type": "thinking", "thinking": "no signature"}, {"type": "text", "text": "x"}]}
    assert _extract_thinking_replay_blocks(raw) is None


def test_extract_returns_none_without_content() -> None:
    assert _extract_thinking_replay_blocks({}) is None
    assert _extract_thinking_replay_blocks({"content": "not-a-list"}) is None


def _assistant_with_blocks(blocks: list) -> list:
    return [
        Message(role="user", content="question"),
        Message(
            role="assistant",
            content="",
            tool_calls_canonical=[{"call_id": "toolu_1", "tool_name": "t", "arguments_json": "{}"}],
            reasoning_content="Let me work this out...",
            reasoning_blocks=blocks,
        ),
        Message(role="tool", content="OK", tool_call_id="toolu_1"),
    ]


def test_format_replays_thinking_blocks_first_when_enabled() -> None:
    _system, msgs = _format_messages_for_anthropic(
        messages=_assistant_with_blocks([_SIGNED_THINKING, _REDACTED_THINKING]),
        request_context=RequestContext(enable_multimodal=False),
        include_thinking_blocks=True,
    )
    assistant = msgs[1]
    assert assistant["role"] == "assistant"
    types = [b["type"] for b in assistant["content"]]
    # Thinking blocks must precede tool_use for Anthropic to accept the turn.
    assert types == ["thinking", "redacted_thinking", "tool_use"]
    assert assistant["content"][0] == _SIGNED_THINKING


def test_format_omits_thinking_blocks_when_disabled() -> None:
    """Anthropic rejects thinking blocks when thinking is off, so replay is gated."""
    _system, msgs = _format_messages_for_anthropic(
        messages=_assistant_with_blocks([_SIGNED_THINKING]),
        request_context=RequestContext(enable_multimodal=False),
        include_thinking_blocks=False,
    )
    types = [b["type"] for b in msgs[1]["content"]]
    assert "thinking" not in types


def test_format_skips_foreign_shaped_reasoning_blocks() -> None:
    """OpenAI encrypted reasoning items must never leak into Anthropic history."""
    openai_item = {"type": "reasoning", "id": "rs_123", "encrypted_content": "gAAAA..."}
    _system, msgs = _format_messages_for_anthropic(
        messages=_assistant_with_blocks([openai_item]),
        request_context=RequestContext(enable_multimodal=False),
        include_thinking_blocks=True,
    )
    types = [b["type"] for b in msgs[1]["content"]]
    assert types == ["tool_use"]


def test_valid_blocks_disqualifies_mixed_lists() -> None:
    """One foreign entry disqualifies the whole list (fail-soft, no partial replay)."""
    m = Message(
        role="assistant",
        content="",
        reasoning_blocks=[_SIGNED_THINKING, {"type": "reasoning", "id": "rs_1"}],
    )
    assert _valid_anthropic_thinking_blocks(m) == []


def test_valid_blocks_drops_unsigned_thinking() -> None:
    m = Message(role="assistant", content="", reasoning_blocks=[{"type": "thinking", "thinking": "x"}])
    assert _valid_anthropic_thinking_blocks(m) == []
