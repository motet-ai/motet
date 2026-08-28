"""
Motet - Trailing User Turn Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-27

Description:
    Regression tests for turns assembled without their own user input. A scheduled
    core.agent_turn that reuses a conversation replays stored history ending on an
    assistant reply; Anthropic models from Opus 4.5 onward read that trailing
    assistant turn as an assistant prefill and reject the request with a 400.

    Covers the shared trailing-turn invariant (needs_user_turn /
    assert_trailing_user_turn in message_history_sanitizer) and the strict
    canonical turn shape: no alias keys ('message'/'prompt') and no bare-string
    coercion — misnamed payloads are rejected with actionable errors at schedule
    creation (validate_command_data / unknown_command_data_keys) so an LLM caller
    self-corrects in one retry.

Dependencies:
    - pytest: Test runner
    - Message: Canonical message type (ADR-0064)

Usage:
    pytest tests/unit/core/test_trailing_user_turn.py

Notes:
    - needs_user_turn treats system/developer messages as non-conversational, so
      trailing memory/context system messages never mask an assistant tail.
    - Tool results are a valid tail: the model is expected to continue from them.
"""

from __future__ import annotations

import pytest

from motet.core.models.adapters.providers.message_history_sanitizer import (
    assert_trailing_user_turn,
    needs_user_turn,
)
from motet.core.commands.base_command_data import unknown_command_data_keys
from motet.core.commands.command_data_classes import (
    AgentTurnData,
    validate_command_data,
)
from motet.core.types import Message


def test_needs_user_turn_on_empty_history() -> None:
    assert needs_user_turn([]) is True


def test_needs_user_turn_with_only_system_messages() -> None:
    history = [
        Message(role="system", content="agent prompt"),
        Message(role="system", content="memory context"),
    ]
    assert needs_user_turn(history) is True


def test_no_user_turn_needed_when_history_ends_with_user() -> None:
    history = [
        Message(role="system", content="agent prompt"),
        Message(role="user", content="hi"),
    ]
    assert needs_user_turn(history) is False


def test_no_user_turn_needed_when_history_ends_with_tool_result() -> None:
    history = [
        Message(role="user", content="search"),
        Message(role="assistant", content="", tool_calls_canonical=[{"call_id": "c1", "tool_name": "t", "arguments_json": "{}"}]),
        Message(role="tool", content="result", tool_call_id="c1"),
    ]
    assert needs_user_turn(history) is False


def test_needs_user_turn_when_assistant_tail_follows_older_user_turn() -> None:
    """A replayed transcript already contains a user message, but not as the tail."""
    history = [
        Message(role="system", content="agent prompt"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="Hi there! How can I help you today?"),
    ]
    assert needs_user_turn(history) is True


def test_needs_user_turn_ignores_trailing_system_messages() -> None:
    history = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="Hi there!"),
        Message(role="system", content="memory item"),
        Message(role="system", content="skill catalog"),
    ]
    assert needs_user_turn(history) is True


def test_needs_user_turn_accepts_dict_shaped_messages() -> None:
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    assert needs_user_turn(history) is True


def test_agent_turn_data_rejects_bare_string_messages() -> None:
    with pytest.raises(ValueError, match="not a string"):
        AgentTurnData(**{"messages": "hi"})


def test_agent_turn_data_accepts_canonical_messages() -> None:
    data = AgentTurnData(**{"messages": [{"role": "user", "content": "hi"}]})
    assert [m.content for m in data.messages] == ["hi"]
    assert data.messages[0].role == "user"


def test_validate_command_data_rejects_unknown_field() -> None:
    error = validate_command_data("core.tool_execution", {"tool": "core.note"})
    assert error is not None
    assert "unknown command_data field" in error


def test_unknown_command_data_keys_flags_alias_keys() -> None:
    assert unknown_command_data_keys(AgentTurnData, {"message": "hi"}) == ["message"]
    assert unknown_command_data_keys(
        AgentTurnData, {"messages": [{"role": "user", "content": "hi"}]}
    ) == []


def test_unknown_command_data_keys_on_non_dict_payload() -> None:
    assert unknown_command_data_keys(AgentTurnData, "hi") == []


def test_validate_command_data_rejects_message_alias_with_hint() -> None:
    error = validate_command_data("core.agent_turn", {"message": "hi"})
    assert error is not None
    assert "unknown command_data field" in error
    assert 'Did you mean "messages"' in error


def test_validate_command_data_rejects_prompt_alias_with_hint() -> None:
    error = validate_command_data("core.agent_turn", {"prompt": "hi"})
    assert error is not None
    assert 'Did you mean "messages"' in error


def test_validate_command_data_rejects_string_messages() -> None:
    error = validate_command_data("core.agent_turn", {"messages": "hi"})
    assert error is not None
    assert "invalid command_data" in error
    assert "not a string" in error


def test_validate_command_data_accepts_bare_command_type() -> None:
    assert validate_command_data("agent_turn", {"messages": [{"role": "user", "content": "hi"}]}) is None


def test_validate_command_data_skips_unregistered_command_type() -> None:
    assert validate_command_data("some-bundle.custom", {"anything": 1}) is None


def test_validate_command_data_rejects_non_object() -> None:
    error = validate_command_data("core.agent_turn", "hi")
    assert error is not None
    assert "must be an object" in error


def test_trailing_turn_guard_rejects_trailing_assistant_turn() -> None:
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Hi there!"}]},
    ]
    with pytest.raises(ValueError, match="assistant prefill"):
        assert_trailing_user_turn(messages, provider="anthropic", model="claude-opus-4-8")


def test_trailing_turn_guard_rejects_empty_message_list() -> None:
    with pytest.raises(ValueError, match="no user/assistant messages"):
        assert_trailing_user_turn([], provider="anthropic", model="claude-opus-4-8")


def test_trailing_turn_guard_allows_trailing_user_turn() -> None:
    messages = [
        {"role": "assistant", "content": [{"type": "text", "text": "Hi there!"}]},
        {"role": "user", "content": [{"type": "text", "text": "hi again"}]},
    ]
    assert_trailing_user_turn(messages, provider="anthropic", model="claude-opus-4-8")
