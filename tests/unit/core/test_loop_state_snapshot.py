"""
Unit tests for LoopStateSnapshot codec (issue #147).

Verifies agent entry / recursion / suspend / resume field parity so new
AgenticLoopData fields cannot silently drift across those paths.
"""

from __future__ import annotations

from typing import Any, Dict

from motet.core.reasoning.react.agentic_loop_data import (
    AgenticLoopData,
)
from motet.core.reasoning.react.loop_state_snapshot import (
    LoopStateSnapshot,
)
from motet.core.checkpoints import TurnCheckpoint
from motet.core.types import Message


def _loop_data(**overrides: Any) -> AgenticLoopData:
    defaults: Dict[str, Any] = dict(
        input="schedule a reminder",
        conversation_history=[Message(role="user", content="schedule a reminder")],
        tools=[{"type": "function", "function": {"name": "core.help"}}],
        tool_filter_metadata={"prefix": ["core."]},
        executed_signatures=["core.help:abc"],
        stalled_iterations=1,
        observation_cache={"core.help:abc": {"tool_name": "core.help", "cache_control": {"no_store": True}, "stored_at": 1.0}},
        used_tool_names=["core.help"],
        max_iterations=8,
        remaining_iterations=5,
        max_model_calls=24,
        model_calls_used=3,
        max_cost_usd=0.20,
        max_prompt_tokens=80_000,
        max_tool_time_ms=60_000,
        stream_key="task:t1:response",
        max_tools=10,
        model_provider="anthropic",
        model_name="claude-sonnet-4",
        model_profile_name="default",
        temperature=0.2,
        enable_thinking=True,
        reasoning_effort="high",
        enable_prompt_caching=True,
        usage_accumulator={"input_tokens": 10, "output_tokens": 4},
        media_accumulator=[{"artifact_id": "img-1"}],
        handback_tool_names=["client.tool"],
        handback_tools=[{"type": "function", "function": {"name": "client.tool"}}],
        agent_id="core.chat",
        parent_agent_id="core.default",
    )
    defaults.update(overrides)
    return AgenticLoopData(**defaults)


def test_from_loop_data_round_trip_preserves_fields() -> None:
    data = _loop_data()
    restored = LoopStateSnapshot.from_loop_data(data).to_loop_data(
        conversation_history=data.conversation_history,
        stream_key=data.stream_key,
    )
    assert restored.input == data.input
    assert restored.remaining_iterations == data.remaining_iterations
    assert restored.max_model_calls == data.max_model_calls
    assert restored.model_calls_used == data.model_calls_used
    assert restored.max_cost_usd == data.max_cost_usd
    assert restored.max_prompt_tokens == data.max_prompt_tokens
    assert restored.max_tool_time_ms == data.max_tool_time_ms
    assert restored.used_tool_names == data.used_tool_names
    assert restored.handback_tool_names == data.handback_tool_names
    assert restored.agent_id == data.agent_id
    assert restored.parent_agent_id == data.parent_agent_id
    assert restored.inject_meta_tools is True
    assert restored.observation_cache == data.observation_cache
    assert restored.usage_accumulator == data.usage_accumulator
    assert restored.media_accumulator == data.media_accumulator
    assert restored.stream_key == data.stream_key
    assert restored.conversation_history == data.conversation_history


def test_recursion_override_decrements_remaining_iterations() -> None:
    data = _loop_data(remaining_iterations=4)
    next_data = LoopStateSnapshot.from_loop_data(
        data,
        remaining_iterations=data.remaining_iterations - 1,
        usage_accumulator={"input_tokens": 20},
        used_tool_names=["core.help", "core.tools_search"],
    ).to_loop_data(
        conversation_history=data.conversation_history,
        stream_key=data.stream_key,
    )
    assert next_data.remaining_iterations == 3
    assert next_data.usage_accumulator == {"input_tokens": 20}
    assert next_data.used_tool_names == ["core.help", "core.tools_search"]
    assert next_data.model_provider == "anthropic"


def test_checkpoint_round_trip_via_codec() -> None:
    data = _loop_data()
    loop_fields = LoopStateSnapshot.from_loop_data(
        data,
        usage_accumulator=dict(data.usage_accumulator or {}),
        media_accumulator=list(data.media_accumulator),
    ).to_checkpoint_loop_fields()

    checkpoint = TurnCheckpoint(
        motet_id="default",
        tenant_id="t1",
        principal_id="p1",
        handed_back_tool_calls=[
            {"tool_call_id": "c1", "tool_name": "client.tool", "parameters": {}}
        ],
        conversation_history=[
            m.model_dump(mode="json") for m in data.conversation_history
        ],
        **loop_fields,
    )

    resumed = LoopStateSnapshot.from_checkpoint(
        checkpoint,
        max_model_calls=30,
        model_calls_used=3,
        executed_signatures=["core.help:abc"],
    ).to_loop_data(
        conversation_history=[Message(role="user", content="schedule a reminder")],
        stream_key="task:resume:response",
    )

    assert resumed.input == "schedule a reminder"
    assert resumed.handback_tool_names == ["client.tool"]
    assert resumed.agent_id == "core.chat"
    assert resumed.parent_agent_id == "core.default"
    assert resumed.inject_meta_tools is True
    assert resumed.max_model_calls == 30
    assert resumed.model_calls_used == 3
    assert resumed.max_tool_time_ms == 60_000
    assert checkpoint.max_tool_time_ms == 60_000
    assert resumed.stream_key == "task:resume:response"
    assert resumed.tools is not None
    assert resumed.usage_accumulator == {"input_tokens": 10, "output_tokens": 4}
    assert resumed.observation_cache == data.observation_cache


def test_checkpoint_loop_fields_always_list_handback_names() -> None:
    fields = LoopStateSnapshot(input="x", handback_tool_names=None).to_checkpoint_loop_fields()
    assert fields["handback_tool_names"] == []
    assert fields["inject_meta_tools"] is True


def test_inject_meta_tools_false_survives_checkpoint() -> None:
    data = _loop_data(inject_meta_tools=False, agent_id=None)
    resumed = LoopStateSnapshot.from_loop_data(data).to_loop_data(
        conversation_history=data.conversation_history,
        stream_key=data.stream_key,
    )
    assert resumed.inject_meta_tools is False
    assert resumed.agent_id is None

    checkpoint = TurnCheckpoint(
        motet_id="default",
        tenant_id="t1",
        principal_id="p1",
        handed_back_tool_calls=[],
        conversation_history=[],
        **LoopStateSnapshot.from_loop_data(data).to_checkpoint_loop_fields(),
    )
    assert checkpoint.inject_meta_tools is False
    assert checkpoint.agent_id is None


def test_reasoning_effort_defaults_when_missing() -> None:
    data = LoopStateSnapshot(input="x", reasoning_effort=None).to_loop_data(
        conversation_history=[],
        stream_key="task:t:response",
    )
    assert data.reasoning_effort == "medium"


def test_with_fresh_budget_resets_counters_keeps_tools() -> None:
    """Issue #188 Continue policy vs ADR-0127 keep-budget resume."""
    snap = LoopStateSnapshot.from_loop_data(
        _loop_data(
            remaining_iterations=0,
            model_calls_used=24,
            stalled_iterations=2,
            usage_accumulator={"prompt_tokens": 99},
        )
    )
    fresh = snap.with_fresh_budget(max_iterations=20, max_model_calls=60)
    assert fresh.max_iterations == 20
    assert fresh.remaining_iterations == 20
    assert fresh.max_model_calls == 60
    assert fresh.model_calls_used == 0
    assert fresh.stalled_iterations == 0
    assert fresh.usage_accumulator is None
    assert fresh.used_tool_names == ["core.help"]
    assert fresh.executed_signatures == ["core.help:abc"]
    # Keep-budget resume must not use with_fresh_budget.
    kept = LoopStateSnapshot.from_checkpoint(
        TurnCheckpoint(**snap.to_checkpoint_loop_fields())
    )
    assert kept.remaining_iterations == 0
    assert kept.model_calls_used == 24
