"""
Motet - Turn Suspension and Resume Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Unit tests for the turn suspension/resume primitive (ADR-0127):
    - TurnCheckpoint Redis store: write with TTL + tool_call_id index,
      non-consuming load round-trip, expired/missing lookups, nested v1
      storage parity.
    - agentic_loop suspension trigger (_maybe_suspend_turn): returns a handback
      intent; Turn Runtime materialize_intent writes the checkpoint (ADR-0134).
    - mixed-turn execute-at-resume (issue #159): whole-turn handback at
      suspend; at resume the client covers only externally-owned ids, Motet
      executes its own handed-back calls before loop re-entry, with a
      synthetic error-observation backstop.
    - agent_turn suspended gate (_suspended_turn_response): skips finalize
      path, emits suspended-terminal stream event, response shape.
    - resume_turn: returns TurnResult (ADR-0134); handle resolution, principal
      re-authorization, forged / missing / duplicate observation rejection,
      history authority, loop re-entry state restoration, conversation
      rebinding to the checkpoint id. Resume-only keys live on ``.payload``.
    - handback tool schemas (ADR-0125 §5c.1): name union, per-iteration
      schema injection with client-wins collision handling (and shadow
      logging), suspension triggered by schema names, checkpointed
      handback_tools / agent_id.
    - finalize-on-resume (ADR-0127 transcript semantics): completed resumed
      turns run the recorded agent's core.finalize_turn hook exactly once;
      re-suspended, agent-less, and hook-disabled turns skip it.
    - resume response contract: resume_agent_turn's real return shape keeps the
      keys the OpenAI facade branches on (stop_reason/suspended/outcome), so a
      re-suspended resume still hands tool calls back instead of degrading to a
      text completion. Facade tests stub the command boundary and cannot see this.

Usage:
    pytest tests/unit/core/test_turn_suspension.py
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from motet.core.reasoning.react.agentic_loop import (
    _ensure_handback_tools_in_schemas,
    _handback_tool_names,
    _maybe_suspend_turn,
)
from motet.core.reasoning.react.loop_execution import (
    _generate_tool_signature,
    derive_executed_signatures,
)
from motet.core.reasoning.react.agentic_loop_data import (
    AgenticLoopData,
)
from motet.core.orchestration.turn.runtime.resume import (
    ResumeTurnData,
    _bind_resume_conversation,
    _validate_observations,
    build_resume_history,
    resume_turn,
)
from motet.core.orchestration.turn.runtime.result import TurnResult, TurnResultKind
from motet.core.checkpoints import (
    CHECKPOINT_SCHEMA_VERSION,
    TURN_CHECKPOINT_TTL_SECONDS,
    TurnCheckpoint,
    find_checkpoint_id_by_tool_call,
    load_turn_checkpoint,
    resolve_resume_checkpoint,
    store_turn_checkpoint,
)
from motet.core.orchestration.turn import _suspended_turn_response
from motet.core.orchestration.turn.runtime import materialize_intent
from motet.core.reasoning.react.loop_intents import is_turn_intent
from motet.core.types import Message

REDIS_MANAGER = "motet.core.distributed.redis_manager"
LOOP_MODULE = "motet.core.reasoning.react.agentic_loop"
LOOP_ITERATE = "motet.core.reasoning.react.agentic_loop.agentic_loop"
CHECKPOINT_MODULE = "motet.core.checkpoints"
RESUME_MODULE = "motet.core.orchestration.turn.runtime.resume"
RESUME_TURN_FN = "motet.core.orchestration.turn.runtime.resume_turn"


def _payload(result: Any) -> Dict[str, Any]:
    """Loop dict from typed resume_turn (or a raw dict if a test patched one)."""
    if isinstance(result, TurnResult):
        return result.payload
    return result


def _mock_motet(**overrides: Any) -> MagicMock:
    motet = MagicMock()
    motet.motet_id = overrides.get("motet_id", "default")
    motet.tenant_id = overrides.get("tenant_id", "tenant-a")
    motet.principal_id = overrides.get("principal_id", "user-1")
    motet.task_id = overrides.get("task_id", "task-1")
    motet.stream_key = overrides.get("stream_key", f"task:{motet.task_id}:response")
    motet.conversation_id = overrides.get("conversation_id", "conv-1")
    # No command context by default: _metadata_agent_id resolves to None.
    motet._command = overrides.get("command", None)
    return motet


def _checkpoint(**overrides: Any) -> TurnCheckpoint:
    defaults: Dict[str, Any] = dict(
        motet_id="default",
        tenant_id="tenant-a",
        principal_id="user-1",
        conversation_id="conv-1",
        handed_back_tool_calls=[
            {"tool_call_id": "call_1", "tool_name": "get_weather", "parameters": {"city": "SF"}},
            {"tool_call_id": "call_2", "tool_name": "get_news", "parameters": {}},
        ],
        conversation_history=[
            {"role": "user", "content": "weather and news?"},
            {"role": "assistant", "content": "", "tool_calls": []},
        ],
        input="weather and news?",
        max_iterations=10,
        remaining_iterations=7,
        max_model_calls=30,
        model_calls_used=2,
        used_tool_names=["get_weather", "get_news"],
        executed_signatures=["sig1"],
        stalled_iterations=2,
        usage_accumulator={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        handback_tool_names=["get_weather", "get_news"],
    )
    defaults.update(overrides)
    return TurnCheckpoint(**defaults)


# --- TurnCheckpoint store ------------------------------------------------------


class TestTurnCheckpointStore:
    def test_store_writes_checkpoint_and_index_with_ttl(self) -> None:
        stored: Dict[str, Any] = {}

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        client = MagicMock()
        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=client):
            checkpoint = _checkpoint()
            returned_id = store_turn_checkpoint(checkpoint)

        assert returned_id == checkpoint.checkpoint_id
        cp_key = f"tenant-a:turn_checkpoint:tenant-a:default:{checkpoint.checkpoint_id}"
        assert cp_key in stored
        blob = stored[cp_key]
        assert blob["schema_version"] == CHECKPOINT_SCHEMA_VERSION
        assert blob["loop_state"]["remaining_iterations"] == 7
        assert blob["identity"]["tenant_id"] == "tenant-a"
        assert blob["handback"]["handed_back_tool_calls"][0]["tool_call_id"] == "call_1"
        # Index entries for both handed-back tool_call_ids.
        for call_id in ("call_1", "call_2"):
            index_key = f"tenant-a:turn_checkpoint:index:tenant-a:default:{call_id}"
            assert stored[index_key] == {"checkpoint_id": checkpoint.checkpoint_id}
        # TTL applied to checkpoint + both index keys.
        expired_keys = [c.args[0] for c in client.expire.call_args_list]
        assert cp_key in expired_keys
        assert all(c.args[1] == TURN_CHECKPOINT_TTL_SECONDS for c in client.expire.call_args_list)
        assert len(expired_keys) == 3

    def test_store_failure_raises(self) -> None:
        with patch(
            f"{REDIS_MANAGER}.store_structured_data_sync",
            side_effect=ConnectionError("redis down"),
        ), patch(f"{REDIS_MANAGER}.get_sync_redis_client"):
            with pytest.raises(RuntimeError, match="failed to persist"):
                store_turn_checkpoint(_checkpoint())

    def test_budget_continue_store_writes_conversation_index(self) -> None:
        from motet.core.checkpoints import (
            CheckpointKind,
            find_latest_checkpoint_for_conversation,
        )

        stored: Dict[str, Any] = {}

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        def fake_retrieve(service: str, key: str, format_type: str = "hash") -> Any:
            return stored.get(key)

        client = MagicMock()
        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=fake_retrieve), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=client):
            checkpoint = _checkpoint(
                checkpoint_id="budget-abc",
                checkpoint_kind=CheckpointKind.BUDGET_CONTINUE,
                budget_stop_reason="max_iterations",
                handed_back_tool_calls=[],
            )
            store_turn_checkpoint(checkpoint)
            loaded = find_latest_checkpoint_for_conversation(
                tenant_id="tenant-a",
                motet_id="default",
                conversation_id="conv-1",
                kind=CheckpointKind.BUDGET_CONTINUE,
            )

        assert loaded is not None
        assert loaded.checkpoint_id == "budget-abc"
        assert loaded.checkpoint_kind == CheckpointKind.BUDGET_CONTINUE
        assert loaded.budget_stop_reason == "max_iterations"
        index_key = "tenant-a:turn_checkpoint:by_conversation:tenant-a:default:conv-1:budget_continue"
        assert stored[index_key] == {"checkpoint_id": "budget-abc"}

    def test_load_round_trip_is_non_consuming(self) -> None:
        checkpoint = _checkpoint()
        payload = checkpoint.to_storage_dict()
        retrieve = MagicMock(return_value=payload)
        with patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", retrieve):
            first = load_turn_checkpoint(
                tenant_id="tenant-a", motet_id="default", checkpoint_id=checkpoint.checkpoint_id
            )
            second = load_turn_checkpoint(
                tenant_id="tenant-a", motet_id="default", checkpoint_id=checkpoint.checkpoint_id
            )
        assert first is not None and second is not None
        assert first.checkpoint_id == second.checkpoint_id == checkpoint.checkpoint_id
        assert first.handed_back_tool_calls == checkpoint.handed_back_tool_calls
        assert first.usage_accumulator == checkpoint.usage_accumulator
        assert first.remaining_iterations == 7
        assert first.schema_version == CHECKPOINT_SCHEMA_VERSION
        # Non-consuming: both reads hit the same key, no deletes issued.
        assert retrieve.call_count == 2

    def test_load_accepts_legacy_flat_blob(self) -> None:
        """Pre-#157 flat Redis blobs still deserialize (dual-read)."""
        checkpoint = _checkpoint()
        flat = checkpoint.model_dump(mode="json")
        flat.pop("schema_version", None)
        with patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", return_value=flat):
            loaded = load_turn_checkpoint(
                tenant_id="tenant-a",
                motet_id="default",
                checkpoint_id=checkpoint.checkpoint_id,
            )
        assert loaded is not None
        assert loaded.remaining_iterations == 7
        assert loaded.handed_back_tool_calls == checkpoint.handed_back_tool_calls

    def test_storage_sections_cover_all_model_fields(self) -> None:
        """Every TurnCheckpoint field must map to a storage section.

        ``to_storage_dict`` writes only the fields named in the section tuples;
        a model field missing from all of them would silently vanish across a
        suspend/resume round trip.
        """
        from motet.core.checkpoints.checkpoint import (
            _CHECKPOINT_EXTRAS,
            _HANDBACK_FIELDS,
            _IDENTITY_FIELDS,
            _LOOP_STATE_FIELDS,
        )

        top_level = {"schema_version", "checkpoint_id", "created_at"} | set(
            _CHECKPOINT_EXTRAS
        )
        covered = (
            set(_IDENTITY_FIELDS)
            | set(_LOOP_STATE_FIELDS)
            | set(_HANDBACK_FIELDS)
            | top_level
        )
        assert covered == set(TurnCheckpoint.model_fields)

    def test_resolve_resume_checkpoint_loads_via_index(self) -> None:
        checkpoint = _checkpoint()

        def retrieve(service: str, key: str, format_type: str = "hash"):
            if ":index:" in key:
                return {"checkpoint_id": checkpoint.checkpoint_id}
            return checkpoint.to_storage_dict()

        with patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=retrieve):
            loaded = resolve_resume_checkpoint(
                tenant_id="tenant-a",
                motet_id="default",
                tool_call_ids=["call_1"],
            )
        assert loaded is not None
        assert loaded.checkpoint_id == checkpoint.checkpoint_id

    def test_load_missing_returns_none(self) -> None:
        with patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", return_value=None):
            assert (
                load_turn_checkpoint(
                    tenant_id="tenant-a", motet_id="default", checkpoint_id="suspend-gone"
                )
                is None
            )

    def test_index_lookup_resolves_checkpoint_id(self) -> None:
        with patch(
            f"{REDIS_MANAGER}.retrieve_structured_data_sync",
            return_value={"checkpoint_id": "suspend-abc"},
        ):
            assert (
                find_checkpoint_id_by_tool_call(
                    tenant_id="tenant-a", motet_id="default", tool_call_id="call_1"
                )
                == "suspend-abc"
            )

    def test_index_lookup_missing_returns_none(self) -> None:
        with patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", return_value=None):
            assert (
                find_checkpoint_id_by_tool_call(
                    tenant_id="tenant-a", motet_id="default", tool_call_id="call_x"
                )
                is None
            )


# --- agentic_loop suspension trigger -------------------------------------------


def _loop_data(**overrides: Any) -> AgenticLoopData:
    defaults: Dict[str, Any] = dict(
        input="weather and news?",
        conversation_history=[
            Message(role="user", content="weather and news?"),
            Message(role="assistant", content=""),
        ],
        max_iterations=10,
        remaining_iterations=8,
        max_model_calls=30,
        model_calls_used=0,
        stream_key="task:task-1:response",
        handback_tool_names=["get_weather"],
        used_tool_names=["earlier_tool"],
        executed_signatures=["sig-old"],
    )
    defaults.update(overrides)
    return AgenticLoopData(**defaults)


UNIQUE_CALLS: List[Dict[str, Any]] = [
    {"tool_call_id": "call_1", "tool_name": "get_weather", "parameters": {"city": "SF"}},
    {"tool_call_id": "call_2", "tool_name": "core.web_search", "parameters": {"q": "news"}},
]


def _materialize_suspend(
    motet: Any,
    data: AgenticLoopData,
    unique_calls: List[Dict[str, Any]],
    content: str,
    iterations: int,
    usage: Dict[str, Any],
    media: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Intent from the loop, then Turn Runtime writes the checkpoint."""
    intent = _maybe_suspend_turn(
        motet, data, unique_calls, content, iterations, usage, media
    )
    if intent is None:
        return None
    return materialize_intent(motet, data, intent)


class TestMaybeSuspendTurn:
    def test_no_suspend_names_returns_none(self) -> None:
        result = _maybe_suspend_turn(
            _mock_motet(), _loop_data(handback_tool_names=None), UNIQUE_CALLS,
            "", 3, {}, [],
        )
        assert result is None

    def test_handback_returns_intent_without_storing(self) -> None:
        with patch(f"{CHECKPOINT_MODULE}.store_turn_checkpoint") as store:
            result = _maybe_suspend_turn(
                _mock_motet(), _loop_data(), UNIQUE_CALLS, "", 3, {}, [],
            )
        store.assert_not_called()
        assert is_turn_intent(result)
        assert result["__turn_intent__"] == "handback"

    def test_no_externally_owned_call_returns_none(self) -> None:
        result = _maybe_suspend_turn(
            _mock_motet(), _loop_data(handback_tool_names=["client.tool"]), UNIQUE_CALLS,
            "", 3, {}, [],
        )
        assert result is None

    def test_suspends_and_hands_back_whole_turn(self) -> None:
        """Mixed turn: ALL calls handed back — a partial execution would fork the transcript."""
        motet = _mock_motet()
        captured: Dict[str, TurnCheckpoint] = {}

        def fake_store(checkpoint: TurnCheckpoint) -> str:
            captured["cp"] = checkpoint
            return checkpoint.checkpoint_id

        usage = {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}
        with patch(f"{CHECKPOINT_MODULE}.store_turn_checkpoint", side_effect=fake_store):
            result = _materialize_suspend(
                motet, _loop_data(), UNIQUE_CALLS,
                "checking...", 3, usage, [{"artifact_id": "img-1"}],
            )

        assert result is not None
        assert result["stop_reason"] == "suspended"
        assert result["suspended"] is True
        assert result["final_response"] == "checking..."
        assert result["usage"] == usage
        assert result["media"] == [{"artifact_id": "img-1"}]
        assert result["tool_results"] == []
        # Whole turn handed back, in assistant-message order.
        assert [c["tool_call_id"] for c in result["handed_back_tool_calls"]] == ["call_1", "call_2"]

        cp = captured["cp"]
        assert result["checkpoint_id"] == cp.checkpoint_id
        assert cp.tenant_id == "tenant-a"
        assert cp.principal_id == "user-1"
        # Same-iteration handback: remaining_iterations is not decremented.
        assert cp.remaining_iterations == 8
        assert cp.model_calls_used == 0  # caller increments before suspend in loop
        assert set(cp.used_tool_names) == {"earlier_tool", "get_weather", "core.web_search"}
        assert cp.executed_signatures == ["sig-old"]
        assert cp.usage_accumulator == usage
        assert cp.media_accumulator == [{"artifact_id": "img-1"}]
        assert cp.handback_tool_names == ["get_weather"]
        # History checkpointed ending with the assistant message.
        assert cp.conversation_history is not None
        assert cp.conversation_history[-1]["role"] == "assistant"
        # Loop announces the suspension on the task stream.
        events = [c.args[0] for c in motet.stream_event.call_args_list]
        assert "agentic_loop_suspended" in events

    def test_mixed_turn_hands_back_whole_turn_without_executing(self) -> None:
        """Issue #159: even on a mixed Motet + client turn, suspend executes
        nothing — Motet's subset runs at resume, keeping the wire assistant
        message complete."""
        captured: Dict[str, TurnCheckpoint] = {}
        data = _loop_data()

        def fake_store(checkpoint: TurnCheckpoint) -> str:
            captured["cp"] = checkpoint
            return checkpoint.checkpoint_id

        exec_module = "motet.core.reasoning.react.loop_execution"
        with patch(f"{CHECKPOINT_MODULE}.store_turn_checkpoint", side_effect=fake_store), patch(
            f"{exec_module}.execute_tools_and_append_results"
        ) as mock_execute:
            intent = _maybe_suspend_turn(
                _mock_motet(), data, UNIQUE_CALLS, "", 3, {}, []
            )
            result = materialize_intent(_mock_motet(), data, intent)

        assert intent is not None and is_turn_intent(intent)
        assert not mock_execute.called
        assert [c["tool_call_id"] for c in result["handed_back_tool_calls"]] == ["call_1", "call_2"]
        assert data.conversation_history[-1].role == "assistant"

    def test_same_iteration_handback_preserves_remaining(self) -> None:
        """Suspend keeps remaining_iterations even when only one Motet iteration left."""
        captured: Dict[str, TurnCheckpoint] = {}

        def fake_store(checkpoint: TurnCheckpoint) -> str:
            captured["cp"] = checkpoint
            return checkpoint.checkpoint_id

        with patch(f"{CHECKPOINT_MODULE}.store_turn_checkpoint", side_effect=fake_store):
            result = _materialize_suspend(
                _mock_motet(),
                _loop_data(remaining_iterations=1, model_calls_used=4, max_model_calls=30),
                UNIQUE_CALLS,
                "",
                10,
                {},
                [],
            )
        assert result is not None and result["stop_reason"] == "suspended"
        assert captured["cp"].remaining_iterations == 1
        assert captured["cp"].model_calls_used == 4
        assert captured["cp"].max_model_calls == 30

    def test_handback_tool_schemas_trigger_suspension(self) -> None:
        """§5c.1: schemas in handback_tools imply handback names — no separate list needed."""
        captured: Dict[str, TurnCheckpoint] = {}

        def fake_store(checkpoint: TurnCheckpoint) -> str:
            captured["cp"] = checkpoint
            return checkpoint.checkpoint_id

        data = _loop_data(
            handback_tool_names=None,
            handback_tools=[{"name": "get_weather", "description": "", "json_schema": {}}],
        )
        with patch(f"{CHECKPOINT_MODULE}.store_turn_checkpoint", side_effect=fake_store):
            result = _materialize_suspend(_mock_motet(), data, UNIQUE_CALLS, "", 3, {}, [])

        assert result is not None and result["stop_reason"] == "suspended"
        # Schemas are checkpointed so the resumed turn keeps offering them.
        assert captured["cp"].handback_tools == [
            {"name": "get_weather", "description": "", "json_schema": {}}
        ]

    def test_checkpoint_records_agent_id_from_command_metadata(self) -> None:
        """agent_id (ADR-0083 metadata) is captured for finalize-on-resume."""
        from types import SimpleNamespace

        command = SimpleNamespace(
            distributed_context=SimpleNamespace(metadata={"agent_id": "core.default"})
        )
        captured: Dict[str, TurnCheckpoint] = {}

        def fake_store(checkpoint: TurnCheckpoint) -> str:
            captured["cp"] = checkpoint
            return checkpoint.checkpoint_id

        with patch(f"{CHECKPOINT_MODULE}.store_turn_checkpoint", side_effect=fake_store):
            result = _materialize_suspend(
                _mock_motet(command=command), _loop_data(), UNIQUE_CALLS, "", 3, {}, [],
            )
        assert result is not None
        assert captured["cp"].agent_id == "core.default"

    def test_checkpoint_prefers_loop_carried_agent_id(self) -> None:
        """Loop-carried agent_id wins when resume→agentic_loop has empty metadata."""
        captured: Dict[str, TurnCheckpoint] = {}

        def fake_store(checkpoint: TurnCheckpoint) -> str:
            captured["cp"] = checkpoint
            return checkpoint.checkpoint_id

        with patch(f"{CHECKPOINT_MODULE}.store_turn_checkpoint", side_effect=fake_store):
            result = _materialize_suspend(
                _mock_motet(),
                _loop_data(agent_id="cursor.backend"),
                UNIQUE_CALLS,
                "",
                3,
                {},
                [],
            )
        assert result is not None
        assert captured["cp"].agent_id == "cursor.backend"


class TestHandbackToolSchemas:
    """Externally-owned schema injection into the model tool list (ADR-0125 §5c.1)."""

    def test_names_union_explicit_and_schema_names(self) -> None:
        data = _loop_data(
            handback_tool_names=["explicit_tool"],
            handback_tools=[{"name": "Shell", "description": "", "json_schema": {}}],
        )
        assert _handback_tool_names(data) == {"explicit_tool", "Shell"}

    def test_injection_appends_missing_schemas(self) -> None:
        existing = [{"name": "core.web_search", "description": "", "json_schema": {}}]
        handback = [{"name": "Shell", "description": "run", "json_schema": {}}]
        merged = _ensure_handback_tools_in_schemas(existing, handback)
        assert [t["name"] for t in merged] == ["core.web_search", "Shell"]

    def test_injection_is_idempotent_and_handback_wins_on_collision(self) -> None:
        """Ownership rule: a client-declared name replaces the registry schema."""
        registry_schema = {"name": "Shell", "description": "motet shell", "json_schema": {"a": 1}}
        client_schema = {"name": "Shell", "description": "client shell", "json_schema": {"b": 2}}
        merged = _ensure_handback_tools_in_schemas([registry_schema], [client_schema])
        assert merged == [client_schema]
        # Re-running injection (every iteration) does not duplicate.
        assert _ensure_handback_tools_in_schemas(merged, [client_schema]) == [client_schema]

    def test_no_handback_tools_is_passthrough(self) -> None:
        existing = [{"name": "core.web_search", "description": "", "json_schema": {}}]
        assert _ensure_handback_tools_in_schemas(existing, None) == existing

    def test_collision_logs_shadowed_registry_tool(self) -> None:
        registry_schema = {"name": "Shell", "description": "motet shell", "json_schema": {}}
        client_schema = {"name": "Shell", "description": "client shell", "json_schema": {}}
        with patch(f"{LOOP_MODULE}.logger") as mock_logger:
            _ensure_handback_tools_in_schemas([registry_schema], [client_schema])
        mock_logger.info.assert_called_once_with(
            "agentic_loop_handback_tool_shadows_registry_tool",
            shadowed_tool_names=["Shell"],
        )

    def test_no_collision_logs_nothing(self) -> None:
        existing = [{"name": "core.web_search", "description": "", "json_schema": {}}]
        handback = [{"name": "Shell", "description": "run", "json_schema": {}}]
        with patch(f"{LOOP_MODULE}.logger") as mock_logger:
            _ensure_handback_tools_in_schemas(existing, handback)
        mock_logger.info.assert_not_called()


# --- agent_turn suspended gate ---------------------------------------------------


class TestSuspendedTurnResponse:
    SUSPENDED_PAYLOAD: Dict[str, Any] = {
        "stop_reason": "suspended",
        "suspended": True,
        "checkpoint_id": "suspend-abc",
        "handed_back_tool_calls": [{"tool_call_id": "call_1", "tool_name": "get_weather", "parameters": {}}],
        "final_response": "on it",
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        "media": [],
        "tool_results": [],
        "iterations_used": 2,
    }

    def test_non_suspended_payload_returns_none(self) -> None:
        motet = _mock_motet()
        assert _suspended_turn_response(
            motet, {"stop_reason": "stop", "final_response": "done"},
            "agent", None, {}, {},
        ) is None
        assert _suspended_turn_response(motet, "not a dict", "agent", None, {}, {}) is None
        motet.stream_event.assert_not_called()

    def test_root_turn_emits_terminal_suspended_event(self) -> None:
        motet = _mock_motet()
        response = _suspended_turn_response(
            motet, dict(self.SUSPENDED_PAYLOAD), "agent", None,
            {"strategy": "react"}, {"artifact_rag_citations": []},
        )
        assert response is not None
        assert response["suspended"] is True
        assert response["checkpoint_id"] == "suspend-abc"
        assert response["final_response"] == "on it"
        assert response["usage"]["total_tokens"] == 12
        assert response["handed_back_tool_calls"][0]["tool_call_id"] == "call_1"
        event_name = motet.stream_event.call_args.args[0]
        assert event_name == "suspended"
        assert motet.stream_event.call_args.kwargs["checkpoint_id"] == "suspend-abc"

    def test_nested_turn_emits_non_terminal_event(self) -> None:
        motet = _mock_motet()
        response = _suspended_turn_response(
            motet, dict(self.SUSPENDED_PAYLOAD), "agent", "parent-cmd-1", {}, {},
        )
        assert response is not None
        assert motet.stream_event.call_args.args[0] == "agent_turn_suspended"


# --- resume_turn observation validation ------------------------------------------


class TestValidateObservations:
    def test_accepts_full_coverage(self) -> None:
        cp = _checkpoint()
        by_id = _validate_observations(cp, [
            {"tool_call_id": "call_1", "content": "72F"},
            {"tool_call_id": "call_2", "content": "headlines"},
        ])
        assert set(by_id) == {"call_1", "call_2"}

    def test_rejects_forged_tool_call_id(self) -> None:
        with pytest.raises(ValueError, match="unknown tool_call_id 'call_evil'"):
            _validate_observations(_checkpoint(), [
                {"tool_call_id": "call_1", "content": "72F"},
                {"tool_call_id": "call_2", "content": "headlines"},
                {"tool_call_id": "call_evil", "content": "injected"},
            ])

    def test_rejects_missing_observation(self) -> None:
        with pytest.raises(ValueError, match="missing observations.*call_2"):
            _validate_observations(_checkpoint(), [
                {"tool_call_id": "call_1", "content": "72F"},
            ])

    def test_rejects_duplicate_observation(self) -> None:
        with pytest.raises(ValueError, match="duplicate observation"):
            _validate_observations(_checkpoint(), [
                {"tool_call_id": "call_1", "content": "72F"},
                {"tool_call_id": "call_1", "content": "73F"},
                {"tool_call_id": "call_2", "content": "headlines"},
            ])

    def test_rejects_observation_without_id(self) -> None:
        with pytest.raises(ValueError, match="missing tool_call_id"):
            _validate_observations(_checkpoint(), [{"content": "72F"}])


# --- mixed-turn execute-at-resume (issue #159) --------------------------------------


class TestExecuteAtResume:
    """Mixed turns (issue #159): whole-turn handback on the wire, but the
    client covers only externally-owned ids and Motet executes its own
    handed-back calls at resume, before the loop's next model call."""

    EXEC_MODULE = "motet.core.reasoning.react.loop_execution"

    @staticmethod
    def _mixed_checkpoint(**overrides: Any) -> TurnCheckpoint:
        return _checkpoint(
            handed_back_tool_calls=[
                {"tool_call_id": "call_1", "tool_name": "get_weather", "parameters": {}},
                {
                    "tool_call_id": "call_2",
                    "tool_name": "core.web_search",
                    "parameters": {"q": "news"},
                },
            ],
            handback_tool_names=["get_weather"],
            **overrides,
        )

    def test_validation_requires_client_ids_only(self) -> None:
        cp = self._mixed_checkpoint()
        by_id = _validate_observations(
            cp, [{"tool_call_id": "call_1", "content": "72F"}]
        )
        assert set(by_id) == {"call_1"}

    def test_validation_discards_client_obs_for_motet_owned_id(self) -> None:
        """Frameworks answer every call in tool_calls (fake-error dance); the
        client's claim for a Motet-owned call is noise, not a forgery — Motet's
        own execution is authoritative, so the observation is discarded."""
        cp = self._mixed_checkpoint()
        by_id = _validate_observations(
            cp,
            [
                {"tool_call_id": "call_1", "content": "72F"},
                {"tool_call_id": "call_2", "content": "Error: unknown tool"},
            ],
        )
        assert set(by_id) == {"call_1"}

    def test_validation_still_requires_external_ids(self) -> None:
        cp = self._mixed_checkpoint()
        with pytest.raises(ValueError, match="missing observations.*call_1"):
            _validate_observations(cp, [])

    def test_build_resume_history_defers_motet_owned_calls(self) -> None:
        cp = self._mixed_checkpoint()
        history = build_resume_history(
            cp, None, [{"tool_call_id": "call_1", "content": "72F"}]
        )
        tool_msgs = [m for m in history if m.role == "tool"]
        assert [m.tool_call_id for m in tool_msgs] == ["call_1"]

    def _resume(
        self,
        cp: TurnCheckpoint,
        fake_execute: Any,
        observations: List[Dict[str, Any]],
        conversation_history: Optional[List[Any]] = None,
    ) -> tuple:
        motet = _mock_motet()
        loop_calls: List[Any] = []

        def fake_iterate(data: Any) -> Dict[str, Any]:
            loop_calls.append(data)
            return {"final_response": "done", "stop_reason": "stop"}

        with patch(f"{RESUME_MODULE}.get_motet_context", return_value=motet), \
             patch(f"{RESUME_MODULE}.load_turn_checkpoint", return_value=cp), \
             patch(f"{RESUME_MODULE}.find_checkpoint_id_by_tool_call", return_value=None), \
             patch(
                 f"{self.EXEC_MODULE}.execute_tools_and_append_results",
                 side_effect=fake_execute,
             ), \
             patch(LOOP_ITERATE, side_effect=fake_iterate), \
             patch(
                 "motet.core.distributed.task_control.is_cancelled",
                 return_value=False,
             ):
            result = resume_turn(
                ResumeTurnData(
                    checkpoint_id=cp.checkpoint_id,
                    observations=observations,
                    conversation_history=conversation_history,
                )
            )
        return result, loop_calls

    def test_resume_executes_motet_calls_before_loop(self) -> None:
        from motet.core.reasoning.react.loop_execution import ExecuteToolsResult

        cp = self._mixed_checkpoint()

        def fake_execute(calls, _esc, loop_data, *_args, **_kwargs):
            assert [c["tool_name"] for c in calls] == ["core.web_search"]
            loop_data.conversation_history.append(
                Message(
                    role="tool",
                    tool_call_id="call_2",
                    name="core.web_search",
                    content="search results",
                )
            )
            return ExecuteToolsResult(tool_results=[], auth_response=None, early_return=None)

        result, loop_calls = self._resume(
            cp, fake_execute, [{"tool_call_id": "call_1", "content": "72F"}]
        )
        assert _payload(result)["final_response"] == "done"
        history = loop_calls[0].conversation_history
        tool_msgs = [m for m in history if m.role == "tool"]
        assert [m.tool_call_id for m in tool_msgs] == ["call_1", "call_2"]
        assert tool_msgs[0].content == "72F"
        assert tool_msgs[1].content == "search results"
        # Issue #160: Motet-owned handback results ship for orchestration finalize.
        assert _payload(result)["motet_owned_tool_observations"] == [
            {
                "role": "tool",
                "tool_call_id": "call_2",
                "name": "core.web_search",
                "content": "search results",
            }
        ]

    def test_pure_client_resume_omits_motet_owned_tool_observations(self) -> None:
        """Pure externally-owned turns must not ship an empty finalize field."""
        cp = _checkpoint(
            handed_back_tool_calls=[
                {"tool_call_id": "call_1", "tool_name": "get_weather", "parameters": {}},
                {"tool_call_id": "call_2", "tool_name": "get_news", "parameters": {}},
            ],
            handback_tool_names=["get_weather", "get_news"],
        )

        def fake_execute(*_args, **_kwargs):
            raise AssertionError("Motet should not execute tools on pure-client resume")

        result, _ = self._resume(
            cp,
            fake_execute,
            [
                {"tool_call_id": "call_1", "content": "72F"},
                {"tool_call_id": "call_2", "content": "headlines"},
            ],
        )
        assert "motet_owned_tool_observations" not in _payload(result)

    def test_resume_history_appends_motet_owned_tool_observations(self) -> None:
        """Issue #160: finalize rebuild must include Motet-owned tool results."""
        from motet.core.orchestration.turn.resume_agent_turn import (
            ResumeAgentTurnData,
            _resume_history,
        )

        cp = self._mixed_checkpoint()
        data = ResumeAgentTurnData(
            checkpoint_id=cp.checkpoint_id,
            observations=[{"tool_call_id": "call_1", "content": "72F"}],
        )
        history = _resume_history(
            data,
            cp,
            motet_owned_tool_observations=[
                {
                    "role": "tool",
                    "tool_call_id": "call_2",
                    "name": "core.web_search",
                    "content": "search results",
                }
            ],
        )
        tool_msgs = [m for m in history if m.role == "tool"]
        assert [m.tool_call_id for m in tool_msgs] == ["call_1", "call_2"]
        assert tool_msgs[1].content == "search results"
        assert tool_msgs[1].name == "core.web_search"

    def test_backstop_observation_when_execution_yields_nothing(self) -> None:
        """A Motet call left unanswered would wedge the transcript (strict
        providers), and classic handback is no longer an option at this point —
        so an error observation is synthesized."""
        from motet.core.reasoning.react.loop_execution import ExecuteToolsResult

        cp = self._mixed_checkpoint()

        def fake_execute(*_args, **_kwargs):
            return ExecuteToolsResult(tool_results=[], auth_response=None, early_return=None)

        _, loop_calls = self._resume(
            cp, fake_execute, [{"tool_call_id": "call_1", "content": "72F"}]
        )
        tool_msgs = [m for m in loop_calls[0].conversation_history if m.role == "tool"]
        assert [m.tool_call_id for m in tool_msgs] == ["call_1", "call_2"]
        assert "failed" in tool_msgs[1].content

    def test_backstop_observation_when_execution_raises(self) -> None:
        cp = self._mixed_checkpoint()

        def fake_execute(*_args, **_kwargs):
            raise ConnectionError("redis down")

        _, loop_calls = self._resume(
            cp, fake_execute, [{"tool_call_id": "call_1", "content": "72F"}]
        )
        tool_msgs = [m for m in loop_calls[0].conversation_history if m.role == "tool"]
        assert [m.tool_call_id for m in tool_msgs] == ["call_1", "call_2"]
        assert "failed" in tool_msgs[1].content

    def test_caller_history_signatures_include_motet_calls(self) -> None:
        """Signatures are derived after Motet's own observations land, so the
        just-executed Motet call counts as executed and cannot be immediately
        re-requested."""
        from motet.core.reasoning.react.loop_execution import ExecuteToolsResult

        cp = self._mixed_checkpoint()
        external = [
            {"role": "user", "content": "weather and search?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls_canonical": [
                    {
                        "call_id": "call_1",
                        "tool_name": "get_weather",
                        "arguments_json": "{}",
                    },
                    {
                        "call_id": "call_2",
                        "tool_name": "core.web_search",
                        "arguments_json": '{"q": "news"}',
                        "arguments": {"q": "news"},
                    },
                ],
            },
        ]

        def fake_execute(calls, _esc, loop_data, *_args, **_kwargs):
            loop_data.conversation_history.append(
                Message(
                    role="tool",
                    tool_call_id="call_2",
                    name="core.web_search",
                    content="search results",
                )
            )
            return ExecuteToolsResult(tool_results=[], auth_response=None, early_return=None)

        _, loop_calls = self._resume(
            cp,
            fake_execute,
            [{"tool_call_id": "call_1", "content": "72F"}],
            conversation_history=external,
        )
        signatures = loop_calls[0].executed_signatures
        assert _generate_tool_signature("core.web_search", {"q": "news"}) in signatures
        assert _generate_tool_signature("get_weather", {}) in signatures


# --- executed-signature derivation (transcript-authoritative dedupe) --------------


class TestDeriveExecutedSignatures:
    @staticmethod
    def _canonical_call(name: str, params: Dict[str, Any], call_id: str) -> Dict[str, Any]:
        return {
            "call_id": call_id,
            "tool_name": name,
            "arguments": params,
            "arguments_json": json.dumps(params),
        }

    @staticmethod
    def _wire_call(name: str, params: Dict[str, Any], call_id: str) -> Dict[str, Any]:
        return {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(params)},
        }

    def test_empty_history_yields_nothing(self) -> None:
        assert derive_executed_signatures(None) == []
        assert derive_executed_signatures([]) == []

    def test_canonical_shape_hashes(self) -> None:
        """Motet stores canonical calls; leftover wire ``tool_calls`` is ignored (#225)."""
        params = {"path": "a.py", "offset": 10, "limit": 200}
        expected = [_generate_tool_signature("Read", params)]
        history = [
            {"role": "assistant", "content": "", "tool_calls_canonical": [self._canonical_call("Read", params, "c1")]},
            {"role": "tool", "tool_call_id": "c1", "content": "file body"},
        ]
        assert derive_executed_signatures(history) == expected

    def test_leftover_tool_calls_key_is_ignored(self) -> None:
        """Issue #225: leftover ChatCompletions ``tool_calls`` do not hash."""
        history = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [self._wire_call("Read", {"path": "a.py"}, "c1")],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "file body"},
        ]
        assert derive_executed_signatures(history) == []

    def test_call_without_result_is_not_executed(self) -> None:
        """The whole point: no visible observation means the model may ask again."""
        history = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls_canonical": [self._canonical_call("Read", {"path": "a.py"}, "c1")],
            },
        ]
        assert derive_executed_signatures(history) == []

    def test_provider_builtin_is_excluded(self) -> None:
        """Provider-executed builtins never entered executed_signatures."""
        history = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls_canonical": [
                    {"call_id": "c1", "tool_name": "web_search", "kind": "provider", "arguments": {}, "arguments_json": "{}"}
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "done"},
        ]
        assert derive_executed_signatures(history) == []

    def test_repeated_identical_call_yields_one_signature(self) -> None:
        history = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls_canonical": [self._canonical_call("Read", {"path": "a.py"}, "c1")],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "x"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls_canonical": [self._canonical_call("Read", {"path": "a.py"}, "c2")],
            },
            {"role": "tool", "tool_call_id": "c2", "content": "x"},
        ]
        assert derive_executed_signatures(history) == [
            _generate_tool_signature("Read", {"path": "a.py"})
        ]

    def test_accepts_message_models_not_only_dicts(self) -> None:
        history = [
            Message(
                role="assistant",
                content="",
                tool_calls_canonical=[self._canonical_call("Read", {"path": "a.py"}, "c1")],
            ),
            Message(role="tool", tool_call_id="c1", name="Read", content="x"),
        ]
        assert derive_executed_signatures(history) == [
            _generate_tool_signature("Read", {"path": "a.py"})
        ]


# --- resume_turn primitive -------------------------------------------------------


OBSERVATIONS: List[Dict[str, Any]] = [
    {"tool_call_id": "call_1", "content": "72F and sunny"},
    {"tool_call_id": "call_2", "content": {"headlines": ["a", "b"]}},
]


def _run_resume(
    data: ResumeTurnData,
    checkpoint: Optional[TurnCheckpoint],
    motet: Optional[MagicMock] = None,
    index_result: Optional[str] = None,
) -> tuple:
    """Invoke resume_turn with a patched context and checkpoint store."""
    motet = motet or _mock_motet()
    loop_calls: List[Any] = []

    def fake_iterate(data: Any) -> Dict[str, Any]:
        loop_calls.append(data)
        return {"final_response": "resumed answer", "stop_reason": "stop"}

    with patch(f"{RESUME_MODULE}.get_motet_context", return_value=motet), \
         patch(f"{RESUME_MODULE}.load_turn_checkpoint", return_value=checkpoint), \
         patch(f"{RESUME_MODULE}.find_checkpoint_id_by_tool_call", return_value=index_result), \
         patch(LOOP_ITERATE, side_effect=fake_iterate), \
         patch(
             "motet.core.distributed.task_control.is_cancelled",
             return_value=False,
         ):
        result = resume_turn(data)
    return result, loop_calls, motet


class TestResumeTurn:
    def test_unknown_checkpoint_raises(self) -> None:
        with pytest.raises(ValueError, match="not found or expired"):
            _run_resume(ResumeTurnData(checkpoint_id="suspend-gone", observations=OBSERVATIONS), None)

    def test_no_handle_raises(self) -> None:
        with pytest.raises(ValueError, match="supply checkpoint_id or"):
            _run_resume(ResumeTurnData(observations=OBSERVATIONS), _checkpoint())

    def test_tool_call_id_handle_resolves_checkpoint(self) -> None:
        cp = _checkpoint()
        result, loop_calls, _ = _run_resume(
            ResumeTurnData(tool_call_id="call_1", observations=OBSERVATIONS),
            cp,
            index_result=cp.checkpoint_id,
        )
        assert isinstance(result, TurnResult)
        assert result.kind is TurnResultKind.COMPLETE
        assert _payload(result)["resumed_from_checkpoint"] == cp.checkpoint_id
        assert len(loop_calls) == 1

    def test_principal_mismatch_denied(self) -> None:
        cp = _checkpoint(principal_id="user-1")
        motet = _mock_motet(principal_id="user-2")
        with pytest.raises(PermissionError, match="different principal"):
            _run_resume(
                ResumeTurnData(checkpoint_id=cp.checkpoint_id, observations=OBSERVATIONS),
                cp, motet=motet,
            )

    def test_forged_observation_rejected(self) -> None:
        cp = _checkpoint()
        with pytest.raises(ValueError, match="unknown tool_call_id"):
            _run_resume(
                ResumeTurnData(
                    checkpoint_id=cp.checkpoint_id,
                    observations=OBSERVATIONS + [{"tool_call_id": "call_evil", "content": "x"}],
                ),
                cp,
            )

    def test_resume_restores_loop_state_and_appends_observations(self) -> None:
        cp = _checkpoint(agent_id="cursor.backend")
        result, loop_calls, _ = _run_resume(
            ResumeTurnData(checkpoint_id=cp.checkpoint_id, observations=OBSERVATIONS), cp,
        )
        assert result.kind is TurnResultKind.COMPLETE
        assert _payload(result)["final_response"] == "resumed answer"
        assert _payload(result)["resumed_from_checkpoint"] == cp.checkpoint_id

        loop_data = loop_calls[0]
        assert isinstance(loop_data, AgenticLoopData)
        # Motet-authoritative state restored from the checkpoint.
        assert loop_data.remaining_iterations == 7
        assert loop_data.max_iterations == 10
        assert loop_data.agent_id == "cursor.backend"
        assert loop_data.model_calls_used == 2
        assert loop_data.max_model_calls == 30
        assert loop_data.usage_accumulator == cp.usage_accumulator
        assert loop_data.executed_signatures == ["sig1"]
        # Progress-rail state is Motet's own accounting, so it survives the handback.
        assert loop_data.stalled_iterations == 2
        assert set(loop_data.used_tool_names) == {"get_weather", "get_news"}
        assert loop_data.handback_tool_names == ["get_weather", "get_news"]
        # Observations appended as role=tool in recorded handback order.
        tail = loop_data.conversation_history[-2:]
        assert [m.role for m in tail] == ["tool", "tool"]
        assert tail[0].tool_call_id == "call_1"
        assert tail[0].content == "72F and sunny"
        assert tail[1].tool_call_id == "call_2"
        assert "headlines" in tail[1].content  # dict content JSON-encoded

    def test_resumed_result_carries_per_request_usage_delta(self) -> None:
        """The wire needs this call's cost; the accumulator is the whole turn's.

        Cursor treats reported usage as context fullness. Reporting the
        turn-cumulative total on every handback convinced it the window was
        overfull, so it summarized the transcript away each turn and the model
        re-read the same file for the rest of the run.
        """
        cp = _checkpoint()  # usage_accumulator: 100 / 20 / 120
        motet = _mock_motet()
        with patch(f"{RESUME_MODULE}.get_motet_context", return_value=motet), \
             patch(f"{RESUME_MODULE}.load_turn_checkpoint", return_value=cp), \
             patch(f"{RESUME_MODULE}.find_checkpoint_id_by_tool_call", return_value=None), \
             patch(
                 LOOP_ITERATE,
                 return_value={
                     "final_response": "resumed answer",
                     "stop_reason": "stop",
                     "usage": {
                         "prompt_tokens": 130,
                         "completion_tokens": 26,
                         "total_tokens": 156,
                     },
                 },
             ), \
             patch(
                 "motet.core.distributed.task_control.is_cancelled",
                 return_value=False,
             ):
            result = resume_turn(
                ResumeTurnData(checkpoint_id=cp.checkpoint_id, observations=OBSERVATIONS)
            )

        assert _payload(result)["usage_this_request"] == {
            "prompt_tokens": 30,
            "completion_tokens": 6,
            "total_tokens": 36,
        }
        # The turn total stays available for Motet's own accounting.
        assert _payload(result)["usage"]["prompt_tokens"] == 130

    def test_per_request_usage_absent_when_loop_reports_none(self) -> None:
        cp = _checkpoint()
        result, _, _ = _run_resume(
            ResumeTurnData(checkpoint_id=cp.checkpoint_id, observations=OBSERVATIONS), cp,
        )
        assert _payload(result)["usage_this_request"] is None

    def test_external_history_overrides_checkpoint(self) -> None:
        cp = _checkpoint()
        external = [
            {"role": "user", "content": "client-owned transcript"},
            {"role": "assistant", "content": ""},
        ]
        _, loop_calls, _ = _run_resume(
            ResumeTurnData(
                checkpoint_id=cp.checkpoint_id,
                observations=OBSERVATIONS,
                conversation_history=external,
            ),
            cp,
        )
        history = loop_calls[0].conversation_history
        assert history[0].content == "client-owned transcript"
        # Observations still appended after the external history.
        assert history[-1].role == "tool"

    def test_client_history_rederives_signatures_and_drops_pruned_ones(self) -> None:
        """A pruned client transcript must not inherit Motet's dedupe memory.

        Cursor summarizes long conversations, dropping old tool results. Carrying
        the checkpoint's signatures across that would make the loop refuse to
        re-read a file the model can no longer see, and tell it to "adjust
        parameters" instead — the read-window thrashing loop.
        """
        cp = _checkpoint()
        cp.executed_signatures = [
            _generate_tool_signature("Read", {"path": "kept.py"}),
            _generate_tool_signature("Read", {"path": "summarized_away.py"}),
        ]
        # The client resends a transcript retaining only the first read.
        external = [
            {"role": "user", "content": "refactor this"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls_canonical": [
                    {
                        "call_id": "read_kept",
                        "tool_name": "Read",
                        "arguments_json": '{"path": "kept.py"}',
                        "arguments": {"path": "kept.py"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "read_kept", "name": "Read", "content": "..."},
            {"role": "assistant", "content": ""},
        ]
        _, loop_calls, _ = _run_resume(
            ResumeTurnData(
                checkpoint_id=cp.checkpoint_id,
                observations=OBSERVATIONS,
                conversation_history=external,
            ),
            cp,
        )
        signatures = loop_calls[0].executed_signatures
        assert signatures == [_generate_tool_signature("Read", {"path": "kept.py"})]
        assert _generate_tool_signature("Read", {"path": "summarized_away.py"}) not in signatures

    def test_rederived_signatures_include_the_just_handed_back_calls(self) -> None:
        """Derivation runs after observations land, so this turn's calls count.

        Otherwise the model could immediately re-request the tool it just got a
        result for and get handed back again.
        """
        cp = _checkpoint()
        external = [
            {"role": "user", "content": "weather and news?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls_canonical": [
                    {
                        "call_id": "call_1",
                        "tool_name": "get_weather",
                        "arguments_json": '{"city": "SF"}',
                        "arguments": {"city": "SF"},
                    },
                    {
                        "call_id": "call_2",
                        "tool_name": "get_news",
                        "arguments_json": "{}",
                    },
                ],
            },
        ]
        _, loop_calls, _ = _run_resume(
            ResumeTurnData(
                checkpoint_id=cp.checkpoint_id,
                observations=OBSERVATIONS,
                conversation_history=external,
            ),
            cp,
        )
        signatures = loop_calls[0].executed_signatures
        assert _generate_tool_signature("get_weather", {"city": "SF"}) in signatures
        assert _generate_tool_signature("get_news", {}) in signatures

    def test_checkpoint_history_keeps_checkpoint_signatures(self) -> None:
        """Without a client transcript, Motet owns both and nothing is re-derived."""
        cp = _checkpoint()
        _, loop_calls, _ = _run_resume(
            ResumeTurnData(checkpoint_id=cp.checkpoint_id, observations=OBSERVATIONS), cp,
        )
        assert loop_calls[0].executed_signatures == list(cp.executed_signatures)

    def test_missing_history_raises(self) -> None:
        cp = _checkpoint(conversation_history=None)
        with pytest.raises(ValueError, match="no conversation history"):
            _run_resume(
                ResumeTurnData(checkpoint_id=cp.checkpoint_id, observations=OBSERVATIONS), cp,
            )

    def test_resume_restores_handback_tools(self) -> None:
        """A resumed turn keeps offering the client's tools and can suspend again."""
        cp = _checkpoint(
            handback_tools=[{"name": "Shell", "description": "", "json_schema": {}}],
        )
        _, loop_calls, _ = _run_resume(
            ResumeTurnData(checkpoint_id=cp.checkpoint_id, observations=OBSERVATIONS), cp,
        )
        assert loop_calls[0].handback_tools == [
            {"name": "Shell", "description": "", "json_schema": {}}
        ]

    def test_resume_rebinds_conversation_to_checkpoint(self) -> None:
        """HTTP resume often mints a fresh id; rebound so cache/cost stay on suspend."""
        cp = _checkpoint(conversation_id="openai-suspend-conv")
        motet = _mock_motet(conversation_id="openai-freshly-minted")
        result, _, motet = _run_resume(
            ResumeTurnData(checkpoint_id=cp.checkpoint_id, observations=OBSERVATIONS),
            cp,
            motet=motet,
        )
        assert motet.conversation_id == "openai-suspend-conv"
        assert _payload(result)["conversation_id"] == "openai-suspend-conv"

    def test_resume_conversation_bind_noop_when_already_aligned(self) -> None:
        cp = _checkpoint(conversation_id="conv-1")
        motet = _mock_motet(conversation_id="conv-1")
        assert _bind_resume_conversation(motet, cp) == "conv-1"
        assert motet.conversation_id == "conv-1"

    def test_resume_conversation_bind_via_distributed_context(self) -> None:
        from types import SimpleNamespace

        class FakeMotet:
            def __init__(self) -> None:
                self.distributed_context = SimpleNamespace(conversation_id="openai-minted")

            @property
            def conversation_id(self) -> str:
                return str(self.distributed_context.conversation_id)

        cp = _checkpoint(conversation_id="openai-seed")
        motet = FakeMotet()
        assert _bind_resume_conversation(motet, cp) == "openai-seed"
        assert motet.distributed_context.conversation_id == "openai-seed"
        assert motet.conversation_id == "openai-seed"


# --- resume_turn finalize-on-completion (ADR-0127 transcript semantics) -----------


def _finalize_registry(finalize: Optional[str] = "core.finalize_turn") -> MagicMock:
    from types import SimpleNamespace

    registry = MagicMock()
    registry.get.return_value = SimpleNamespace(
        turn_hooks=SimpleNamespace(finalize=finalize)
    )
    return registry


class TestResumeFinalize:
    """Exactly one transcript per logical turn: written by resume_agent_turn (#147)."""

    RESUME_AGENT_MODULE = "motet.core.orchestration.turn.resume_agent_turn"

    def _resume_with_finalize(
        self,
        checkpoint: TurnCheckpoint,
        *,
        loop_result: Dict[str, Any],
        registry: Optional[MagicMock] = None,
    ) -> MagicMock:
        from motet.core.orchestration.turn.resume_agent_turn import (
            ResumeAgentTurnData,
            resume_agent_turn,
        )

        motet = _mock_motet()
        # resume_agent_turn → in-process resume_turn; stamp agent_id as resume_turn does.
        stamped = dict(loop_result)
        if checkpoint.agent_id:
            stamped.setdefault("agent_id", checkpoint.agent_id)
        stamped.setdefault("resumed_from_checkpoint", checkpoint.checkpoint_id)
        motet.maybe.return_value = ({"stored": True}, None)
        with patch(
            f"{self.RESUME_AGENT_MODULE}.get_motet_context", return_value=motet
        ), patch(
            RESUME_TURN_FN, return_value=stamped
        ), patch(
            f"{CHECKPOINT_MODULE}.load_turn_checkpoint", return_value=checkpoint
        ), patch(
            "motet.core.agents.get_agent_registry",
            return_value=registry or _finalize_registry(),
        ), patch(
            "motet.core.agents.resolve_agent_id",
            side_effect=lambda x: x,
        ):
            resume_agent_turn.__wrapped__(
                ResumeAgentTurnData(
                    checkpoint_id=checkpoint.checkpoint_id,
                    observations=OBSERVATIONS,
                )
            )
        return motet

    def test_completed_resume_finalizes_under_recorded_agent(self) -> None:
        cp = _checkpoint(agent_id="core.default")
        motet = self._resume_with_finalize(
            cp, loop_result={"final_response": "resumed answer", "stop_reason": "stop"},
        )
        assert motet.maybe.called, "completed resume must write the turn transcript"
        fin_data = motet.maybe.call_args.kwargs["data"]
        assert fin_data.agent_id == "core.default"
        assert fin_data.assistant_response == "resumed answer"
        assert fin_data.store_conversation is True

    def test_resuspended_resume_does_not_finalize(self) -> None:
        cp = _checkpoint(agent_id="core.default")
        motet = self._resume_with_finalize(
            cp,
            loop_result={
                "final_response": "",
                "stop_reason": "suspended",
                "handed_back_tool_calls": [],
            },
        )
        assert not motet.maybe.called, "a re-suspended turn is still incomplete"

    def test_no_recorded_agent_skips_finalize(self) -> None:
        cp = _checkpoint(agent_id=None)
        motet = self._resume_with_finalize(
            cp, loop_result={"final_response": "done", "stop_reason": "stop"},
        )
        assert not motet.maybe.called, "non-agent_turn consumers own their lifecycle"

    def test_disabled_finalize_hook_is_respected(self) -> None:
        cp = _checkpoint(agent_id="core.no_transcripts")
        motet = self._resume_with_finalize(
            cp,
            loop_result={"final_response": "done", "stop_reason": "stop"},
            registry=_finalize_registry(finalize=None),
        )
        assert not motet.maybe.called, "agents that opted out of transcripts stay opted out"

    def test_finalized_history_is_rebuilt_from_the_checkpoint(self) -> None:
        """The transcript must match what the model saw, without the loop
        shipping the whole history back on the command response."""
        cp = _checkpoint(agent_id="core.default")
        motet = self._resume_with_finalize(
            cp, loop_result={"final_response": "resumed answer", "stop_reason": "stop"},
        )

        messages = motet.maybe.call_args.kwargs["data"].messages
        assert [m.role for m in messages] == ["user", "assistant", "tool", "tool"]
        assert [m.tool_call_id for m in messages[-2:]] == ["call_1", "call_2"]
        assert messages[-2].content == "72F and sunny"

    def test_loop_result_does_not_ship_the_transcript(self) -> None:
        """resume_turn stamps identity only; history stays in the checkpoint."""
        checkpoint = _checkpoint(agent_id="core.default")
        result, _, _ = _run_resume(
            ResumeTurnData(
                checkpoint_id=checkpoint.checkpoint_id, observations=OBSERVATIONS
            ),
            checkpoint,
        )
        assert "conversation_history" not in _payload(result)
        assert _payload(result)["agent_id"] == "core.default"
        assert not result.conversation_history

    def test_resume_ensures_the_stream_exists(self) -> None:
        """A resume lands on a fresh task; the terminal `end` needs a stream."""
        cp = _checkpoint(agent_id="core.default")
        motet = self._resume_with_finalize(
            cp, loop_result={"final_response": "done", "stop_reason": "stop"},
        )
        assert motet.ensure_stream.called


class TestAuthRequiredHistory:
    """auth_required has no resume handle, so its history is written now or never."""

    RESUME_AGENT_MODULE = "motet.core.orchestration.turn.resume_agent_turn"

    AUTH_RESULT: Dict[str, Any] = {
        "final_response": "Please authorize Google Workspace to continue.",
        "stop_reason": "auth_required",
        "auth_required": True,
        "service_id": "google_workspace",
    }

    def test_gate_classifies_auth_as_history_only(self) -> None:
        from motet.core.orchestration.turn.outcome import classify_loop_outcome

        outcome = classify_loop_outcome(self.AUTH_RESULT)
        assert outcome.should_finalize is False
        assert outcome.history_only_finalize is True

    def test_suspended_stays_history_free(self) -> None:
        from motet.core.orchestration.turn.outcome import classify_loop_outcome

        outcome = classify_loop_outcome(
            {"stop_reason": "suspended", "handed_back_tool_calls": []}
        )
        assert outcome.history_only_finalize is False, "resume writes the transcript"

    def test_gate_invokes_persist_callback_with_assistant_text(self) -> None:
        from motet.core.orchestration.turn.outcome import (
            apply_turn_outcome_gate,
            classify_loop_outcome,
        )

        persisted: List[str] = []
        outcome = classify_loop_outcome(self.AUTH_RESULT)
        apply_turn_outcome_gate(
            _mock_motet(), outcome, self.AUTH_RESULT, "core.default", None, {}, {},
            persisted.append,
        )
        assert persisted == ["Please authorize Google Workspace to continue."]

    def test_persist_failure_does_not_break_the_auth_response(self) -> None:
        from motet.core.orchestration.turn.outcome import (
            apply_turn_outcome_gate,
            classify_loop_outcome,
        )

        def boom(_: str) -> None:
            raise RuntimeError("redis down")

        outcome = classify_loop_outcome(self.AUTH_RESULT)
        response = apply_turn_outcome_gate(
            _mock_motet(), outcome, self.AUTH_RESULT, "core.default", None, {}, {}, boom,
        )
        assert response is not None and response["auth_required"] is True

    def test_resume_persists_auth_history_without_memory_update(self) -> None:
        from motet.core.orchestration.turn.resume_agent_turn import (
            ResumeAgentTurnData,
            resume_agent_turn,
        )

        checkpoint = _checkpoint(agent_id="core.default")
        motet = _mock_motet()
        stamped = {
            **self.AUTH_RESULT,
            "agent_id": "core.default",
            "resumed_from_checkpoint": checkpoint.checkpoint_id,
        }
        motet.maybe.return_value = ({"stored": True}, None)
        with patch(
            f"{self.RESUME_AGENT_MODULE}.get_motet_context", return_value=motet
        ), patch(
            RESUME_TURN_FN, return_value=stamped
        ), patch(
            f"{CHECKPOINT_MODULE}.load_turn_checkpoint", return_value=checkpoint
        ), patch(
            "motet.core.agents.get_agent_registry", return_value=_finalize_registry()
        ), patch(
            "motet.core.agents.resolve_agent_id", side_effect=lambda x: x
        ):
            response = resume_agent_turn.__wrapped__(
                ResumeAgentTurnData(
                    checkpoint_id=checkpoint.checkpoint_id, observations=OBSERVATIONS
                )
            )

        assert motet.maybe.called, "the exchange must survive the trip to the auth page"
        fin_data = motet.maybe.call_args.kwargs["data"]
        assert fin_data.store_conversation is True
        assert fin_data.update_memory is False, "no answer was produced to learn from"
        assert fin_data.assistant_response == self.AUTH_RESULT["final_response"]
        assert response["auth_required"] is True


class TestDirectResumeWarning:
    """Direct resume_turn skips orchestration finalize and warns on agent checkpoints."""

    def test_direct_call_on_agent_checkpoint_warns(self) -> None:
        checkpoint = _checkpoint(agent_id="core.default")
        with patch(f"{RESUME_MODULE}.logger") as log:
            _run_resume(
                ResumeTurnData(
                    checkpoint_id=checkpoint.checkpoint_id, observations=OBSERVATIONS
                ),
                checkpoint,
            )
        warned = [c.args[0] for c in log.warning.call_args_list]
        assert "resume_turn_direct_call_skips_finalize" in warned

    def test_orchestrated_call_does_not_warn(self) -> None:
        checkpoint = _checkpoint(agent_id="core.default")
        with patch(f"{RESUME_MODULE}.logger") as log:
            _run_resume(
                ResumeTurnData(
                    checkpoint_id=checkpoint.checkpoint_id,
                    observations=OBSERVATIONS,
                    orchestrated=True,
                ),
                checkpoint,
            )
        warned = [c.args[0] for c in log.warning.call_args_list]
        assert "resume_turn_direct_call_skips_finalize" not in warned

    def test_non_agent_checkpoint_does_not_warn(self) -> None:
        checkpoint = _checkpoint(agent_id=None)
        with patch(f"{RESUME_MODULE}.logger") as log:
            _run_resume(
                ResumeTurnData(
                    checkpoint_id=checkpoint.checkpoint_id, observations=OBSERVATIONS
                ),
                checkpoint,
            )
        warned = [c.args[0] for c in log.warning.call_args_list]
        assert "resume_turn_direct_call_skips_finalize" not in warned


class TestResumeResponseContract:
    """resume_agent_turn's real return shape must satisfy the OpenAI facade.

    The facade classifies the resume command dict into TurnResult and branches
    on ``kind``. Facade tests stub the command boundary, so only this test
    catches the turn response dropping a key the classifier reads.
    """

    RESUME_AGENT_MODULE = "motet.core.orchestration.turn.resume_agent_turn"

    def _resume_response(self, loop_result: Dict[str, Any]) -> Dict[str, Any]:
        from motet.core.orchestration.turn.resume_agent_turn import (
            ResumeAgentTurnData,
            resume_agent_turn,
        )

        motet = _mock_motet()
        stamped = dict(loop_result)
        stamped.setdefault("agent_id", "core.default")
        stamped.setdefault("resumed_from_checkpoint", "suspend-abc")
        stamped.setdefault("conversation_history", [])
        motet.maybe.return_value = ({"stored": True}, None)
        with patch(
            f"{self.RESUME_AGENT_MODULE}.get_motet_context", return_value=motet
        ), patch(
            RESUME_TURN_FN, return_value=stamped
        ), patch(
            "motet.core.agents.get_agent_registry", return_value=_finalize_registry()
        ), patch(
            "motet.core.agents.resolve_agent_id", side_effect=lambda x: x
        ):
            return resume_agent_turn.__wrapped__(
                ResumeAgentTurnData(
                    checkpoint_id="suspend-abc", observations=OBSERVATIONS
                )
            )

    def test_resuspended_response_still_reads_as_suspended(self) -> None:
        response = self._resume_response(
            {
                "final_response": "",
                "stop_reason": "suspended",
                "checkpoint_id": "suspend-def",
                "handed_back_tool_calls": [
                    {"tool_call_id": "call_2", "tool_name": "edit_file", "parameters": {}}
                ],
            }
        )

        assert response["stop_reason"] == "suspended"
        assert response["suspended"] is True
        assert response["outcome"] == "suspended"
        assert response["handed_back_tool_calls"][0]["tool_call_id"] == "call_2"

    def test_resuspended_response_maps_to_facade_tool_calls(self) -> None:
        from motet.interfaces.api.openai_compat import execution

        response = self._resume_response(
            {
                "final_response": "",
                "stop_reason": "suspended",
                "checkpoint_id": "suspend-def",
                "handed_back_tool_calls": [
                    {"tool_call_id": "call_2", "tool_name": "edit_file", "parameters": {}}
                ],
            }
        )
        mapped = execution._result_from_resumed_loop(response)

        assert mapped["finish_reason"] == "tool_calls"
        assert mapped["tool_calls_canonical"][0]["call_id"] == "call_2"

    def test_completed_response_carries_stop_reason(self) -> None:
        response = self._resume_response(
            {"final_response": "resumed answer", "stop_reason": "stop"}
        )

        assert response["stop_reason"] == "stop"
        assert response.get("suspended") is not True
        assert response["final_response"] == "resumed answer"

    def test_completed_response_maps_to_facade_text(self) -> None:
        from motet.interfaces.api.openai_compat import execution

        response = self._resume_response(
            {"final_response": "resumed answer", "stop_reason": "stop"}
        )
        mapped = execution._result_from_resumed_loop(response)

        assert mapped.get("finish_reason") != "tool_calls"
        assert "resumed answer" in str(mapped.get("content") or "")


# --- agent_turn AgentData handback forwarding (ADR-0125 §5c.1) ------------


HANDBACK_SCHEMA = {"name": "ReadFile", "description": "client tool", "json_schema": {}}


class TestTurnAgentDataHandback:
    """agent_turn builds AgentData itself; handbacks must survive that build."""

    def _agent_data(self, **context: Any) -> Any:
        from motet.core.orchestration.turn.agent_turn import _agent_data_for_turn

        return _agent_data_for_turn(
            query="what does agents.md say?",
            history=[{"role": "user", "content": "what does agents.md say?"}],
            qualified_id=str(context.get("agent_id") or "core.default"),
            agent_config=type("Cfg", (), {"max_tools": None, "max_iterations": 20})(),
            provider="openai",
            model_name="gpt-4.1-mini",
            model_profile_name=None,
            enable_thinking=False,
            reasoning_effort="medium",
            resolved_tools=None,
            metadata=context.pop("metadata", {}),
            effective_context=context,
        )

    def test_handback_fields_forwarded_from_context(self) -> None:
        agent_data = self._agent_data(
            agent_id="core.default",
            handback_tool_names=["Shell"],
            handback_tools=[HANDBACK_SCHEMA],
        )
        assert agent_data.handback_tool_names == ["Shell"]
        assert agent_data.handback_tools == [HANDBACK_SCHEMA]

    def test_suspended_loop_result_is_classified_at_the_top_level(self) -> None:
        from motet.core.orchestration.turn.outcome import (
            TurnOutcomeKind,
            classify_loop_outcome,
        )

        suspended = {
            "final_response": "",
            "stop_reason": "suspended",
            "checkpoint_id": "cp-1",
            "handed_back_tool_calls": [
                {"tool_call_id": "call_1", "tool_name": "ReadFile", "parameters": {}}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }
        outcome = classify_loop_outcome(suspended)
        assert outcome.kind == TurnOutcomeKind.SUSPENDED
        assert outcome.checkpoint_id == "cp-1"

    def test_max_tools_defaults_to_twenty(self) -> None:
        """Regression: hardcoding max_tools=10 starved Motet discovery/memory."""
        agent_data = self._agent_data(agent_id="cursor.backend")
        assert agent_data.max_tools == 20

    def test_max_tools_and_filter_metadata(self) -> None:
        from motet.core.orchestration.turn.agent_turn import _agent_data_for_turn

        agent_data = _agent_data_for_turn(
            query="list tools",
            history=[{"role": "user", "content": "list tools"}],
            qualified_id="cursor.backend",
            agent_config=type("Cfg", (), {"max_tools": 20, "max_iterations": 20})(),
            provider="openai",
            model_name="gpt-4.1-mini",
            model_profile_name=None,
            enable_thinking=False,
            reasoning_effort="medium",
            resolved_tools=None,
            metadata={
                "tool_filter_metadata": {
                    "required_tools": ["core.tools_search", "core.memory_store"],
                },
            },
            effective_context={"agent_id": "cursor.backend"},
        )
        assert agent_data.max_tools == 20
        assert agent_data.tool_filter_metadata == {
            "required_tools": ["core.tools_search", "core.memory_store"],
        }
