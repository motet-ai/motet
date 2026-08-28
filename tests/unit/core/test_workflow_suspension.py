"""
Motet - Workflow Suspension and Resume Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-10

Description:
    Unit tests for workflow step ownership and suspension (issue #149):
    - WorkflowCheckpoint Redis store (TTL, index, non-consuming load)
    - ownership=handback pause + resume_workflow observations
    - confirmation / elicitation / oauth suspend reasons
    - parallel multi-handback (Phase E)
    - principal / forged id rejection
    - nested turn checkpoint fields
    - claim / resume_epoch / terminal idempotency
    - bounded nesting (#189) depth + parent/child suspend
    - agent-path fail-fast for non-handback nested suspends
    - operator pause/cancel (control signals + cooperative honor)
    - operator resume, cancel cascade, cancel-over-pause precedence
    - ADR-0131 workflow push wake + task→workflow bridge index

Usage:
    pytest tests/unit/core/test_workflow_suspension.py
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

# Ensure core.tool_execution is registered for executor step dispatch.
import motet.core.commands.builtin.tool  # noqa: F401
import motet.core.commands.builtin.workflow  # noqa: F401

from motet.core.workflow import (
    Workflow,
    WorkflowStep,
    WorkflowStatus,
    validate_workflow,
)
from motet.core.workflow.checkpoint import (
    WORKFLOW_CHECKPOINT_TTL_SECONDS,
    PendingInteraction,
    WorkflowCheckpoint,
    WorkflowResumeConflict,
    WorkflowRunControlConflict,
    WorkflowRunStatus,
    WorkflowSuspendNotConsumable,
    WorkflowSuspendReason,
    claim_workflow_run_for_resume,
    clear_workflow_run_control,
    find_workflow_run_id_by_interaction,
    is_workflow_cancelled,
    list_workflow_runs_for_task,
    load_workflow_checkpoint,
    peek_workflow_run_control,
    register_workflow_control_waiter,
    request_workflow_run_control,
    store_workflow_checkpoint,
)
from motet.core.workflow.executor import WorkflowExecutor
from motet.core.checkpoints import TurnCheckpoint
from motet.core.reasoning.react.agentic_loop import _suspend_for_nested_workflow
from motet.core.reasoning.react.agentic_loop_data import AgenticLoopData

REDIS_MANAGER = "motet.core.distributed.redis_manager"


def _memory_redis(stored: Dict[str, Any]) -> MagicMock:
    """Sync Redis client mock that keeps delete/zrem in sync with ``stored``."""
    client = MagicMock()

    def _delete(*keys: Any) -> int:
        removed = 0
        for key in keys:
            k = key.decode() if isinstance(key, bytes) else str(key)
            if stored.pop(k, None) is not None:
                removed += 1
        return removed

    client.delete.side_effect = _delete
    return client


class _WakeRedis:
    """Minimal Redis stand-in for workflow waiter registry + LPUSH wakes."""

    def __init__(self) -> None:
        self.sets: Dict[str, set] = {}
        self.lists: Dict[str, list] = {}
        self.kv: Dict[str, Any] = {}

    def exists(self, *keys: str) -> int:
        return sum(1 for key in keys if key in self.kv)

    def expire(self, key: str, ttl: int) -> bool:
        return True

    def sadd(self, key: str, *members: Any) -> int:
        s = self.sets.setdefault(key, set())
        before = len(s)
        for m in members:
            s.add(m)
        return len(s) - before

    def srem(self, key: str, *members: Any) -> int:
        s = self.sets.get(key) or set()
        n = 0
        for m in members:
            if m in s:
                s.discard(m)
                n += 1
        return n

    def smembers(self, key: str) -> set:
        return set(self.sets.get(key) or set())

    def lpush(self, key: str, value: Any) -> int:
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    def zadd(self, key: str, mapping: Dict[str, Any]) -> int:
        return 0

    def zrem(self, key: str, *members: Any) -> int:
        return 0

    def delete(self, *keys: str) -> int:
        n = 0
        for key in keys:
            if key in self.kv:
                del self.kv[key]
                n += 1
            if key in self.lists:
                del self.lists[key]
                n += 1
            if key in self.sets:
                del self.sets[key]
                n += 1
        return n


def _mock_motet(**overrides: Any) -> MagicMock:
    motet = MagicMock()
    motet.motet_id = overrides.get("motet_id", "default")
    motet.tenant_id = overrides.get("tenant_id", "tenant-a")
    motet.principal_id = overrides.get("principal_id", "user-1")
    motet.task_id = overrides.get("task_id", "task-1")
    motet.conversation_id = overrides.get("conversation_id", "conv-1")
    motet.metadata = {}
    motet.stream_key = None
    return motet


def _checkpoint(**overrides: Any) -> WorkflowCheckpoint:
    defaults: Dict[str, Any] = dict(
        motet_id="default",
        tenant_id="tenant-a",
        principal_id="user-1",
        conversation_id="conv-1",
        workflow_id="demo_wf",
        workflow_name="Demo",
        workflow_steps=[
            {
                "step_id": "read",
                "name": "read",
                "command_type": "tool_execution",
                "command_data": {"tool_name": "ReadFile", "parameters": {"path": "a.py"}},
                "ownership": "handback",
                "dependencies": [],
            }
        ],
        execution_order=[["read"]],
        completed_step_ids=[],
        pending_step_ids=["read"],
        context={},
        step_results={},
        suspend_reason=WorkflowSuspendReason.HANDBACK_TOOLS,
        pending_interactions=[
            PendingInteraction(
                interaction_id="call_1",
                kind=WorkflowSuspendReason.HANDBACK_TOOLS,
                step_id="read",
                tool_name="ReadFile",
                parameters={"path": "a.py"},
            )
        ],
    )
    defaults.update(overrides)
    return WorkflowCheckpoint(**defaults)


# --- Store --------------------------------------------------------------------


class TestWorkflowCheckpointStore:
    def test_store_writes_checkpoint_and_index_with_ttl(self) -> None:
        stored: Dict[str, Any] = {}

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        client = MagicMock()
        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=client):
            cp = _checkpoint()
            returned = store_workflow_checkpoint(cp)

        assert returned == cp.workflow_run_id
        key = f"tenant-a:workflow_checkpoint:tenant-a:default:{cp.workflow_run_id}"
        assert key in stored
        blob = stored[key]
        assert blob["schema_version"] == 1
        assert "identity" in blob and "run_state" in blob and "pending" in blob
        idx = "tenant-a:workflow_checkpoint:index:tenant-a:default:call_1"
        assert stored[idx]["workflow_run_id"] == cp.workflow_run_id
        assert client.expire.call_count >= 2
        client.expire.assert_any_call(key, WORKFLOW_CHECKPOINT_TTL_SECONDS)

    def test_load_round_trip_non_consuming(self) -> None:
        stored: Dict[str, Any] = {}

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        def fake_load(service: str, key: str, format_type: str = "hash") -> Any:
            return stored.get(key)

        client = MagicMock()
        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=fake_load), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=client):
            cp = _checkpoint()
            store_workflow_checkpoint(cp)
            loaded1 = load_workflow_checkpoint(
                tenant_id="tenant-a", motet_id="default", workflow_run_id=cp.workflow_run_id
            )
            loaded2 = load_workflow_checkpoint(
                tenant_id="tenant-a", motet_id="default", workflow_run_id=cp.workflow_run_id
            )
            indexed = find_workflow_run_id_by_interaction(
                tenant_id="tenant-a", motet_id="default", interaction_id="call_1"
            )

        assert loaded1 is not None and loaded2 is not None
        assert loaded1.workflow_run_id == cp.workflow_run_id
        assert loaded2.pending_interactions[0].interaction_id == "call_1"
        assert indexed == cp.workflow_run_id

    def test_load_missing_returns_none(self) -> None:
        with patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", return_value=None):
            assert (
                load_workflow_checkpoint(
                    tenant_id="t", motet_id="default", workflow_run_id="missing"
                )
                is None
            )


# --- Ownership validation -----------------------------------------------------


class TestOwnershipValidation:
    def test_handback_on_non_tool_step_rejected(self) -> None:
        wf = Workflow(
            workflow_id="bad",
            name="bad",
            steps={
                "m": WorkflowStep(
                    step_id="m",
                    name="model",
                    command_type="model_inference",
                    command_data={},
                    ownership="handback",
                )
            },
        )
        with pytest.raises(ValueError, match="not tool-shaped"):
            validate_workflow(wf)

    def test_elicitation_requires_schema(self) -> None:
        wf = Workflow(
            workflow_id="bad",
            name="bad",
            steps={
                "e": WorkflowStep(
                    step_id="e",
                    name="ask",
                    step_type="elicitation",
                    command_type="",
                    ownership="motet",
                )
            },
        )
        with pytest.raises(ValueError, match="elicitation_schema"):
            validate_workflow(wf)

    def test_yaml_type_alias_maps_to_step_type(self) -> None:
        step = WorkflowStep.from_dict(
            {
                "step_id": "ask",
                "name": "ask",
                "type": "elicitation",
                "schema": {"type": "object", "properties": {"x": {"type": "string"}}},
            }
        )
        assert step.step_type == "elicitation"
        assert step.elicitation_schema is not None


# --- Executor handback / resume -----------------------------------------------


class TestExecutorSuspension:
    def _handback_workflow(self) -> Workflow:
        return Workflow(
            workflow_id="hb_wf",
            name="Handback Demo",
            steps={
                "prep": WorkflowStep(
                    step_id="prep",
                    name="prep",
                    command_type="core.tool_execution",
                    command_data={"tool_name": "core.echo", "parameters": {"text": "hi"}},
                    ownership="motet",
                    dependencies=[],
                ),
                "read": WorkflowStep(
                    step_id="read",
                    name="read",
                    command_type="core.tool_execution",
                    command_data={
                        "tool_name": "ReadFile",
                        "parameters": {"target_file": "x.py"},
                    },
                    ownership="handback",
                    dependencies=["prep"],
                ),
                "done": WorkflowStep(
                    step_id="done",
                    name="done",
                    command_type="core.tool_execution",
                    command_data={"tool_name": "core.echo", "parameters": {"text": "ok"}},
                    ownership="motet",
                    dependencies=["read"],
                ),
            },
            context={},
        )

    def test_handback_pauses_and_sets_paused(self) -> None:
        wf = self._handback_workflow()
        motet = _mock_motet()
        executor = WorkflowExecutor()
        stored: Dict[str, Any] = {}

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        def fake_call(cmd: Any, data: Any = None, **kwargs: Any) -> Dict[str, Any]:
            return {"status": "success", "data": {"echoed": True}}

        motet.do = MagicMock(side_effect=fake_call)

        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=MagicMock()):
            result = executor.execute_workflow(wf, motet)

        assert result["suspended"] is True
        assert result["suspend_reason"] == "handback_tools"
        assert wf.status == WorkflowStatus.PAUSED
        assert result["pending_tool_calls"]
        assert result["pending_tool_calls"][0]["tool_name"] == "ReadFile"
        assert "prep" in result["completed_step_ids"]
        assert motet.do.call_count == 1  # only prep ran

    def test_resume_handback_continues_on_new_invocation(self) -> None:
        wf = self._handback_workflow()
        motet = _mock_motet()
        executor = WorkflowExecutor()
        stored: Dict[str, Any] = {}

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        def fake_load(service: str, key: str, format_type: str = "hash") -> Any:
            return stored.get(key)

        def fake_call(cmd: Any, data: Any = None, **kwargs: Any) -> Dict[str, Any]:
            return {"status": "success", "data": {"ok": True}}

        motet.do = MagicMock(side_effect=fake_call)

        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=fake_load), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=MagicMock()):
            paused = executor.execute_workflow(wf, motet)
            run_id = paused["workflow_run_id"]
            call_id = paused["pending_tool_calls"][0]["tool_call_id"]
            cp = load_workflow_checkpoint(
                tenant_id="tenant-a", motet_id="default", workflow_run_id=run_id
            )
            assert cp is not None
            # Simulate a different worker resuming.
            motet2 = _mock_motet()
            motet2.do = MagicMock(side_effect=fake_call)
            result = executor.resume_from_checkpoint(
                cp,
                motet2,
                kind="handback_tools",
                observations=[{"tool_call_id": call_id, "content": "file contents"}],
            )

        assert result.get("status") == "completed"
        assert "done" in result["step_results"]
        assert motet2.do.call_count == 1  # only remaining Motet step

    def test_forged_observation_rejected(self) -> None:
        cp = _checkpoint()
        executor = WorkflowExecutor()
        with pytest.raises(ValueError, match="unknown tool_call_id"):
            executor.resume_from_checkpoint(
                cp,
                _mock_motet(),
                kind="handback_tools",
                observations=[{"tool_call_id": "forged", "content": "x"}],
            )

    def test_missing_observation_rejected(self) -> None:
        cp = _checkpoint()
        executor = WorkflowExecutor()
        with pytest.raises(ValueError, match="missing observations"):
            executor.resume_from_checkpoint(
                cp,
                _mock_motet(),
                kind="handback_tools",
                observations=[],
            )


class TestConfirmationAndElicitation:
    def test_confirmation_pauses_then_motet_executes_on_approve(self) -> None:
        wf = Workflow(
            workflow_id="confirm_wf",
            name="Confirm",
            steps={
                "danger": WorkflowStep(
                    step_id="danger",
                    name="danger",
                    command_type="core.tool_execution",
                    command_data={
                        "tool_name": "core.delete",
                        "parameters": {"id": "1"},
                    },
                    ownership="motet",
                    requires_confirmation=True,
                )
            },
        )
        motet = _mock_motet()
        motet.do = MagicMock(return_value={"status": "success", "data": {"deleted": True}})
        executor = WorkflowExecutor()
        stored: Dict[str, Any] = {}

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        def fake_load(service: str, key: str, format_type: str = "hash") -> Any:
            return stored.get(key)

        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=fake_load), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=MagicMock()):
            paused = executor.execute_workflow(wf, motet)
            assert paused["suspend_reason"] == "confirmation"
            assert motet.do.call_count == 0
            cp = load_workflow_checkpoint(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id=paused["workflow_run_id"],
            )
            assert cp is not None
            result = executor.resume_from_checkpoint(
                cp, motet, kind="confirmation", decision="approve"
            )

        assert result.get("status") == "completed"
        assert motet.do.call_count == 1

    def test_elicitation_resume_with_answers(self) -> None:
        wf = Workflow(
            workflow_id="elicit_wf",
            name="Elicit",
            steps={
                "ask": WorkflowStep(
                    step_id="ask",
                    name="ask",
                    step_type="elicitation",
                    command_type="",
                    elicitation_schema={
                        "type": "object",
                        "properties": {"section": {"type": "string"}},
                    },
                    elicitation_prompt="Which section?",
                )
            },
        )
        # Skip command_type registry check for empty elicitation.
        motet = _mock_motet()
        executor = WorkflowExecutor()
        stored: Dict[str, Any] = {}

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        def fake_load(service: str, key: str, format_type: str = "hash") -> Any:
            return stored.get(key)

        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=fake_load), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=MagicMock()):
            paused = executor.execute_workflow(wf, motet)
            assert paused["suspend_reason"] == "elicitation"
            cp = load_workflow_checkpoint(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id=paused["workflow_run_id"],
            )
            assert cp is not None
            result = executor.resume_from_checkpoint(
                cp, motet, kind="elicitation", answers={"section": "intro"}
            )

        assert result.get("status") == "completed"
        assert result["step_results"]["ask"]["data"]["answers"]["section"] == "intro"


class TestParallelMultiHandback:
    def test_parallel_handbacks_require_all_observations(self) -> None:
        wf = Workflow(
            workflow_id="parallel_hb",
            name="Parallel",
            steps={
                "a": WorkflowStep(
                    step_id="a",
                    name="a",
                    command_type="core.tool_execution",
                    command_data={"tool_name": "ReadFile", "parameters": {"path": "a"}},
                    ownership="handback",
                ),
                "b": WorkflowStep(
                    step_id="b",
                    name="b",
                    command_type="core.tool_execution",
                    command_data={"tool_name": "ReadFile", "parameters": {"path": "b"}},
                    ownership="handback",
                ),
            },
        )
        motet = _mock_motet()
        executor = WorkflowExecutor()
        stored: Dict[str, Any] = {}

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        def fake_load(service: str, key: str, format_type: str = "hash") -> Any:
            return stored.get(key)

        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=fake_load), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=MagicMock()):
            paused = executor.execute_workflow(wf, motet)
            assert len(paused["pending_tool_calls"]) == 2
            cp = load_workflow_checkpoint(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id=paused["workflow_run_id"],
            )
            assert cp is not None
            only_one = paused["pending_tool_calls"][:1]
            with pytest.raises(ValueError, match="missing observations"):
                executor.resume_from_checkpoint(
                    cp,
                    motet,
                    kind="handback_tools",
                    observations=[
                        {"tool_call_id": only_one[0]["tool_call_id"], "content": "a"}
                    ],
                )
            # Reload fresh copy (previous call mutated checkpoint in memory).
            cp2 = load_workflow_checkpoint(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id=paused["workflow_run_id"],
            )
            assert cp2 is not None
            result = executor.resume_from_checkpoint(
                cp2,
                motet,
                kind="handback_tools",
                observations=[
                    {"tool_call_id": c["tool_call_id"], "content": c["tool_name"]}
                    for c in paused["pending_tool_calls"]
                ],
            )
        assert result.get("status") == "completed"

    def test_mixed_motet_handback_suspends_before_motet_sibling(self) -> None:
        wf = Workflow(
            workflow_id="mixed",
            name="Mixed",
            steps={
                "hb": WorkflowStep(
                    step_id="hb",
                    name="hb",
                    command_type="core.tool_execution",
                    command_data={"tool_name": "ReadFile", "parameters": {}},
                    ownership="handback",
                ),
                "motet_step": WorkflowStep(
                    step_id="motet_step",
                    name="motet_step",
                    command_type="core.tool_execution",
                    command_data={"tool_name": "core.echo", "parameters": {"text": "x"}},
                    ownership="motet",
                ),
            },
        )
        motet = _mock_motet()
        motet.do = MagicMock(return_value={"status": "success", "data": {}})
        executor = WorkflowExecutor()
        with patch(f"{REDIS_MANAGER}.store_structured_data_sync"), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=MagicMock()):
            paused = executor.execute_workflow(wf, motet)
        assert paused["suspended"]
        assert motet.do.call_count == 0
        assert set(paused["pending_step_ids"]) == {"hb", "motet_step"}


class TestNestedTurnCheckpointFields:
    def test_turn_checkpoint_accepts_workflow_run_id(self) -> None:
        cp = TurnCheckpoint(
            workflow_run_id="wfrun-abc",
            suspend_reason="handback_tools",
            nested_workflow_tool_call_id="call_wf",
            nested_workflow_tool_name="workflow_demo",
            handed_back_tool_calls=[
                {"tool_call_id": "call_1", "tool_name": "ReadFile", "parameters": {}}
            ],
            nested_resume_history=[{"role": "user", "content": "go"}],
        )
        blob = cp.to_storage_dict()
        assert blob["handback"]["workflow_run_id"] == "wfrun-abc"
        restored = TurnCheckpoint.model_validate(blob)
        assert restored.workflow_run_id == "wfrun-abc"
        assert restored.nested_workflow_tool_call_id == "call_wf"


class TestResumeWorkflowCommandPrincipal:
    def test_principal_mismatch_raises(self) -> None:
        from motet.core.commands.builtin.workflow import resume_workflow
        from motet.core.commands.command_data_classes import ResumeWorkflowData

        cp = _checkpoint()
        stored: Dict[str, Any] = {}

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        def fake_load(service: str, key: str, format_type: str = "hash") -> Any:
            return stored.get(key)

        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=fake_load), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=MagicMock()):
            store_workflow_checkpoint(cp)

            motet = _mock_motet(principal_id="other-user")
            with patch(
                "motet.core.commands.builtin.workflow.get_motet_context",
                return_value=motet,
            ):
                with pytest.raises(PermissionError, match="different principal"):
                    resume_workflow.__wrapped__(
                        ResumeWorkflowData(
                            workflow_run_id=cp.workflow_run_id,
                            kind="handback_tools",
                            observations=[
                                {"tool_call_id": "call_1", "content": "x"}
                            ],
                        )
                    )


def _lock_mock() -> MagicMock:
    lock = MagicMock()
    lock.release_sync = MagicMock()
    return lock


class TestClaimAndEpoch:
    def test_claim_increments_epoch_and_rejects_replay(self) -> None:
        stored: Dict[str, Any] = {}

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        def fake_load(service: str, key: str, format_type: str = "hash") -> Any:
            return stored.get(key)

        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=fake_load), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=MagicMock()), \
             patch(
                 f"{REDIS_MANAGER}.acquire_distributed_lock_sync",
                 return_value=_lock_mock(),
             ):
            cp = _checkpoint()
            store_workflow_checkpoint(cp)
            claimed = claim_workflow_run_for_resume(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id=cp.workflow_run_id,
                expected_epoch=0,
            )
            assert claimed is not None
            assert claimed.resume_epoch == 1
            assert claimed.status == WorkflowRunStatus.RUNNING
            with pytest.raises(WorkflowResumeConflict):
                claim_workflow_run_for_resume(
                    tenant_id="tenant-a",
                    motet_id="default",
                    workflow_run_id=cp.workflow_run_id,
                    expected_epoch=0,
                )

    def test_wrong_epoch_rejected(self) -> None:
        stored: Dict[str, Any] = {}

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        def fake_load(service: str, key: str, format_type: str = "hash") -> Any:
            return stored.get(key)

        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=fake_load), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=MagicMock()), \
             patch(
                 f"{REDIS_MANAGER}.acquire_distributed_lock_sync",
                 return_value=_lock_mock(),
             ):
            cp = _checkpoint(resume_epoch=2)
            store_workflow_checkpoint(cp)
            with pytest.raises(WorkflowResumeConflict, match="resume epoch"):
                claim_workflow_run_for_resume(
                    tenant_id="tenant-a",
                    motet_id="default",
                    workflow_run_id=cp.workflow_run_id,
                    expected_epoch=0,
                )

    def test_terminal_blocks_resume(self) -> None:
        stored: Dict[str, Any] = {}

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        def fake_load(service: str, key: str, format_type: str = "hash") -> Any:
            return stored.get(key)

        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=fake_load), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=MagicMock()), \
             patch(
                 f"{REDIS_MANAGER}.acquire_distributed_lock_sync",
                 return_value=_lock_mock(),
             ):
            cp = _checkpoint(status=WorkflowRunStatus.COMPLETED)
            store_workflow_checkpoint(cp)
            with pytest.raises(WorkflowResumeConflict, match="not awaiting resume"):
                claim_workflow_run_for_resume(
                    tenant_id="tenant-a",
                    motet_id="default",
                    workflow_run_id=cp.workflow_run_id,
                )


class TestAgentPathFailFast:
    def test_non_handback_nested_suspend_raises(self) -> None:
        motet = _mock_motet()
        data = AgenticLoopData(
            input="hi",
            conversation_history=[],
            max_iterations=5,
            remaining_iterations=4,
        )
        with pytest.raises(WorkflowSuspendNotConsumable, match="elicitation"):
            _suspend_for_nested_workflow(
                motet,
                data,
                {
                    "workflow_run_id": "wfrun-x",
                    "suspend_reason": "elicitation",
                    "pending_tool_calls": [],
                    "pending_interactions": [
                        {
                            "interaction_id": "el-1",
                            "kind": "elicitation",
                            "step_id": "ask",
                        }
                    ],
                },
                "",
                1,
                {},
                [],
            )


class TestBoundedNesting:
    def test_depth_exceeded_raises(self) -> None:
        parent = Workflow(
            workflow_id="parent",
            name="Parent",
            max_nesting_depth=0,  # refuse any nested workflow_execution
            steps={
                "nested": WorkflowStep(
                    step_id="nested",
                    name="nested",
                    command_type="core.workflow_execution",
                    command_data={
                        "workflow_id": "leaf",
                        "workflow_name": "Leaf",
                        "workflow_steps": [
                            {
                                "step_id": "echo",
                                "name": "echo",
                                "command_type": "core.tool_execution",
                                "command_data": {
                                    "tool_name": "core.echo",
                                    "parameters": {"text": "x"},
                                },
                            }
                        ],
                    },
                )
            },
        )
        motet = _mock_motet()
        motet.metadata = {}
        motet.do = MagicMock()
        executor = WorkflowExecutor()
        with patch(f"{REDIS_MANAGER}.store_structured_data_sync"), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=MagicMock()):
            with pytest.raises(ValueError, match="nesting depth"):
                executor.execute_workflow(parent, motet)
        assert motet.do.call_count == 0

    def test_from_execution_data_isolates_steps(self) -> None:
        original = Workflow(
            workflow_id="self",
            name="Self",
            steps={
                "a": WorkflowStep(
                    step_id="a",
                    name="a",
                    command_type="core.tool_execution",
                    command_data={"tool_name": "core.echo", "parameters": {}},
                )
            },
        )
        data = original.to_execution_data()
        # Simulate registry fill-in path with workflow_steps=None
        data.workflow_steps = None
        frame1 = Workflow.from_execution_data(data, original_workflow=original)
        frame2 = Workflow.from_execution_data(data, original_workflow=original)
        assert frame1.steps["a"] is not frame2.steps["a"]
        assert frame1.steps["a"] is not original.steps["a"]
        frame1.steps["a"].requires_confirmation = True
        assert original.steps["a"].requires_confirmation is False


class TestOperatorPauseCancel:
    def test_cancel_paused_run_immediate(self) -> None:
        stored: Dict[str, Any] = {}

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        def fake_load(service: str, key: str, format_type: str = "hash") -> Any:
            return stored.get(key)

        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=fake_load), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=MagicMock()), \
             patch(
                 f"{REDIS_MANAGER}.acquire_distributed_lock_sync",
                 return_value=_lock_mock(),
             ):
            cp = _checkpoint()
            store_workflow_checkpoint(cp)
            result = request_workflow_run_control(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id=cp.workflow_run_id,
                action="cancel",
                principal_id="user-1",
            )
            assert result["status"] == "cancelled"
            assert result["applied"] is True
            loaded = load_workflow_checkpoint(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id=cp.workflow_run_id,
            )
            assert loaded is not None
            assert loaded.status == WorkflowRunStatus.CANCELLED
            with pytest.raises(WorkflowResumeConflict):
                claim_workflow_run_for_resume(
                    tenant_id="tenant-a",
                    motet_id="default",
                    workflow_run_id=cp.workflow_run_id,
                )

    def test_cancel_terminal_conflicts(self) -> None:
        stored: Dict[str, Any] = {}

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        def fake_load(service: str, key: str, format_type: str = "hash") -> Any:
            return stored.get(key)

        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=fake_load), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=MagicMock()):
            cp = _checkpoint(status=WorkflowRunStatus.COMPLETED)
            store_workflow_checkpoint(cp)
            with pytest.raises(WorkflowRunControlConflict, match="terminal"):
                request_workflow_run_control(
                    tenant_id="tenant-a",
                    motet_id="default",
                    workflow_run_id=cp.workflow_run_id,
                    action="cancel",
                )

    def test_pause_running_writes_signal(self) -> None:
        stored: Dict[str, Any] = {}

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        def fake_load(service: str, key: str, format_type: str = "hash") -> Any:
            return stored.get(key)

        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=fake_load), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=MagicMock()):
            cp = _checkpoint(status=WorkflowRunStatus.RUNNING, pending_interactions=[])
            store_workflow_checkpoint(cp)
            result = request_workflow_run_control(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id=cp.workflow_run_id,
                action="pause",
                reason="ops",
            )
            assert result["status"] == "pause_requested"
            signal = peek_workflow_run_control(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id=cp.workflow_run_id,
            )
            assert signal is not None
            assert signal["action"] == "pause"
            assert signal["reason"] == "ops"

    def test_executor_honors_cancel_between_levels(self) -> None:
        call_count = {"n": 0}

        def fake_call(impl, data=None, **kwargs):
            call_count["n"] += 1
            # After first step, request cancel so level 2 never runs.
            if call_count["n"] == 1:
                request_workflow_run_control(
                    tenant_id="tenant-a",
                    motet_id="default",
                    workflow_run_id=motet.metadata["workflow_run_id"],
                    action="cancel",
                )
            return {"status": "success", "data": {"n": call_count["n"]}}

        wf = Workflow(
            workflow_id="durable_demo",
            name="Durable",
            durable=True,
            steps={
                "a": WorkflowStep(
                    step_id="a",
                    name="a",
                    command_type="core.tool_execution",
                    command_data={"tool_name": "core.echo", "parameters": {"text": "a"}},
                ),
                "b": WorkflowStep(
                    step_id="b",
                    name="b",
                    command_type="core.tool_execution",
                    command_data={"tool_name": "core.echo", "parameters": {"text": "b"}},
                    dependencies=["a"],
                ),
            },
        )
        motet = _mock_motet()
        motet.metadata = {}
        motet.do = MagicMock(side_effect=fake_call)
        stored: Dict[str, Any] = {}

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        def fake_load(service: str, key: str, format_type: str = "hash") -> Any:
            return stored.get(key)

        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=fake_load), \
             patch(
                 f"{REDIS_MANAGER}.get_sync_redis_client",
                 return_value=_memory_redis(stored),
             ), \
             patch(
                 f"{REDIS_MANAGER}.acquire_distributed_lock_sync",
                 return_value=_lock_mock(),
             ):
            result = WorkflowExecutor().execute_workflow(wf, motet)

        assert result["status"] == "cancelled"
        assert call_count["n"] == 1  # step b never executed
        assert "a" in result["completed_step_ids"]
        assert "b" in result["pending_step_ids"]

    def test_executor_honors_pause_as_operator_suspend(self) -> None:
        def fake_call(impl, data=None, **kwargs):
            request_workflow_run_control(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id=motet.metadata["workflow_run_id"],
                action="pause",
            )
            return {"status": "success", "data": {"ok": True}}

        wf = Workflow(
            workflow_id="durable_pause",
            name="DurablePause",
            durable=True,
            steps={
                "a": WorkflowStep(
                    step_id="a",
                    name="a",
                    command_type="core.tool_execution",
                    command_data={"tool_name": "core.echo", "parameters": {"text": "a"}},
                ),
                "b": WorkflowStep(
                    step_id="b",
                    name="b",
                    command_type="core.tool_execution",
                    command_data={"tool_name": "core.echo", "parameters": {"text": "b"}},
                    dependencies=["a"],
                ),
            },
        )
        motet = _mock_motet()
        motet.metadata = {}
        motet.do = MagicMock(side_effect=fake_call)
        stored: Dict[str, Any] = {}

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        def fake_load(service: str, key: str, format_type: str = "hash") -> Any:
            return stored.get(key)

        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=fake_load), \
             patch(
                 f"{REDIS_MANAGER}.get_sync_redis_client",
                 return_value=_memory_redis(stored),
             ), \
             patch(
                 f"{REDIS_MANAGER}.acquire_distributed_lock_sync",
                 return_value=_lock_mock(),
             ):
            result = WorkflowExecutor().execute_workflow(wf, motet)

        assert result["status"] == "suspended"
        assert result["suspend_reason"] == "operator"
        assert result["pending_interactions"] == []
        assert (
            peek_workflow_run_control(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id=result["workflow_run_id"],
            )
            is None
        )

    def test_resume_operator_pause_continues(self) -> None:
        """Operator pause then kind=operator resume runs remaining Motet steps."""
        call_count = {"n": 0}
        stored: Dict[str, Any] = {}

        def fake_call(impl, data=None, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                request_workflow_run_control(
                    tenant_id="tenant-a",
                    motet_id="default",
                    workflow_run_id=motet.metadata["workflow_run_id"],
                    action="pause",
                )
            return {"status": "success", "data": {"n": call_count["n"]}}

        wf = Workflow(
            workflow_id="op_resume",
            name="OpResume",
            durable=True,
            steps={
                "a": WorkflowStep(
                    step_id="a",
                    name="a",
                    command_type="core.tool_execution",
                    command_data={"tool_name": "core.echo", "parameters": {"text": "a"}},
                ),
                "b": WorkflowStep(
                    step_id="b",
                    name="b",
                    command_type="core.tool_execution",
                    command_data={"tool_name": "core.echo", "parameters": {"text": "b"}},
                    dependencies=["a"],
                ),
            },
        )
        motet = _mock_motet()
        motet.metadata = {}
        motet.do = MagicMock(side_effect=fake_call)

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        def fake_load(service: str, key: str, format_type: str = "hash") -> Any:
            return stored.get(key)

        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=fake_load), \
             patch(
                 f"{REDIS_MANAGER}.get_sync_redis_client",
                 return_value=_memory_redis(stored),
             ), \
             patch(
                 f"{REDIS_MANAGER}.acquire_distributed_lock_sync",
                 return_value=_lock_mock(),
             ):
            paused = WorkflowExecutor().execute_workflow(wf, motet)
            assert paused["status"] == "suspended"
            assert paused["suspend_reason"] == "operator"
            run_id = paused["workflow_run_id"]
            # Control signal must be gone after the executor honors pause.
            assert (
                peek_workflow_run_control(
                    tenant_id="tenant-a", motet_id="default", workflow_run_id=run_id
                )
                is None
            )
            claimed = claim_workflow_run_for_resume(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id=run_id,
            )
            assert claimed is not None
            motet2 = _mock_motet()
            motet2.metadata = {}
            motet2.do = MagicMock(
                side_effect=lambda *a, **k: {"status": "success", "data": {"ok": True}}
            )
            result = WorkflowExecutor().resume_from_checkpoint(
                claimed, motet2, kind="operator"
            )

        assert result.get("status") == "completed"
        assert "b" in result["step_results"]
        assert motet2.do.call_count == 1

    def test_cancel_cascades_to_child(self) -> None:
        stored: Dict[str, Any] = {}

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        def fake_load(service: str, key: str, format_type: str = "hash") -> Any:
            return stored.get(key)

        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=fake_load), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=MagicMock()), \
             patch(
                 f"{REDIS_MANAGER}.acquire_distributed_lock_sync",
                 return_value=_lock_mock(),
             ):
            child = _checkpoint(
                workflow_run_id="wfrun-child",
                parent_workflow_run_id="wfrun-parent",
            )
            parent = _checkpoint(
                workflow_run_id="wfrun-parent",
                child_workflow_run_id="wfrun-child",
                blocked_step_id="summarize",
                pending_interactions=[],
                suspend_reason=WorkflowSuspendReason.HANDBACK_TOOLS,
            )
            store_workflow_checkpoint(child)
            store_workflow_checkpoint(parent)
            result = request_workflow_run_control(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id="wfrun-parent",
                action="cancel",
            )
            assert result["status"] == "cancelled"
            assert result["child"]["status"] == "cancelled"
            loaded_child = load_workflow_checkpoint(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id="wfrun-child",
            )
            loaded_parent = load_workflow_checkpoint(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id="wfrun-parent",
            )
            assert loaded_child is not None
            assert loaded_parent is not None
            assert loaded_child.status == WorkflowRunStatus.CANCELLED
            assert loaded_parent.status == WorkflowRunStatus.CANCELLED

    def test_cancel_cascades_to_parent_blocked_on_child(self) -> None:
        stored: Dict[str, Any] = {}

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        def fake_load(service: str, key: str, format_type: str = "hash") -> Any:
            return stored.get(key)

        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=fake_load), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=MagicMock()), \
             patch(
                 f"{REDIS_MANAGER}.acquire_distributed_lock_sync",
                 return_value=_lock_mock(),
             ):
            child = _checkpoint(
                workflow_run_id="wfrun-leaf",
                parent_workflow_run_id="wfrun-outer",
            )
            parent = _checkpoint(
                workflow_run_id="wfrun-outer",
                child_workflow_run_id="wfrun-leaf",
                blocked_step_id="nested",
                pending_interactions=[],
            )
            store_workflow_checkpoint(child)
            store_workflow_checkpoint(parent)
            result = request_workflow_run_control(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id="wfrun-leaf",
                action="cancel",
            )
            assert result["status"] == "cancelled"
            assert result["parent"]["status"] == "cancelled"
            loaded_parent = load_workflow_checkpoint(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id="wfrun-outer",
            )
            assert loaded_parent is not None
            assert loaded_parent.status == WorkflowRunStatus.CANCELLED

    def test_cancel_wins_over_prior_pause_signal(self) -> None:
        stored: Dict[str, Any] = {}

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        def fake_load(service: str, key: str, format_type: str = "hash") -> Any:
            return stored.get(key)

        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=fake_load), \
             patch(
                 f"{REDIS_MANAGER}.get_sync_redis_client",
                 return_value=_memory_redis(stored),
             ):
            cp = _checkpoint(status=WorkflowRunStatus.RUNNING, pending_interactions=[])
            store_workflow_checkpoint(cp)
            request_workflow_run_control(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id=cp.workflow_run_id,
                action="pause",
            )
            request_workflow_run_control(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id=cp.workflow_run_id,
                action="cancel",
            )
            # A later pause must not overwrite cancel.
            request_workflow_run_control(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id=cp.workflow_run_id,
                action="pause",
            )
            signal = peek_workflow_run_control(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id=cp.workflow_run_id,
            )
            assert signal is not None
            assert signal["action"] == "cancel"

    def test_cancel_running_wakes_registered_waiters(self) -> None:
        from motet.core.distributed.task_control import cancel_wake_key

        stored: Dict[str, Any] = {}
        wake = _WakeRedis()

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data
            wake.kv[key] = data

        def fake_load(service: str, key: str, format_type: str = "hash") -> Any:
            return stored.get(key)

        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=fake_load), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=wake):
            cp = _checkpoint(
                status=WorkflowRunStatus.RUNNING,
                pending_interactions=[],
                task_id="task-wake",
            )
            store_workflow_checkpoint(cp)
            register_workflow_control_waiter(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id=cp.workflow_run_id,
                waiter_id="celery-wf-1",
            )
            result = request_workflow_run_control(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id=cp.workflow_run_id,
                action="cancel",
            )
            assert result["status"] == "cancel_requested"
            assert is_workflow_cancelled(
                tenant_id="tenant-a",
                motet_id="default",
                workflow_run_id=cp.workflow_run_id,
            )
            assert wake.lists.get(cancel_wake_key("celery-wf-1"))

    def test_by_task_index_lists_active_run(self) -> None:
        stored: Dict[str, Any] = {}
        wake = _WakeRedis()

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data
            wake.kv[key] = data

        def fake_load(service: str, key: str, format_type: str = "hash") -> Any:
            return stored.get(key)

        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=fake_load), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=wake):
            cp = _checkpoint(
                status=WorkflowRunStatus.RUNNING,
                pending_interactions=[],
                task_id="task-bridge-1",
            )
            store_workflow_checkpoint(cp)
            listed = list_workflow_runs_for_task("task-bridge-1")
            assert len(listed) == 1
            assert listed[0]["workflow_run_id"] == cp.workflow_run_id

            cp.status = WorkflowRunStatus.COMPLETED
            store_workflow_checkpoint(cp)
            assert list_workflow_runs_for_task("task-bridge-1") == []


class TestWorkflowRunControlCommand:
    def test_control_command_pause_running(self) -> None:
        from motet.core.commands.builtin.workflow import workflow_run_control
        from motet.core.commands.command_data_classes import WorkflowRunControlData

        stored: Dict[str, Any] = {}

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        def fake_load(service: str, key: str, format_type: str = "hash") -> Any:
            return stored.get(key)

        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=fake_load), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=MagicMock()):
            cp = _checkpoint(status=WorkflowRunStatus.RUNNING, pending_interactions=[])
            store_workflow_checkpoint(cp)
            motet = _mock_motet()
            with patch(
                "motet.core.commands.builtin.workflow.get_motet_context",
                return_value=motet,
            ):
                result = workflow_run_control.__wrapped__(
                    WorkflowRunControlData(
                        workflow_run_id=cp.workflow_run_id,
                        action="pause",
                        reason="ops",
                    )
                )
            assert result["status"] == "pause_requested"
            assert result["action"] == "pause"

    def test_control_command_principal_mismatch(self) -> None:
        from motet.core.commands.builtin.workflow import workflow_run_control
        from motet.core.commands.command_data_classes import WorkflowRunControlData

        stored: Dict[str, Any] = {}

        def fake_store(service: str, key: str, data: Any, format_type: str = "hash") -> None:
            stored[key] = data

        def fake_load(service: str, key: str, format_type: str = "hash") -> Any:
            return stored.get(key)

        with patch(f"{REDIS_MANAGER}.store_structured_data_sync", side_effect=fake_store), \
             patch(f"{REDIS_MANAGER}.retrieve_structured_data_sync", side_effect=fake_load), \
             patch(f"{REDIS_MANAGER}.get_sync_redis_client", return_value=MagicMock()):
            cp = _checkpoint()
            store_workflow_checkpoint(cp)
            motet = _mock_motet(principal_id="other-user")
            with patch(
                "motet.core.commands.builtin.workflow.get_motet_context",
                return_value=motet,
            ):
                with pytest.raises(PermissionError, match="different principal"):
                    workflow_run_control.__wrapped__(
                        WorkflowRunControlData(
                            workflow_run_id=cp.workflow_run_id,
                            action="cancel",
                        )
                    )
