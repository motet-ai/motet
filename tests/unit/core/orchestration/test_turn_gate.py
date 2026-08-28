"""
Motet - Turn Gate Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Unit tests for the always-on local turn gate and the single mode
    resolve in front of it.

Dependencies:
    - pytest
    - unittest.mock
    - motet.core.orchestration.turn.gate
    - motet.core.conversations.trivial_message
    - motet.core.conversations.pending_action
    - motet.core.types.Message

Usage:
    pytest tests/unit/core/orchestration/test_turn_gate.py
"""

from __future__ import annotations

from typing import Any, List, Optional
from unittest.mock import patch

import pytest

from motet.core.conversations.pending_action import (
    pending_action_block_reason,
    pending_action_blocks_direct,
)
from motet.core.conversations.trivial_message import (
    is_trivial_message,
    last_user_message,
)
from motet.core.orchestration.turn.gate import (
    normalize_turn_mode,
    resolve_turn_mode,
    turn_gate,
)
from motet.core.types import Message, TextPart


def _user(content: str, content_parts: Optional[List[Any]] = None) -> Message:
    return Message(role="user", content=content, content_parts=content_parts)


def _assistant(content: str) -> Message:
    return Message(role="assistant", content=content)


class TestIsTrivialMessage:
    @pytest.mark.parametrize(
        "text",
        ["hi", "Hello!", "thanks", "ok thanks", "  yes please  "],
    )
    def test_allowlisted_expressions_are_trivial(self, text: str) -> None:
        assert is_trivial_message(_user(text)) is True

    @pytest.mark.parametrize(
        "text",
        ["search my email", "ok?", "what's this?", ""],
    )
    def test_unlisted_messages_are_not_trivial(self, text: str) -> None:
        assert is_trivial_message(_user(text)) is False

    def test_multimodal_parts_never_trivial(self) -> None:
        assert is_trivial_message(_user("ok", content_parts=[TextPart(text="ok")])) is False


class TestLastUserMessage:
    def test_returns_last_user(self) -> None:
        history = [_assistant("hi"), _user("hello"), _assistant("ok"), _user("thanks")]
        msg = last_user_message(history)
        assert msg is not None
        assert msg.content == "thanks"

    def test_empty_and_no_user(self) -> None:
        assert last_user_message([]) is None
        assert last_user_message([_assistant("hi")]) is None


class TestPendingActionBlocksDirect:
    def test_fresh_and_stale_block(self) -> None:
        assert pending_action_blocks_direct({"status": "fresh", "reply": "confirm"})
        assert pending_action_blocks_direct({"status": "stale", "reply": "other"})
        assert not pending_action_blocks_direct({"status": "none"})
        assert not pending_action_blocks_direct(None)

    def test_block_reason_partition(self) -> None:
        assert pending_action_block_reason(
            {"status": "stale", "reply": "other"}
        ) == "stale_pending_action"
        assert pending_action_block_reason(
            {"status": "fresh", "reply": "confirm"}
        ) == "confirm_pending_action"
        assert pending_action_block_reason(
            {"status": "fresh", "reply": "decline"}
        ) == "decline_pending_action"
        assert pending_action_block_reason(
            {"status": "fresh", "reply": "other"}
        ) == "ack_to_pending_action"
        assert pending_action_block_reason({"status": "none"}) is None


class TestTurnGate:
    def test_allowlisted_message_is_no_tools(self) -> None:
        decision = turn_gate(message=_user("hi"), skip_simple=True)
        assert decision.mode == "no_tools"
        assert decision.no_tools_reason == "trivial"

    def test_pending_action_keeps_auto(self) -> None:
        decision = turn_gate(
            message=_user("ok"),
            pending_action={"status": "fresh", "reply": "confirm"},
            skip_simple=True,
        )
        assert decision.mode == "auto"
        assert decision.no_tools_reason is None

    def test_none_pending_does_not_block(self) -> None:
        decision = turn_gate(
            message=_user("ok"),
            pending_action={"status": "none"},
            skip_simple=True,
        )
        assert decision.mode == "no_tools"

    def test_skip_simple_off_stays_auto(self) -> None:
        assert turn_gate(message=_user("hi"), skip_simple=False).mode == "auto"

    def test_missing_message_stays_auto(self) -> None:
        assert turn_gate(skip_simple=True).mode == "auto"

    def test_config_failure_defaults_on(self) -> None:
        with patch("motet.core.config.Config", side_effect=RuntimeError("boom")):
            assert turn_gate(message=_user("hi")).mode == "no_tools"


class TestNormalizeTurnMode:
    def test_public_spellings_pass_through(self) -> None:
        assert normalize_turn_mode("auto") == "auto"
        assert normalize_turn_mode("no_tools") == "no_tools"
        assert normalize_turn_mode("agentic") == "agentic"

    def test_unknowns_run_as_auto(self) -> None:
        assert normalize_turn_mode("direct") == "auto"
        assert normalize_turn_mode("react") == "auto"
        assert normalize_turn_mode("compare") == "auto"
        assert normalize_turn_mode(None) == "auto"


class TestResolveTurnMode:
    def test_defaults_to_the_agent_loop(self) -> None:
        result = resolve_turn_mode(context={})
        assert result.mode == "auto"
        assert result.no_tools_reason is None

    def test_forced_no_tools_is_a_constraint(self) -> None:
        result = resolve_turn_mode(
            context={"mode": "no_tools"},
            message=_user("search my email"),
        )
        assert result.mode == "no_tools"
        assert result.no_tools_reason is None

    def test_forced_agentic_skips_the_gate(self) -> None:
        result = resolve_turn_mode(
            context={"mode": "agentic"},
            message=_user("hi"),
        )
        assert result.mode == "agentic"
        assert result.no_tools_reason is None

    def test_unknown_mode_runs_as_auto(self) -> None:
        assert resolve_turn_mode(context={"mode": "compare"}).mode == "auto"
        assert resolve_turn_mode(context={"strategy": "no_tools"}).mode == "auto"

    def test_strategy_is_not_a_mode_key(self) -> None:
        result = resolve_turn_mode(
            context={"strategy": "no_tools"},
            message=_user("search my email"),
        )
        assert result.mode == "auto"

    def test_dropped_spellings_run_as_auto(self) -> None:
        assert resolve_turn_mode(context={"mode": "react"}).mode == "auto"
        assert resolve_turn_mode(context={"mode": "direct"}).mode == "auto"

    def test_trivial_turn_answers_directly(self) -> None:
        result = resolve_turn_mode(
            context={},
            message=_user("hi"),
            skip_simple=True,
        )
        assert result.mode == "no_tools"
        assert result.no_tools_reason == "trivial"

    def test_pending_action_keeps_the_loop(self) -> None:
        result = resolve_turn_mode(
            context={},
            message=_user("ok"),
            pending_action={"status": "fresh", "reply": "confirm"},
            skip_simple=True,
        )
        assert result.mode == "auto"
        assert result.no_tools_reason is None

    def test_empty_context_is_auto(self) -> None:
        result = resolve_turn_mode(context={})
        assert result.mode == "auto"
