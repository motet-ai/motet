"""
Motet - Gather/Map cmd:outcome Fan-In Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-23

Description:
    Unit tests for issue #242: gather/map fan-in via Motet cmd:outcome waits
    instead of GatherObserver EventBus completion events.

Dependencies:
    - pytest
    - unittest.mock
    - motet.core.commands.concurrency

Usage:
    pytest -q tests/unit/core/test_gather_cmd_outcome.py

Notes:
    - Pure unit tests; EventBus is not required and must not be registered.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

from motet.core.commands.concurrency import (
    GatherCommand,
    MapCommand,
    collect_child_wait_results,
    map_wait_outcome_to_observer_result,
)
from motet.core.distributed.task_control import CommandWaitResult


def test_map_completed_outcome_to_success_envelope() -> None:
    mapped = map_wait_outcome_to_observer_result(
        command_id="c1",
        command_type="core.echo",
        wait_outcome="completed",
        envelope={
            "status": "completed",
            "command_id": "c1",
            "command_type": "core.echo",
            "result": {"status": "success", "data": {"v": 1}, "metadata": {}},
            "worker_id": "w1",
        },
    )
    assert mapped["status"] == "success"
    assert mapped["result"]["data"]["v"] == 1


def test_map_error_outcome_to_error_envelope() -> None:
    mapped = map_wait_outcome_to_observer_result(
        command_id="c1",
        command_type="core.echo",
        wait_outcome="completed",
        envelope={"status": "error", "error": "boom", "command_id": "c1"},
    )
    assert mapped["status"] == "error"
    assert mapped["error"]["message"] == "boom"


def test_map_timeout_and_cancelled() -> None:
    timed = map_wait_outcome_to_observer_result(
        command_id="c1",
        command_type="core.echo",
        wait_outcome="timeout",
        envelope=None,
    )
    assert timed["status"] == "timeout"
    cancelled = map_wait_outcome_to_observer_result(
        command_id="c1",
        command_type="core.echo",
        wait_outcome="cancelled",
        envelope=None,
        cancelled_scope="task-1",
    )
    assert cancelled["status"] == "error"
    assert cancelled["error"]["details"]["code"] == "task_cancelled"


def test_collect_child_wait_results_does_not_need_events() -> None:
    """Dropped completion events must not matter — payload is cmd:outcome."""
    children = [
        SimpleNamespace(command_id="id-a", get_command_type=lambda: "cmd.a"),
        SimpleNamespace(command_id="id-b", get_command_type=lambda: "cmd.b"),
    ]
    envelopes = {
        "id-a": {
            "status": "completed",
            "command_id": "id-a",
            "command_type": "cmd.a",
            "result": {"status": "success", "data": {"v": "a"}, "metadata": {}},
        },
        "id-b": {
            "status": "completed",
            "command_id": "id-b",
            "command_type": "cmd.b",
            "result": {"status": "success", "data": {"v": "b"}, "metadata": {}},
        },
    }
    manager = MagicMock()
    manager.retrieve_command_wait_outcome.side_effect = (
        lambda command_id, **_kwargs: envelopes[command_id]
    )
    waited = {
        "celery-a": CommandWaitResult("completed"),
        "celery-b": CommandWaitResult("completed"),
    }
    with patch(
        "motet.core.distributed.task_control.wait_for_command_outcomes",
        return_value=waited,
    ), patch(
        "motet.core.distributed.redis_command_data_manager.get_redis_command_data_manager",
        return_value=manager,
    ), patch(
        "motet.core.workers.event_observer_manager.register_event_observer",
    ) as register_observer:
        collected = collect_child_wait_results(
            task_id="task-1",
            child_commands=children,  # type: ignore[arg-type]
            waiter_ids=["celery-a", "celery-b"],
            timeout_seconds=5.0,
            tenant_id="acme",
            motet_id="default",
        )

    register_observer.assert_not_called()
    assert [item["command_id"] for item in collected] == ["id-a", "id-b"]
    assert [item["result"]["data"]["v"] for item in collected] == ["a", "b"]
    assert all(item["status"] == "success" for item in collected)
    manager.retrieve_command_wait_outcome.assert_called()


def test_collect_skips_wait_when_outcomes_already_stored() -> None:
    """Fan-in must not BLPOP children that already wrote cmd:outcome."""
    children = [
        SimpleNamespace(command_id="id-a", get_command_type=lambda: "cmd.a"),
        SimpleNamespace(command_id="id-b", get_command_type=lambda: "cmd.b"),
    ]
    envelopes = {
        "id-a": {
            "status": "completed",
            "result": {"status": "success", "data": {"v": "a"}, "metadata": {}},
        },
        "id-b": {
            "status": "completed",
            "result": {"status": "success", "data": {"v": "b"}, "metadata": {}},
        },
    }
    manager = MagicMock()
    manager.retrieve_command_wait_outcome.side_effect = (
        lambda command_id, **_kwargs: envelopes[command_id]
    )
    with patch(
        "motet.core.distributed.task_control.wait_for_command_outcomes",
    ) as wait, patch(
        "motet.core.distributed.redis_command_data_manager.get_redis_command_data_manager",
        return_value=manager,
    ):
        collected = collect_child_wait_results(
            task_id="task-1",
            child_commands=children,  # type: ignore[arg-type]
            waiter_ids=["celery-a", "celery-b"],
            timeout_seconds=5.0,
        )

    wait.assert_not_called()
    assert [item["result"]["data"]["v"] for item in collected] == ["a", "b"]


def test_collect_hydrates_envelope_when_wake_times_out() -> None:
    """A missed result wake must not hide a stored child envelope."""
    children = [
        SimpleNamespace(command_id="id-a", get_command_type=lambda: "cmd.a"),
        SimpleNamespace(command_id="id-b", get_command_type=lambda: "cmd.b"),
    ]
    envelopes = {
        "id-a": None,
        "id-b": {
            "status": "completed",
            "result": {"status": "success", "data": {"v": "b"}, "metadata": {}},
        },
    }

    def _retrieve(command_id: str, **_kwargs: Any) -> Any:
        return envelopes[command_id]

    manager = MagicMock()
    manager.retrieve_command_wait_outcome.side_effect = _retrieve
    with patch(
        "motet.core.distributed.task_control.wait_for_command_outcomes",
        return_value={"celery-a": CommandWaitResult("timeout")},
    ) as wait, patch(
        "motet.core.distributed.redis_command_data_manager.get_redis_command_data_manager",
        return_value=manager,
    ):
        collected = collect_child_wait_results(
            task_id="task-1",
            child_commands=children,  # type: ignore[arg-type]
            waiter_ids=["celery-a", "celery-b"],
            timeout_seconds=5.0,
        )

    wait.assert_called_once()
    assert wait.call_args[0][1] == ["celery-a"]
    assert collected[0]["status"] == "timeout"
    assert collected[1]["status"] == "success"
    assert collected[1]["result"]["data"]["v"] == "b"


def test_collect_hydrates_completed_outcomes_in_parallel() -> None:
    """Two completed children hydrate at the same time, not one after the other."""
    import time

    children = [
        SimpleNamespace(command_id="id-a", get_command_type=lambda: "cmd.a"),
        SimpleNamespace(command_id="id-b", get_command_type=lambda: "cmd.b"),
    ]
    envelopes = {
        "id-a": {
            "status": "completed",
            "result": {"status": "success", "data": {"v": "a"}, "metadata": {}},
        },
        "id-b": {
            "status": "completed",
            "result": {"status": "success", "data": {"v": "b"}, "metadata": {}},
        },
    }

    def _slow_retrieve(command_id: str, **_kwargs: Any) -> Dict[str, Any]:
        time.sleep(0.15)
        return envelopes[command_id]

    manager = MagicMock()
    manager.retrieve_command_wait_outcome.side_effect = _slow_retrieve
    t0 = time.time()
    with patch(
        "motet.core.distributed.task_control.wait_for_command_outcomes",
        return_value={
            "celery-a": CommandWaitResult("completed"),
            "celery-b": CommandWaitResult("completed"),
        },
    ), patch(
        "motet.core.distributed.redis_command_data_manager.get_redis_command_data_manager",
        return_value=manager,
    ):
        collected = collect_child_wait_results(
            task_id="task-1",
            child_commands=children,  # type: ignore[arg-type]
            waiter_ids=["celery-a", "celery-b"],
            timeout_seconds=5.0,
        )
    elapsed = time.time() - t0
    assert [item["result"]["data"]["v"] for item in collected] == ["a", "b"]
    assert elapsed < 0.26


def test_collect_preserves_submission_order_after_scrambled_waits() -> None:
    children = [
        SimpleNamespace(command_id="id-grammar", get_command_type=lambda: "cmd.g"),
        SimpleNamespace(command_id="id-tone", get_command_type=lambda: "cmd.t"),
        SimpleNamespace(command_id="id-accuracy", get_command_type=lambda: "cmd.a"),
    ]
    envelopes = {
        "id-grammar": {
            "status": "completed",
            "result": {"status": "success", "data": {"perspective": "grammar"}},
        },
        "id-tone": {
            "status": "completed",
            "result": {"status": "success", "data": {"perspective": "tone"}},
        },
        "id-accuracy": {
            "status": "completed",
            "result": {"status": "success", "data": {"perspective": "accuracy"}},
        },
    }
    manager = MagicMock()
    manager.retrieve_command_wait_outcome.side_effect = (
        lambda command_id, **_kwargs: envelopes[command_id]
    )
    with patch(
        "motet.core.distributed.task_control.wait_for_command_outcomes",
        return_value={
            "w-acc": CommandWaitResult("completed"),
            "w-gram": CommandWaitResult("completed"),
            "w-tone": CommandWaitResult("completed"),
        },
    ), patch(
        "motet.core.distributed.redis_command_data_manager.get_redis_command_data_manager",
        return_value=manager,
    ):
        collected = collect_child_wait_results(
            task_id="task-1",
            child_commands=children,  # type: ignore[arg-type]
            waiter_ids=["w-gram", "w-tone", "w-acc"],
            timeout_seconds=5.0,
        )

    assert [item["result"]["data"]["perspective"] for item in collected] == [
        "grammar",
        "tone",
        "accuracy",
    ]


def test_gather_execute_does_not_register_event_observer() -> None:
    child = SimpleNamespace(
        command_id="id-a",
        get_command_type=lambda: "cmd.a",
        distributed_context=SimpleNamespace(timeout_seconds=10),
    )
    mapped: List[Dict[str, Any]] = [
        {
            "command_id": "id-a",
            "command_type": "cmd.a",
            "status": "success",
            "result": {"status": "success", "data": {"ok": True}, "metadata": {}},
        }
    ]
    gather = GatherCommand.__new__(GatherCommand)
    gather.command_id = "gather-1"
    gather.data = SimpleNamespace(
        aggregation_strategy="all_results",
        fail_fast=False,
        max_parallel=None,
    )
    gather.distributed_context = SimpleNamespace(
        cancel_scopes=["task-1"],
        tenant_id="acme",
        motet_id="default",
        task_id="task-1",
    )
    gather._create_success_response = lambda **kwargs: {"status": "success", **kwargs}
    gather._create_partial_success_response = MagicMock()
    gather._create_error_response = MagicMock()

    with patch.object(
        GatherCommand, "_deserialize_child_commands", return_value=[child]
    ), patch.object(
        GatherCommand, "_create_routed_tasks", return_value=["sig-a"]
    ), patch(
        "motet.core.commands.concurrency._dispatch_signatures",
        return_value=["celery-a"],
    ) as dispatch, patch(
        "motet.core.commands.concurrency.collect_child_wait_results",
        return_value=mapped,
    ), patch(
        "motet.core.workers.event_observer_manager.register_event_observer",
    ) as register_observer:
        result = GatherCommand._do_execute(gather, {"worker_id": "w1"})

    register_observer.assert_not_called()
    dispatch.assert_called_once()
    assert result["status"] == "success"
    assert result["data"]["results"][0]["data"]["ok"] is True


def test_gather_chunked_max_parallel_waits_per_chunk() -> None:
    children = [
        SimpleNamespace(
            command_id=f"id-{i}",
            get_command_type=lambda i=i: f"cmd.{i}",
            distributed_context=SimpleNamespace(timeout_seconds=10),
        )
        for i in range(3)
    ]
    gather = GatherCommand.__new__(GatherCommand)
    gather.command_id = "gather-chunk"
    gather.data = SimpleNamespace(
        aggregation_strategy="all_results",
        fail_fast=False,
        max_parallel=2,
    )
    gather.distributed_context = SimpleNamespace(
        cancel_scopes=["task-1"],
        tenant_id="acme",
        motet_id="default",
        task_id="task-1",
    )
    gather._create_success_response = lambda **kwargs: {"status": "success", **kwargs}
    gather._create_partial_success_response = MagicMock()
    gather._create_error_response = MagicMock()

    def _collect(**kwargs: Any) -> List[Dict[str, Any]]:
        return [
            {
                "command_id": cmd.command_id,
                "command_type": cmd.get_command_type(),
                "status": "success",
                "result": {"status": "success", "data": {"ok": True}, "metadata": {}},
            }
            for cmd in kwargs["child_commands"]
        ]

    with patch.object(
        GatherCommand, "_deserialize_child_commands", return_value=children
    ), patch.object(
        GatherCommand, "_create_routed_tasks", return_value=["s0", "s1", "s2"]
    ), patch(
        "motet.core.commands.concurrency._dispatch_signatures",
        side_effect=lambda sigs: [f"w-{i}" for i in range(len(list(sigs)))],
    ) as dispatch, patch(
        "motet.core.commands.concurrency.collect_child_wait_results",
        side_effect=_collect,
    ) as collect:
        result = GatherCommand._do_execute(gather, {"worker_id": "w1"})

    assert dispatch.call_count == 2
    assert collect.call_count == 2
    assert [cmd.command_id for cmd in collect.call_args_list[0].kwargs["child_commands"]] == [
        "id-0",
        "id-1",
    ]
    assert [cmd.command_id for cmd in collect.call_args_list[1].kwargs["child_commands"]] == [
        "id-2",
    ]
    assert result["status"] == "success"
    assert result["data"]["successful"] == 3


def test_map_batch_size_waits_per_chunk() -> None:
    children = [
        SimpleNamespace(
            command_id=f"id-{i}",
            get_command_type=lambda i=i: f"cmd.{i}",
        )
        for i in range(3)
    ]
    map_cmd = MapCommand.__new__(MapCommand)
    map_cmd.command_id = "map-chunk"
    map_cmd.data = SimpleNamespace(
        command_type="cmd.x",
        batch_size=2,
        max_parallel=None,
        inputs=[{}, {}, {}],
        aggregation_strategy="all_results",
    )
    map_cmd.distributed_context = SimpleNamespace(
        cancel_scopes=["task-1"],
        tenant_id="acme",
        motet_id="default",
        task_id="task-1",
        timeout_seconds=30,
    )
    map_cmd._create_error_response = MagicMock()

    def _collect(**kwargs: Any) -> List[Dict[str, Any]]:
        return [
            {
                "command_id": cmd.command_id,
                "command_type": cmd.get_command_type(),
                "status": "success",
                "result": {"status": "success", "data": {"ok": True}, "metadata": {}},
            }
            for cmd in kwargs["child_commands"]
        ]

    with patch.object(
        MapCommand, "_create_command_instances", return_value=children
    ), patch.object(
        MapCommand, "_create_routed_tasks", return_value=["s0", "s1", "s2"]
    ), patch(
        "motet.core.commands.concurrency._dispatch_signatures",
        side_effect=lambda sigs: [f"w-{i}" for i in range(len(list(sigs)))],
    ) as dispatch, patch(
        "motet.core.commands.concurrency.collect_child_wait_results",
        side_effect=_collect,
    ) as collect:
        result = MapCommand._do_execute(map_cmd, {"worker_id": "w1"})

    assert dispatch.call_count == 2
    assert collect.call_count == 2
    assert [cmd.command_id for cmd in collect.call_args_list[0].kwargs["child_commands"]] == [
        "id-0",
        "id-1",
    ]
    assert [cmd.command_id for cmd in collect.call_args_list[1].kwargs["child_commands"]] == [
        "id-2",
    ]
    assert result["status"] == "success"
    assert result["data"]["successful"] == 3
