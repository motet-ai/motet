"""
Motet - Direct-Answer Short-Circuit Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Unit tests for the trivial-turn direct-answer short-circuit.

    The allowlist lives in `trivial_message`. `conversation_analysis` reuses
    `is_trivial_message` so skip-analysis and the gate cannot drift. Only
    messages matching a closed set of trivial expressions (greetings/acks/thanks)
    may skip analysis. Anything not on the allowlist — including short tool
    requests — routes to LLM-free lightweight analysis. The live turn path
    calls `turn_gate` with the last user message.

    Validates:
    - `is_trivial_message`: allowlist matching with punctuation/whitespace
      normalization, the question guard, and the multimodal guard
    - `_should_analyze_conversation`: allowlist messages skip; unlisted short
      messages (tool requests, unusual acks) route to lightweight
    - Pending-action hint gate: an allowlisted ack that answers a
      pending proposal ("Should I send it?" → "ok") is a confirmation, not a
      pleasantry — the marker hint from agent_turn routes it to lightweight
      analysis instead of skipping; without a marker, nothing is pending
      (the command applies no text heuristics of its own)
    - `_tool_intent_pattern`: registry-derived vocabulary (union of registered
      tool keywords + static floor), caching keyed on the registered tool-name
      set, and fallback to the static floor when the registry is unavailable
    - `_extract_analysis_metadata`: analysis_mode / tool_requirements only

Dependencies:
    - pytest
    - unittest.mock
    - motet.core.commands.builtin.conversation_analysis.conversation_analysis
    - motet.core.orchestration.turn.phases

Usage:
    pytest tests/unit/core/orchestration/test_direct_answer_short_circuit.py
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

import importlib

from motet.core.commands.builtin.conversation_analysis.conversation_analysis import (
    _lightweight_intent_detection,
    _should_analyze_conversation,
    _tool_intent_pattern,
)
from motet.core.conversations.trivial_message import is_trivial_message

# Plain `import pkg.mod as m` resolves the trailing attribute on the package,
# where the `conversation_analysis` command function shadows the submodule.
conversation_analysis_module = importlib.import_module(
    "motet.core.commands.builtin.conversation_analysis.conversation_analysis"
)
from motet.core.orchestration.turn.phases import (
    _extract_analysis_metadata,
)
from motet.core.types import Message


def _user(content: str, content_parts: Optional[List[Any]] = None) -> Message:
    return Message(role="user", content=content, content_parts=content_parts)


def _assistant(content: str) -> Message:
    return Message(role="assistant", content=content)


def _system(content: str) -> Message:
    return Message(role="system", content=content)


class TestIsTrivialMessage:
    @pytest.mark.parametrize(
        "text",
        [
            "hi",
            "Hello!",
            "THANKS",
            "thank you so much",
            "ok thanks",
            "sounds good.",
            "sounds good, thanks",
            "  yes please  ",
            "got it!!!",
            "love it",
        ],
    )
    def test_allowlisted_expressions_are_trivial(self, text: str) -> None:
        assert is_trivial_message(_user(text)) is True

    @pytest.mark.parametrize(
        "text",
        [
            # Tool requests and instructions — never trivial
            "search my email",
            "get weather",
            "run the tests",
            "continue please",
            "summarize this",
            # Questions — a trailing "?" turns an ack into a prompt
            "ok?",
            "yes?",
            "what's this?",
            # Unusual acks not on the closed list — fail open to lightweight
            "hmm interesting",
            "cool cool cool",
            # Empty content
            "",
        ],
    )
    def test_unlisted_messages_are_not_trivial(self, text: str) -> None:
        assert is_trivial_message(_user(text)) is False

    def test_multimodal_parts_never_trivial(self) -> None:
        from motet.core.types import TextPart

        msg = _user("ok", content_parts=[TextPart(text="ok")])
        assert is_trivial_message(msg) is False


class TestShouldAnalyzeConversation:
    def test_greeting_skips(self) -> None:
        result = _should_analyze_conversation([_user("hi")])
        assert result == {
            "full_analysis": False,
            "lightweight": False,
            "reason": "simple_query",
        }

    def test_allowlisted_combo_skips(self) -> None:
        result = _should_analyze_conversation([_user("sounds good, thanks")])
        assert result["lightweight"] is False
        assert result["full_analysis"] is False
        assert result["reason"] == "simple_query"

    @pytest.mark.parametrize(
        "text",
        [
            "search my email",
            "get weather",
            "continue please",
            "hmm interesting",
        ],
    )
    def test_unlisted_short_message_routes_to_lightweight(self, text: str) -> None:
        result = _should_analyze_conversation([_user(text)])
        assert result["lightweight"] is True
        assert result["full_analysis"] is False
        assert result["reason"] == "short_query"

    def test_multimodal_short_message_routes_to_lightweight(self) -> None:
        from motet.core.types import TextPart

        msg = _user("ok", content_parts=[TextPart(text="ok")])
        result = _should_analyze_conversation([msg])
        assert result["reason"] == "short_query"
        assert result["lightweight"] is True

    def test_unlisted_short_message_in_long_conversation_stays_lightweight(self) -> None:
        history = [_user(f"message {i}") for i in range(7)]
        history.append(_user("continue please"))
        result = _should_analyze_conversation(history)
        assert result["reason"] == "short_query"
        assert result["lightweight"] is True


class TestPendingActionHintGate:
    """An allowlisted ack answering a pending assistant proposal must not skip.

    Regression for the confirmation-flow hole: "can you send bob an email?" →
    assistant "Here's the draft. Should I send it?" → user "ok". Skipping that
    ack forces the no-tools direct-answer path, so the agent can never execute
    the confirmed action. Pendingness comes exclusively from the ADR-0121
    marker hint that agent_turn passes; the command applies no text heuristics
    or transcript reads of its own.
    """

    @pytest.mark.parametrize("ack", ["ok", "yes", "yes please", "sure", "sounds good"])
    def test_ack_with_fresh_marker_hint_routes_to_lightweight(self, ack: str) -> None:
        result = _should_analyze_conversation(
            [_system("You are helpful."), _user(ack)],
            pending_action={"status": "fresh", "reply": "confirm"},
        )
        assert result == {
            "full_analysis": False,
            "lightweight": True,
            "reason": "confirm_pending_action",
        }

    def test_ack_without_marker_hint_skips(self) -> None:
        # Nothing pending (hint status "none"): "ok thanks" is a pleasantry.
        result = _should_analyze_conversation(
            [_system("You are helpful."), _user("ok thanks")],
            pending_action={"status": "none"},
        )
        assert result["reason"] == "simple_query"
        assert result["lightweight"] is False

    def test_ack_with_no_hint_at_all_skips(self) -> None:
        # No hint (direct invocation): the marker is the only pendingness
        # signal, so its absence means skip.
        result = _should_analyze_conversation([_user("ok")])
        assert result["reason"] == "simple_query"

    def test_in_payload_assistant_question_does_not_gate(self) -> None:
        # Pendingness is marker-only: an assistant question in the payload
        # without a marker hint does not disable the skip (the writer decides
        # at write time, not the reader at read time).
        messages = [
            _assistant("Sure — here's the draft. Should I send it?"),
            _user("ok"),
        ]
        result = _should_analyze_conversation(messages)
        assert result["reason"] == "simple_query"

    def test_greeting_with_no_assistant_history_skips(self) -> None:
        result = _should_analyze_conversation([_user("hi")])
        assert result["reason"] == "simple_query"


class _FakeRegisteredTool:
    """Minimal stand-in: _tool_intent_pattern only reads ``keywords``."""

    def __init__(self, keywords: List[str]) -> None:
        self.keywords = keywords


class TestToolIntentPattern:
    @pytest.fixture(autouse=True)
    def _reset_pattern_cache(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            conversation_analysis_module, "_tool_intent_pattern_cache", (None, None)
        )

    def _with_registry(self, items: Dict[str, Any]):
        return patch(
            "motet.core.tools.registry.registry.list_items",
            return_value=items,
        )

    def test_static_floor_with_empty_registry(self) -> None:
        with self._with_registry({}):
            pattern = _tool_intent_pattern()
        assert pattern.search("search my email")
        assert not pattern.search("together forever")

    def test_registry_keywords_extend_vocabulary(self) -> None:
        items = {"mcp.spotify.play_track": _FakeRegisteredTool(["spotify", "playlist"])}
        with self._with_registry(items):
            pattern = _tool_intent_pattern()
        assert pattern.search("queue up my spotify playlist")
        # Static floor still present alongside registry terms.
        assert pattern.search("search my email")

    def test_single_char_keywords_filtered(self) -> None:
        items = {"tool_a": _FakeRegisteredTool(["x", "telescope"])}
        with self._with_registry(items):
            pattern = _tool_intent_pattern()
        assert pattern.search("point the telescope")
        assert not pattern.search("x marks the spot")

    def test_pattern_cached_until_registry_changes(self) -> None:
        items = {"tool_a": _FakeRegisteredTool(["telescope"])}
        with self._with_registry(items):
            first = _tool_intent_pattern()
            second = _tool_intent_pattern()
        assert first is second

        items_after = dict(items)
        items_after["mcp.spotify.play_track"] = _FakeRegisteredTool(["spotify"])
        with self._with_registry(items_after):
            rebuilt = _tool_intent_pattern()
        assert rebuilt is not first
        assert rebuilt.search("open spotify")

    def test_registry_failure_falls_back_to_static_floor(self) -> None:
        with patch(
            "motet.core.tools.registry.registry.list_items",
            side_effect=RuntimeError("registry unavailable"),
        ):
            pattern = _tool_intent_pattern()
        assert pattern.search("get weather")
        assert not pattern.search("hello there")

    def test_lightweight_intent_uses_registry_keywords(self) -> None:
        items = {"mcp.spotify.play_track": _FakeRegisteredTool(["spotify"])}
        with self._with_registry(items):
            result = _lightweight_intent_detection(
                [_user("queue up something on spotify for the drive home")]
            )
        assert result["intent"]["primary"] == "tool_usage"


class TestLightweightIntentLabel:
    """
    The label is observability, not routing. The patterns are what
    ``conversation_routing_decision`` reports.
    """

    def _intent(self, text: str) -> str:
        return _lightweight_intent_detection([_user(text)])["intent"]["primary"]

    def test_hint_is_gone(self) -> None:
        intent = _lightweight_intent_detection(
            [_user("Evaluate the tradeoffs of a four-day work week")]
        )["intent"]
        assert "strategy_hint" not in intent

    def test_named_workflow_reads_as_tool_usage(self) -> None:
        assert (
            self._intent(
                'Call core.tool_call with tool_name "workflow_expert-panel.discuss" '
                'and parameters {"topic": "remote work"}. Then show the synthesizer assessment.'
            )
            == "tool_usage"
        )

    def test_named_tool_reads_as_tool_usage(self) -> None:
        assert (
            self._intent("Use expert-panel.recall_discussion and list the options it found")
            == "tool_usage"
        )

    def test_analytical_wording_without_a_dispatch(self) -> None:
        assert self._intent("Evaluate the tradeoffs of a four-day work week") == "analysis"

    def test_exploratory_wording_without_a_dispatch(self) -> None:
        assert self._intent("Brainstorm alternatives for our deployment story") == "exploration"

    def test_prose_abbreviations_are_not_read_as_a_dispatch(self) -> None:
        assert self._intent("Compare the u.s. and canadian markets, e.g. by margin") == "analysis"

    def test_analytical_terms_match_at_word_start_only(self) -> None:
        assert self._intent("Assessment of the rollout is attached") == "analysis"
        # "brunch" must not satisfy the exploratory "brainstorm" pattern. This
        # sentence does trip the tool vocabulary on "get", which is why the
        # assertion is negative rather than "general".
        assert self._intent("We should get brunch together sometime") != "exploration"

    def test_ideal_is_not_exploration(self) -> None:
        assert self._intent("That rollout plan is ideal for our timeline") != "exploration"


class TestRecordRoutingDecision:
    """Routing decisions emit one log + daily Redis counters, best-effort."""

    def _record(self, decision: Dict[str, Any]):
        from unittest.mock import MagicMock

        redis = MagicMock()
        pipe = MagicMock()
        redis.pipeline.return_value = pipe
        with patch(
            "motet.core.distributed.redis_manager.get_sync_redis_client",
            return_value=redis,
        ):
            conversation_analysis_module._record_routing_decision(
                decision, word_count=2, message_count=3
            )
        return pipe

    @pytest.mark.parametrize(
        ("decision", "expected_field"),
        [
            (
                {"full_analysis": False, "lightweight": False, "reason": "simple_query"},
                "skip:simple_query",
            ),
            (
                {"full_analysis": False, "lightweight": True, "reason": "confirm_pending_action"},
                "lightweight:confirm_pending_action",
            ),
            (
                {"full_analysis": True, "lightweight": False, "reason": "complex_multi_turn"},
                "full:complex_multi_turn",
            ),
        ],
    )
    def test_counter_field_derived_from_mode_and_reason(
        self, decision: Dict[str, Any], expected_field: str
    ) -> None:
        pipe = self._record(decision)
        incremented = {call.args[1] for call in pipe.hincrby.call_args_list}
        assert incremented == {expected_field, "total"}
        assert pipe.expire.called
        assert pipe.execute.called

    def test_redis_failure_never_raises(self) -> None:
        with patch(
            "motet.core.distributed.redis_manager.get_sync_redis_client",
            side_effect=RuntimeError("redis down"),
        ):
            conversation_analysis_module._record_routing_decision(
                {"full_analysis": False, "lightweight": False, "reason": "simple_query"},
                word_count=1,
                message_count=1,
            )


class TestAnalysisMetadata:
    def _analysis(self, mode: str, tool_requirements: str) -> Dict[str, Any]:
        return {
            "intent": {"primary": "general", "confidence": 0.6},
            "complexity": {"tool_requirements": tool_requirements},
            "metadata": {"analysis_mode": mode},
        }

    def test_preserves_analysis_mode_and_tool_requirements(self) -> None:
        meta = _extract_analysis_metadata(self._analysis("skipped", "none"))
        assert meta["analysis_mode"] == "skipped"
        assert meta["tool_requirements"] == "none"
        assert "skip_reasoning" not in meta

    def test_lightweight_mode_is_preserved(self) -> None:
        meta = _extract_analysis_metadata(self._analysis("lightweight", "none"))
        assert meta["analysis_mode"] == "lightweight"
        assert meta["tool_requirements"] == "none"


class TestNoToolsSystemPrompt:
    def test_trivial_is_brief(self) -> None:
        from motet.core.orchestration.turn.no_tools import no_tools_system_prompt

        prompt = no_tools_system_prompt("trivial")
        assert "Reply briefly" in prompt
        assert "Answer the user's question directly" not in prompt

    def test_unset_reason_does_not_demand_a_direct_answer(self) -> None:
        from motet.core.orchestration.turn.no_tools import no_tools_system_prompt

        prompt = no_tools_system_prompt(None)
        assert "without using any tools" in prompt
        assert "Answer the user's question directly" not in prompt
