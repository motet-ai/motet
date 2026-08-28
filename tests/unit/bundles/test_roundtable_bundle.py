"""
Motet - Unit tests for the roundtable example bundle

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
Unit tests for the roundtable bundle, which demonstrates runtime speaker
selection via agents.turn. The interesting surface is not the happy path — it
is the guards. An invite that recurses into the facilitator, or one that
accepts an agent id the registry cannot resolve, turns a panel into a hang or
an empty round. These tests also pin the transcript contract that makes rounds
work: the round counter advances per speaker, and an invited agent is briefed
with what was already said.

Dependencies:
- pytest
- motet_sdk.testing.MockMotetContext: SDK test double for MotetContext
- _roundtable_test_loader: canonical-name bundle module loading

Usage:
  pytest tests/unit/bundles/test_roundtable_bundle.py -q

Notes:
- No Redis, agent registry, or LLM is involved: agents is injected as a mock
  through MockMotetContext and get_motet_context is monkeypatched. The
  transcript store falls back to a process-local dict when redis is absent,
  which is cleared between tests.
"""

from __future__ import annotations

import sys
from unittest.mock import Mock

import pytest

from motet_sdk.testing import MockMotetContext

from _roundtable_test_loader import load_tool_module


@pytest.fixture(scope="module")
def invite_mod():
    return load_tool_module("invite")


@pytest.fixture(scope="module")
def roster_mod():
    return load_tool_module("roster")


@pytest.fixture(scope="module")
def transcript_mod():
    return load_tool_module("transcript")


@pytest.fixture(scope="module")
def store_mod(invite_mod, transcript_mod):
    """The store instance the tools actually share, not a fresh copy.

    Loading ``_transcript`` through the loader would re-exec it under the same
    name and give the test a different ``_FALLBACK_STORE`` than the one the
    tools write to, so take the module the relative imports already bound.
    """
    return sys.modules["bundle.roundtable.tools._transcript"]


@pytest.fixture(autouse=True)
def _clear_store(store_mod):
    """Transcript falls back to a process-local dict without redis."""
    store_mod.clear_fallback_store()
    yield
    store_mod.clear_fallback_store()


def _agents(turn_text: str = "A considered reply.", known: bool = True) -> Mock:
    agents = Mock()
    agents.get = Mock(return_value=object() if known else None)
    agents.turn = Mock(return_value={"final_response": turn_text})
    return agents


def _bind(mod, agents: Mock) -> MockMotetContext:
    """Point a tool module's get_motet_context at a mock context."""
    ctx = MockMotetContext(agents=agents)
    mod.get_motet_context = lambda: ctx
    return ctx


# --- guards ---------------------------------------------------------------


def test_invite_refuses_to_invite_the_facilitator(invite_mod):
    """Inviting the chair is a recursion, not a round."""
    _bind(invite_mod, _agents())

    result = invite_mod.invite(
        {"agent_id": "roundtable.facilitator", "question": "What do you think?"}
    )

    assert result["status"] == "error"
    assert "Cannot invite the facilitator" in result["error"]


def test_invite_rejects_an_agent_the_registry_cannot_resolve(invite_mod):
    """A hallucinated id must fail loudly rather than run an empty turn."""
    agents = _agents(known=False)
    _bind(invite_mod, agents)

    result = invite_mod.invite({"agent_id": "roundtable.nobody", "question": "Q"})

    assert result["status"] == "error"
    assert "Unknown agent" in result["error"]
    agents.turn.assert_not_called()


def test_invite_reports_incomplete_when_the_agent_returns_no_text(invite_mod):
    """An empty reply is not a contribution; it must not enter the transcript."""
    agents = _agents(turn_text="")
    _bind(invite_mod, agents)

    result = invite_mod.invite({"agent_id": "roundtable.researcher", "question": "Q"})

    assert result["status"] == "incomplete"
    assert result["response"] == ""


# --- speaker selection and rounds ----------------------------------------


def test_invite_dispatches_the_chosen_agent(invite_mod):
    """Selection is whatever agent_id the facilitator passed."""
    agents = _agents()
    _bind(invite_mod, agents)

    result = invite_mod.invite(
        {"agent_id": "roundtable.practitioner", "question": "What does this cost?"}
    )

    assert result["status"] == "ok"
    assert agents.turn.call_args.args[0] == "roundtable.practitioner"
    assert result["response"] == "A considered reply."


def test_round_advances_per_speaker(invite_mod):
    """Re-inviting the same agent is round two; a new speaker starts at one."""
    _bind(invite_mod, _agents())

    first = invite_mod.invite({"agent_id": "roundtable.researcher", "question": "Q1"})
    other = invite_mod.invite({"agent_id": "roundtable.contrarian", "question": "Q2"})
    second = invite_mod.invite({"agent_id": "roundtable.researcher", "question": "Q3"})

    assert first["round"] == 1
    assert other["round"] == 1
    assert second["round"] == 2


def test_later_speakers_are_briefed_with_the_transcript(invite_mod):
    """The shared channel is what lets the contrarian argue with the researcher."""
    agents = _agents(turn_text="Evidence is mixed.")
    _bind(invite_mod, agents)

    invite_mod.invite({"agent_id": "roundtable.researcher", "question": "What is known?"})
    invite_mod.invite({"agent_id": "roundtable.contrarian", "question": "What is assumed?"})

    prompt = agents.turn.call_args.kwargs["messages"][0]["content"]
    assert "Evidence is mixed." in prompt
    assert "What is assumed?" in prompt


def test_transcript_briefing_can_be_disabled(invite_mod):
    """include_transcript=false gives a clean-room opinion."""
    agents = _agents(turn_text="Prior view.")
    _bind(invite_mod, agents)

    invite_mod.invite({"agent_id": "roundtable.researcher", "question": "Q1"})
    invite_mod.invite(
        {
            "agent_id": "roundtable.contrarian",
            "question": "Q2",
            "include_transcript": False,
        }
    )

    prompt = agents.turn.call_args.kwargs["messages"][0]["content"]
    assert prompt == "Q2"
    assert "Prior view." not in prompt


# --- transcript readback --------------------------------------------------


def test_transcript_returns_recorded_turns(invite_mod, transcript_mod):
    """The facilitator synthesizes from this, so it must reflect who spoke."""
    agents = _agents()
    ctx = _bind(invite_mod, agents)
    transcript_mod.get_motet_context = lambda: ctx

    invite_mod.invite(
        {"agent_id": "roundtable.researcher", "question": "Q1", "topic": "four-day week"}
    )
    invite_mod.invite({"agent_id": "roundtable.practitioner", "question": "Q2"})

    result = transcript_mod.transcript({})

    assert result["topic"] == "four-day week"
    assert result["turn_count"] == 2
    assert result["speakers"] == ["roundtable.practitioner", "roundtable.researcher"]
    assert "researcher" in result["markdown"]


# --- roster ---------------------------------------------------------------


def test_roster_excludes_the_facilitator(roster_mod):
    """The chair should never appear in its own list of invitable agents."""
    agents = Mock()
    agents.list = Mock(
        return_value=[
            {"agent_id": "roundtable.facilitator", "description": "Chairs"},
            {"agent_id": "roundtable.researcher", "description": "Evidence"},
        ]
    )
    roster_mod.get_motet_context = lambda: MockMotetContext(agents=agents)

    result = roster_mod.roster({})

    assert [a["agent_id"] for a in result["agents"]] == ["roundtable.researcher"]
    assert result["count"] == 1


def test_roster_can_scope_to_one_bundle(roster_mod):
    """Panels may seat outside agents, so filtering has to be available."""
    agents = Mock()
    agents.list = Mock(
        return_value=[
            {"agent_id": "roundtable.researcher", "description": "Evidence"},
            {"agent_id": "expert-panel.skeptic", "description": "Risks"},
        ]
    )
    roster_mod.get_motet_context = lambda: MockMotetContext(agents=agents)

    assert roster_mod.roster({})["count"] == 2
    scoped = roster_mod.roster({"bundle": "roundtable"})
    assert [a["agent_id"] for a in scoped["agents"]] == ["roundtable.researcher"]
