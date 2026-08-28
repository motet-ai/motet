"""
Unit tests for TurnOutcome / HandedBackToolCall (issue #147).
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock

from motet.core.orchestration.turn.outcome import (
    HandedBackToolCall,
    TurnOutcomeKind,
    apply_turn_outcome_gate,
    classify_loop_outcome,
    parse_handed_back_tool_calls,
)


def test_classify_complete() -> None:
    outcome = classify_loop_outcome({"stop_reason": "stop", "final_response": "done"})
    assert outcome.kind is TurnOutcomeKind.COMPLETE
    assert outcome.should_finalize is True


def test_classify_suspended_with_typed_handback() -> None:
    outcome = classify_loop_outcome(
        {
            "stop_reason": "suspended",
            "checkpoint_id": "suspend-1",
            "handed_back_tool_calls": [
                {"tool_call_id": "c1", "tool_name": "get_weather", "parameters": {"q": "x"}},
            ],
        }
    )
    assert outcome.kind is TurnOutcomeKind.SUSPENDED
    assert outcome.should_finalize is False
    assert outcome.checkpoint_id == "suspend-1"
    assert outcome.handed_back_tool_calls[0] == HandedBackToolCall(
        tool_call_id="c1",
        tool_name="get_weather",
        parameters={"q": "x"},
    )


def test_classify_auth_required_does_not_finalize() -> None:
    outcome = classify_loop_outcome(
        {
            "stop_reason": "auth_required",
            "auth_required": True,
            "final_response": "please authorize",
            "service_id": "google_workspace",
        }
    )
    assert outcome.kind is TurnOutcomeKind.AUTH_REQUIRED
    assert outcome.should_finalize is False


def test_classify_has_no_escalation_kind() -> None:
    """ADR-0138 deleted the executor swap, so nothing produces this stop_reason.

    A stored payload from before the deletion must still finalize rather than
    fall through to an unhandled kind.
    """
    assert not hasattr(TurnOutcomeKind, "ESCALATION")

    outcome = classify_loop_outcome(
        {"stop_reason": "escalation", "escalation": {"strategy": "cot"}}
    )
    assert outcome.kind is TurnOutcomeKind.COMPLETE
    assert outcome.should_finalize is True


def test_parse_handed_back_skips_incomplete_entries() -> None:
    parsed = parse_handed_back_tool_calls(
        [
            {"tool_call_id": "c1", "tool_name": "a"},
            {"tool_call_id": "", "tool_name": "b"},
            {"tool_name": "c"},
            "nope",
        ]
    )
    assert len(parsed) == 1
    assert parsed[0].tool_call_id == "c1"


def test_apply_gate_auth_required_emits_end_without_finalize_fields() -> None:
    motet = MagicMock()
    payload: Dict[str, Any] = {
        "stop_reason": "auth_required",
        "auth_required": True,
        "final_response": "Authorize Google",
        "service_id": "google_workspace",
        "display_name": "Google",
        "authorization_endpoint": "/oauth",
        "required_scopes": ["gmail"],
    }
    outcome = classify_loop_outcome(payload)
    gated = apply_turn_outcome_gate(
        motet, outcome, payload, "core.chat", None, {}, {},
    )
    assert gated is not None
    assert gated["auth_required"] is True
    assert gated["outcome"] == "auth_required"
    assert motet.stream_event.call_args.args[0] == "end"
    assert motet.stream_event.call_args.kwargs["auth_required"] is True


def test_apply_gate_complete_returns_none() -> None:
    motet = MagicMock()
    outcome = classify_loop_outcome({"stop_reason": "stop"})
    assert (
        apply_turn_outcome_gate(motet, outcome, {"stop_reason": "stop"}, "a", None, {}, {})
        is None
    )
    motet.stream_event.assert_not_called()
