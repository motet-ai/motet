"""
Motet - Pending-Action Confirmation State Tests (ADR-0121 Phase 1)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-21

Description:
    Unit tests for the ADR-0121 Phase 1 pending-action confirmation state:

    - Marker construction: heuristic tail-question writer output
      (marker_id/source/question/tool_shortlist/carried_forward) and the
      capped carry-forward increment
    - Reply classification: the closed confirm/decline/other partition,
      including the amended rule that ambiguous conversation-closers
      ("ok thanks", "no thanks") map to "other"
    - Marker reads: positional semantics (latest root assistant message
      decides; newer rows bury older markers; sub-agent rows skipped),
      freshness derived from the carrying row's timestamp, and fail-open
      behavior
    - Writer integration: store_turn_transcript attaches heuristic markers,
      re-attaches carried markers on deferrals (fresh proposal wins), and
      never writes markers for sub-agent turns
    - Routing: the pending_action hint disables the trivial skip with
      dedicated reasons (confirm_pending_action / decline_pending_action /
      stale_pending_action / ack_to_pending_action); the marker is the single
      source of truth for pendingness (no read-time text heuristics)

Dependencies:
    - pytest
    - motet.core.conversations.pending_action
    - motet.core.conversations.transcript_storage
    - motet.core.commands.builtin.conversation_analysis.conversation_analysis

Usage:
    pytest tests/unit/core/orchestration/test_pending_action_confirmation.py
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from motet.core.conversations.pending_action import (
    AFFIRMATIVE_ACKS,
    CONFIRM_REPLIES,
    DECLINE_REPLIES,
    NEGATIVE_ACKS,
    PendingActionLookup,
    build_carry_forward_marker,
    build_heuristic_marker,
    build_pending_action_system_message,
    classify_confirmation_reply,
    ends_with_question,
    evaluate_pending_action,
    load_pending_action,
)
from motet.core.conversations import transcript_storage
from motet.core.commands.builtin.conversation_analysis.conversation_analysis import (
    _should_analyze_conversation,
)
from motet.core.tools import transcript_service
from motet.core.types import Message


def _user(content: str) -> Message:
    return Message(role="user", content=content)


def _system(content: str) -> Message:
    return Message(role="system", content=content)


# ---------------------------------------------------------------------------
# Heuristic marker construction (Phase 1 writer)
# ---------------------------------------------------------------------------


class TestBuildHeuristicMarker:
    def test_tail_question_produces_marker(self) -> None:
        marker = build_heuristic_marker(
            "Here's the draft.\nShould I send it?",
            ["mcp.google_workspace.send_gmail_message"],
        )
        assert marker is not None
        assert marker["source"] == "heuristic"
        assert marker["question"] == "Should I send it?"
        assert marker["carried_forward"] == 0
        assert marker["marker_id"].startswith("pa_")
        assert marker["tool_shortlist"] == ["mcp.google_workspace.send_gmail_message"]

    def test_statement_produces_no_marker(self) -> None:
        assert build_heuristic_marker("The capital of France is Paris.", []) is None

    def test_rhetorical_mid_message_question_produces_no_marker(self) -> None:
        text = "What does O(n) mean? It means work grows linearly.\nThat's the gist."
        assert build_heuristic_marker(text, []) is None

    def test_empty_response_produces_no_marker(self) -> None:
        assert build_heuristic_marker("", []) is None
        assert build_heuristic_marker(None, []) is None

    def test_shortlist_deduped_sorted_and_omitted_when_empty(self) -> None:
        marker = build_heuristic_marker("Proceed?", ["b_tool", "a_tool", "b_tool", ""])
        assert marker is not None
        assert marker["tool_shortlist"] == ["a_tool", "b_tool"]

        bare = build_heuristic_marker("Proceed?", [])
        assert bare is not None
        assert "tool_shortlist" not in bare

    def test_long_question_truncated(self) -> None:
        marker = build_heuristic_marker("x" * 500 + "?", [])
        assert marker is not None
        assert len(marker["question"]) <= 300
        assert marker["question"].endswith("...")


class TestCarryForward:
    def test_increments_carried_forward(self) -> None:
        marker = {"marker_id": "pa_1", "carried_forward": 0}
        carried = build_carry_forward_marker(marker)
        assert carried is not None
        assert carried["carried_forward"] == 1
        assert carried["marker_id"] == "pa_1"
        # Original untouched.
        assert marker["carried_forward"] == 0

    def test_cap_returns_none(self) -> None:
        assert build_carry_forward_marker({"carried_forward": 2}) is None

    def test_cap_configurable_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MOTET_PENDING_ACTION_MAX_CARRY_FORWARD", "5")
        carried = build_carry_forward_marker({"carried_forward": 2})
        assert carried is not None
        assert carried["carried_forward"] == 3

    def test_missing_count_treated_as_zero(self) -> None:
        carried = build_carry_forward_marker({"marker_id": "pa_2"})
        assert carried is not None
        assert carried["carried_forward"] == 1


# ---------------------------------------------------------------------------
# Reply classification (closed confirm/decline/other partition)
# ---------------------------------------------------------------------------


class TestClassifyConfirmationReply:
    @pytest.mark.parametrize(
        "text",
        ["ok", "OK!", "yes", "yes please", "sure", "yep", "sounds good",
         "go ahead", "do it", "lgtm", "that works", "alright"],
    )
    def test_confirm(self, text: str) -> None:
        assert classify_confirmation_reply(text) == "confirm"

    @pytest.mark.parametrize(
        "text", ["no", "nope", "nah", "cancel", "stop", "never mind"]
    )
    def test_decline(self, text: str) -> None:
        assert classify_confirmation_reply(text) == "decline"

    @pytest.mark.parametrize(
        "text",
        [
            # Ambiguous conversation-closers map to "other" (ADR-0121
            # amendment): the model disambiguates them, not the router.
            "ok thanks",
            "no thanks",
            "thanks",
            "got it",
            "done",
            # Questions never confirm.
            "ok?",
            # Free text is always "other".
            "wait, change the subject line first",
            "",
        ],
    )
    def test_other(self, text: str) -> None:
        assert classify_confirmation_reply(text) == "other"

    def test_none_is_other(self) -> None:
        assert classify_confirmation_reply(None) == "other"


class TestReplyVocabularyConsistency:
    """The ack groups are the single source of truth shared by the trivial
    allowlist (routing) and the confirm/decline classification — these
    invariants keep the two from drifting apart (ADR-0121)."""

    def test_confirm_and_decline_are_disjoint(self) -> None:
        assert not (CONFIRM_REPLIES & DECLINE_REPLIES)

    def test_ack_groups_are_subsets_of_reply_tables(self) -> None:
        assert AFFIRMATIVE_ACKS <= CONFIRM_REPLIES
        assert NEGATIVE_ACKS <= DECLINE_REPLIES

    def test_ack_groups_are_trivial_for_routing(self) -> None:
        from motet.core.conversations.trivial_message import is_trivial_message

        for entry in AFFIRMATIVE_ACKS | NEGATIVE_ACKS:
            assert is_trivial_message(_user(entry)) is True, entry

    def test_every_ack_entry_classifies_as_its_verdict(self) -> None:
        for entry in AFFIRMATIVE_ACKS:
            assert classify_confirmation_reply(entry) == "confirm", entry
        for entry in NEGATIVE_ACKS:
            assert classify_confirmation_reply(entry) == "decline", entry


class TestEvaluatePendingAction:
    """One-shot turn evaluation: marker + reply + carry + routing hint."""

    def _fresh_marker_motet(self, marker: Dict[str, Any]) -> SimpleNamespace:
        return _motet_with_rows(
            [_transcript_row(1, assistant_text=marker.get("question", "?"), marker=marker)]
        )

    def test_fresh_confirm_state(self) -> None:
        marker = {"marker_id": "pa_1", "question": "Send it?", "carried_forward": 0}
        state = evaluate_pending_action(self._fresh_marker_motet(marker), "conv-1", "ok")
        assert state.marker == marker
        assert state.status == "fresh"
        assert state.reply == "confirm"
        assert state.carry is None  # confirm consumes; nothing carried
        assert state.routing_hint == {"status": "fresh", "reply": "confirm"}

    def test_fresh_other_carries_forward(self) -> None:
        marker = {"marker_id": "pa_1", "question": "Send it?", "carried_forward": 0}
        state = evaluate_pending_action(
            self._fresh_marker_motet(marker), "conv-1", "hold on"
        )
        assert state.reply == "other"
        assert state.carry is not None
        assert state.carry["carried_forward"] == 1

    def test_fresh_other_at_cap_does_not_carry(self) -> None:
        marker = {"marker_id": "pa_1", "question": "Send it?", "carried_forward": 2}
        state = evaluate_pending_action(
            self._fresh_marker_motet(marker), "conv-1", "hold on"
        )
        assert state.carry is None

    def test_stale_never_carries(self) -> None:
        marker = {"marker_id": "pa_1", "question": "Send it?", "carried_forward": 0}
        motet = _motet_with_rows(
            [_transcript_row(1, marker=marker, timestamp=time.time() - 7200)]
        )
        state = evaluate_pending_action(motet, "conv-1", "hold on")
        assert state.status == "stale"
        assert state.carry is None
        assert state.routing_hint["status"] == "stale"

    def test_no_marker_yields_none_hint(self) -> None:
        # Even when the latest assistant text ends with "?", pendingness is
        # marker-only: detection happened (or not) at write time.
        motet = _motet_with_rows([_transcript_row(1, assistant_text="Want me to post it?")])
        state = evaluate_pending_action(motet, "conv-1", "ok")
        assert state.marker is None
        assert state.routing_hint == {"status": "none"}

    def test_no_rows_yields_empty_state(self) -> None:
        state = evaluate_pending_action(_motet_with_rows([]), "conv-1", "ok")
        assert state.marker is None
        assert state.routing_hint == {"status": "none"}


# ---------------------------------------------------------------------------
# Marker reads (positional semantics + freshness)
# ---------------------------------------------------------------------------


def _transcript_row(
    sequence: int,
    *,
    assistant_text: Optional[str] = "done",
    marker: Optional[Dict[str, Any]] = None,
    root_turn: Optional[bool] = None,
    timestamp: Optional[float] = None,
) -> SimpleNamespace:
    items: List[Dict[str, Any]] = [
        {"_type": "message", "role": "user", "content": f"user turn {sequence}"}
    ]
    if assistant_text is not None:
        assistant: Dict[str, Any] = {
            "_type": "message",
            "role": "assistant",
            "content": assistant_text,
            "metadata": {"pending_action": marker} if marker else {},
        }
        items.append(assistant)
    metadata: Dict[str, Any] = {
        "sequence": sequence,
        "timestamp": timestamp if timestamp is not None else time.time(),
        "items": items,
    }
    if root_turn is not None:
        metadata["root_turn"] = root_turn
    return SimpleNamespace(metadata=metadata)


def _motet_with_rows(rows: List[SimpleNamespace]) -> SimpleNamespace:
    memory = SimpleNamespace(
        recall_conversation=lambda **kwargs: list(rows),
    )
    return SimpleNamespace(memory=memory, conversation_id="conv-1")


class TestLoadPendingAction:
    def test_marker_on_latest_root_assistant_is_fresh(self) -> None:
        marker = {"marker_id": "pa_1", "source": "heuristic", "question": "Send it?"}
        motet = _motet_with_rows(
            [_transcript_row(1), _transcript_row(2, assistant_text="Send it?", marker=marker)]
        )
        lookup = load_pending_action(motet, "conv-1")
        assert lookup.marker == marker
        assert lookup.status == "fresh"

    def test_old_timestamp_is_stale(self) -> None:
        marker = {"marker_id": "pa_1", "question": "Send it?"}
        motet = _motet_with_rows(
            [_transcript_row(1, marker=marker, timestamp=time.time() - 7200)]
        )
        lookup = load_pending_action(motet, "conv-1")
        assert lookup.status == "stale"

    def test_freshness_window_configurable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MOTET_PENDING_ACTION_FRESHNESS_SECONDS", "10000")
        marker = {"marker_id": "pa_1", "question": "Send it?"}
        motet = _motet_with_rows(
            [_transcript_row(1, marker=marker, timestamp=time.time() - 7200)]
        )
        assert load_pending_action(motet, "conv-1").status == "fresh"

    def test_newer_turn_buries_older_marker(self) -> None:
        marker = {"marker_id": "pa_1", "question": "Send it?"}
        motet = _motet_with_rows(
            [
                _transcript_row(1, assistant_text="Send it?", marker=marker),
                _transcript_row(2, assistant_text="Sent."),
            ]
        )
        lookup = load_pending_action(motet, "conv-1")
        assert lookup.marker is None
        assert lookup.status is None

    def test_sub_agent_rows_skipped(self) -> None:
        marker = {"marker_id": "pa_1", "question": "Send it?"}
        motet = _motet_with_rows(
            [
                _transcript_row(1, assistant_text="Send it?", marker=marker, root_turn=True),
                _transcript_row(2, assistant_text="sub-agent detail", root_turn=False),
            ]
        )
        lookup = load_pending_action(motet, "conv-1")
        assert lookup.marker == marker

    def test_no_rows_returns_empty(self) -> None:
        motet = _motet_with_rows([])
        assert load_pending_action(motet, "conv-1") == PendingActionLookup(None, None)

    def test_no_conversation_id_returns_empty(self) -> None:
        motet = _motet_with_rows([_transcript_row(1)])
        assert load_pending_action(motet, None) == PendingActionLookup(None, None)

    def test_recall_failure_falls_open(self) -> None:
        def _boom(**kwargs: Any) -> List[Any]:
            raise RuntimeError("memory unavailable")

        motet = SimpleNamespace(
            memory=SimpleNamespace(recall_conversation=_boom),
            conversation_id="conv-1",
        )
        assert load_pending_action(motet, "conv-1") == PendingActionLookup(None, None)


class TestPendingActionSystemMessage:
    _marker = {
        "marker_id": "pa_1",
        "question": "Should I send it?",
        "tool_shortlist": ["mcp.google_workspace.send_gmail_message"],
    }

    def test_confirm_instructs_to_proceed(self) -> None:
        text = build_pending_action_system_message(self._marker, "fresh", "confirm")
        assert "Should I send it?" in text
        assert "Proceed with the proposed action" in text
        assert "mcp.google_workspace.send_gmail_message" in text

    def test_decline_instructs_no_action(self) -> None:
        text = build_pending_action_system_message(self._marker, "fresh", "decline")
        assert "do not perform the action" in text

    def test_stale_requires_reconfirmation(self) -> None:
        text = build_pending_action_system_message(self._marker, "stale", "confirm")
        assert "Re-confirm" in text
        assert "do not execute it without fresh confirmation" in text

    def test_other_leaves_interpretation_to_model(self) -> None:
        text = build_pending_action_system_message(self._marker, "fresh", "other")
        assert "confirm, decline, amend, or defer" in text


# ---------------------------------------------------------------------------
# Writer integration (store_turn_transcript)
# ---------------------------------------------------------------------------


class _FakeMemory:
    def __init__(self) -> None:
        self.stored: List[Dict[str, Any]] = []

    def store(self, **kwargs: Any) -> Dict[str, Any]:
        self.stored.append(kwargs)
        return {"id": kwargs.get("item_id")}

    def store_memory(self, **kwargs: Any) -> Dict[str, Any]:
        return self.store(**kwargs)

    def recall_conversation(self, **kwargs: Any) -> List[Any]:
        return []


def _writer_motet(memory: _FakeMemory) -> SimpleNamespace:
    return SimpleNamespace(
        task_id="task-1",
        conversation_id="conv-1",
        memory=memory,
        redis=None,
        distributed_context=SimpleNamespace(metadata={}),
    )


def _stored_assistant_metadata(memory: _FakeMemory) -> Dict[str, Any]:
    assert memory.stored, "no transcript row stored"
    items = memory.stored[-1]["metadata"]["items"]
    assistants = [
        i for i in items if i.get("_type") == "message" and i.get("role") == "assistant"
    ]
    assert assistants, "no assistant item in stored transcript"
    return assistants[-1].get("metadata") or {}


class TestWriterAttachesMarker:
    @pytest.fixture(autouse=True)
    def _no_tool_memories(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            transcript_service,
            "parse_and_dedupe_tool_invocation_memories",
            lambda *a, **k: [],
        )

    def test_question_response_writes_heuristic_marker(self) -> None:
        memory = _FakeMemory()
        result = transcript_storage.store_turn_transcript(
            _writer_motet(memory),
            messages=[Message(role="user", content="send bob an email")],
            assistant_response="Here's the draft. Should I send it?",
            transcript_sequence=1,
        )
        assert result["canonical_transcript_stored"] is True
        marker = _stored_assistant_metadata(memory).get("pending_action")
        assert marker is not None
        assert marker["source"] == "heuristic"
        assert marker["question"] == "Here's the draft. Should I send it?"

    def test_statement_response_writes_no_marker(self) -> None:
        memory = _FakeMemory()
        transcript_storage.store_turn_transcript(
            _writer_motet(memory),
            messages=[Message(role="user", content="capital of france?")],
            assistant_response="Paris.",
            transcript_sequence=1,
        )
        assert "pending_action" not in _stored_assistant_metadata(memory)

    def test_deferral_carries_marker_forward(self) -> None:
        memory = _FakeMemory()
        carry = {"marker_id": "pa_1", "question": "Send it?", "carried_forward": 1}
        transcript_storage.store_turn_transcript(
            _writer_motet(memory),
            messages=[Message(role="user", content="hold on")],
            assistant_response="Sure, take your time.",
            transcript_sequence=2,
            pending_action_carry=carry,
        )
        assert _stored_assistant_metadata(memory).get("pending_action") == carry

    def test_fresh_question_wins_over_carry(self) -> None:
        memory = _FakeMemory()
        carry = {"marker_id": "pa_old", "question": "Old?", "carried_forward": 1}
        transcript_storage.store_turn_transcript(
            _writer_motet(memory),
            messages=[Message(role="user", content="change the subject line")],
            assistant_response="Updated. Should I send it now?",
            transcript_sequence=2,
            pending_action_carry=carry,
        )
        marker = _stored_assistant_metadata(memory).get("pending_action")
        assert marker is not None
        assert marker["marker_id"] != "pa_old"
        assert marker["question"] == "Updated. Should I send it now?"

    def test_sub_agent_turn_never_writes_marker(self) -> None:
        memory = _FakeMemory()
        transcript_storage.store_turn_transcript(
            _writer_motet(memory),
            messages=[Message(role="user", content="delegate")],
            assistant_response="Sub-agent asking: should I continue?",
            transcript_sequence=3,
            root_turn=False,
        )
        assert "pending_action" not in _stored_assistant_metadata(memory)


# ---------------------------------------------------------------------------
# Routing with the pending_action hint (conversation_analysis)
# ---------------------------------------------------------------------------


class TestRoutingWithPendingActionHint:
    """A pending marker disables the trivial skip unconditionally, with
    dedicated confirm/decline reasons for the routing counters (ADR-0121)."""

    def _route(self, user_text: str, hint: Dict[str, Any]) -> Dict[str, Any]:
        return _should_analyze_conversation(
            [_system("You are helpful."), _user(user_text)],
            pending_action=hint,
        )

    def test_fresh_confirm(self) -> None:
        result = self._route("ok", {"status": "fresh", "reply": "confirm"})
        assert result == {
            "full_analysis": False,
            "lightweight": True,
            "reason": "confirm_pending_action",
        }

    def test_fresh_decline(self) -> None:
        result = self._route("no", {"status": "fresh", "reply": "decline"})
        assert result["reason"] == "decline_pending_action"
        assert result["lightweight"] is True

    def test_fresh_other(self) -> None:
        result = self._route("ok thanks", {"status": "fresh", "reply": "other"})
        assert result["reason"] == "ack_to_pending_action"
        assert result["lightweight"] is True

    def test_stale_still_disables_skip(self) -> None:
        result = self._route("ok", {"status": "stale", "reply": "confirm"})
        assert result["reason"] == "stale_pending_action"
        assert result["lightweight"] is True

    def test_none_skips(self) -> None:
        # Nothing pending: the marker is the single source of truth, so a
        # trivial ack skips analysis.
        result = self._route("ok", {"status": "none"})
        assert result["reason"] == "simple_query"
        assert result["lightweight"] is False

    def test_no_hint_skips(self) -> None:
        result = _should_analyze_conversation([_system("You are helpful."), _user("ok")])
        assert result["reason"] == "simple_query"

    def test_hint_only_affects_trivial_messages(self) -> None:
        # Non-trivial replies route through the normal thresholds regardless.
        result = self._route(
            "wait, change the subject line first", {"status": "fresh", "reply": "other"}
        )
        assert result["reason"] in ("short_query", "moderate_query")


class TestEndsWithQuestion:
    """The writer's tail-question detection proxy (applied at write time
    only): the tail line decides, rhetorical mid-message questions do not."""

    def test_tail_question(self) -> None:
        assert ends_with_question("Done! Want me to post it? :)") is True

    def test_statement(self) -> None:
        assert ends_with_question("The capital of France is Paris.") is False

    def test_rhetorical_mid_message(self) -> None:
        assert ends_with_question("What does O(n) mean? It means linear.\nThat's it.") is False

    def test_empty_and_none(self) -> None:
        assert ends_with_question("") is False
        assert ends_with_question(None) is False
