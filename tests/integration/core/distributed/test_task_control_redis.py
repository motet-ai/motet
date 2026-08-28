"""
Motet - Task Control Redis Integration Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-19

Description:
    Real-Redis integration tests for ADR-0131 task-level cooperative cancel
    and issue #242 gather/map wait-many. Verifies sticky
    ``{tenant}:task:control:{task_id}``, per-waiter BLPOP wakes
    (cancel + result), multi-waiter cancel fan-out, the live task index,
    ``cmd:outcome`` collection without EventBus completion events,
    hydrate of ``cmd:result`` pointers on wait retrieve, and parallel
    leftover waits.

Dependencies:
    - pytest, threading
    - motet.core.distributed.task_control
    - Live Redis via MOTET_REDIS_URL (requires_redis marker)

Usage:
    pytest tests/integration/core/distributed/test_task_control_redis.py -v

Notes:
    - Uses unique task/waiter ids and cleans keys in finally blocks.
    - Does not require Celery workers; exercises the Redis control plane only.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, List, Optional

import pytest

from motet.core.distributed import task_control as tc
from motet.core.distributed.redis_manager import get_sync_redis_client
from motet.core.distributed.tenant_keys import (
    task_control_key,
    task_live_key,
    task_response_stream,
    task_waiters_key,
)


def _unique_id(prefix: str = "tc") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _cleanup_task_keys(
    task_id: str,
    *,
    waiter_ids: Optional[List[str]] = None,
    tenant_id: Optional[str] = None,
    principal_id: Optional[str] = None,
) -> None:
    client = get_sync_redis_client("task_control")
    keys = [
        task_control_key(tenant_id, task_id),
        task_waiters_key(tenant_id, task_id),
        task_live_key(tenant_id, task_id),
        task_response_stream(tenant_id, task_id),
    ]
    if tenant_id:
        keys.extend(
            [
                task_control_key(None, task_id),
                task_waiters_key(None, task_id),
                task_live_key(None, task_id),
                task_response_stream(None, task_id),
            ]
        )
    for wid in waiter_ids or []:
        keys.extend(
            [
                tc.cancel_wake_key(wid),
                tc.result_wake_key(wid),
                tc.result_done_key(wid),
            ]
        )
    client.delete(*keys)
    if tenant_id is not None or principal_id is not None:
        client.srem(
            tc.live_index_key(tenant_id=tenant_id, principal_id=principal_id),
            task_id,
        )


@pytest.mark.integration
@pytest.mark.requires_redis
def test_real_redis_sticky_cancel_round_trip() -> None:
    """SET sticky cancel → EXISTS/is_task_cancelled → stream event written."""
    task_id = _unique_id("sticky")
    waiter_id = _unique_id("w")
    client = get_sync_redis_client("task_control")
    try:
        assert tc.is_task_cancelled(task_id) is False
        tc.register_live_task(
            task_id,
            tenant_id="tenant-integration",
            principal_id="principal-integration",
            command_type="core.agent_turn",
        )
        tc.register_command_waiter(
            task_id, waiter_id, tenant_id="tenant-integration"
        )

        payload = tc.request_task_cancel(
            task_id,
            reason="integration sticky",
            principal_id="principal-integration",
            source="integration_test",
            tenant_id="tenant-integration",
        )
        assert payload["action"] == "cancel"
        assert tc.is_task_cancelled(task_id, tenant_id="tenant-integration") is True
        assert client.exists(tc.control_key(task_id, tenant_id="tenant-integration")) == 1
        assert client.llen(tc.cancel_wake_key(waiter_id)) >= 1
        assert client.xlen(task_response_stream("tenant-integration", task_id)) >= 1
    finally:
        _cleanup_task_keys(
            task_id,
            waiter_ids=[waiter_id],
            tenant_id="tenant-integration",
            principal_id="principal-integration",
        )


@pytest.mark.integration
@pytest.mark.requires_redis
def test_real_redis_cancel_wakes_waiting_thread() -> None:
    """Waiting thread unblocks promptly when another thread requests cancel."""
    task_id = _unique_id("wake")
    result: List[Any] = [None]
    errors: List[BaseException] = []
    started = threading.Event()

    def _waiter() -> None:
        try:
            started.set()
            time.sleep(0.05)
            result[0] = tc.wait_for_command_outcome(
                task_id,
                waiter_id,
                timeout_seconds=5.0,
            )
        except BaseException as e:
            errors.append(e)

    waiter_id = _unique_id("w")
    try:
        assert tc.is_task_cancelled(task_id) is False
        t = threading.Thread(target=_waiter, name="task-cancel-waiter", daemon=True)
        t.start()
        assert started.wait(timeout=2.0)
        time.sleep(0.15)
        t0 = time.time()
        tc.request_task_cancel(
            task_id,
            reason="wake waiter",
            principal_id="principal-integration",
            source="integration_test",
        )
        t.join(timeout=5.0)
        elapsed = time.time() - t0

        assert not errors, f"waiter raised: {errors}"
        assert t.is_alive() is False
        assert result[0] is not None
        assert result[0].outcome == "cancelled"
        assert elapsed < 2.0, f"wake too slow: {elapsed:.3f}s"
    finally:
        _cleanup_task_keys(task_id, waiter_ids=[waiter_id])


@pytest.mark.integration
@pytest.mark.requires_redis
def test_real_redis_cancel_fans_out_to_multiple_waiters() -> None:
    """Each parked parent has a private wake list — cancel wakes all of them."""
    task_id = _unique_id("fanout")
    w1, w2 = _unique_id("w1"), _unique_id("w2")
    outcomes: List[Any] = [None, None]
    errors: List[BaseException] = []
    barrier = threading.Barrier(3)

    def _run(idx: int, wid: str) -> None:
        try:
            barrier.wait(timeout=5)
            outcomes[idx] = tc.wait_for_command_outcome(
                task_id, wid, timeout_seconds=5.0
            )
        except BaseException as e:
            errors.append(e)

    try:
        t1 = threading.Thread(target=_run, args=(0, w1), daemon=True)
        t2 = threading.Thread(target=_run, args=(1, w2), daemon=True)
        t1.start()
        t2.start()
        barrier.wait(timeout=5)
        time.sleep(0.1)
        tc.request_task_cancel(task_id, reason="fanout", source="integration_test")
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert not errors, f"waiters raised: {errors}"
        assert [o.outcome for o in outcomes] == ["cancelled", "cancelled"]
    finally:
        _cleanup_task_keys(task_id, waiter_ids=[w1, w2])


@pytest.mark.integration
@pytest.mark.requires_redis
def test_real_redis_result_wake_completes_wait() -> None:
    """Result done + LPUSH unblocks wait_for_command_outcome as completed."""
    task_id = _unique_id("result")
    waiter_id = _unique_id("celery")
    outcome_box: List[Any] = [None]
    started = threading.Event()

    def _waiter() -> None:
        started.set()
        outcome_box[0] = tc.wait_for_command_outcome(
            task_id, waiter_id, timeout_seconds=5.0
        )

    try:
        t = threading.Thread(target=_waiter, daemon=True)
        t.start()
        assert started.wait(timeout=2.0)
        time.sleep(0.1)
        t0 = time.time()
        tc.signal_command_result(waiter_id)
        t.join(timeout=5.0)
        assert outcome_box[0] is not None
        assert outcome_box[0].outcome == "completed"
        assert time.time() - t0 < 2.0
    finally:
        _cleanup_task_keys(task_id, waiter_ids=[waiter_id])


@pytest.mark.integration
@pytest.mark.requires_redis
def test_real_redis_subscribe_after_publish_still_sees_cancel() -> None:
    """Sticky source of truth: cancel before wait still returns True immediately."""
    task_id = _unique_id("race")
    try:
        tc.request_task_cancel(
            task_id,
            reason="published first",
            principal_id="principal-integration",
            source="integration_test",
        )
        t0 = time.time()
        waited = tc.wait_for_command_outcome(
            task_id,
            _unique_id("w"),
            timeout_seconds=2.0,
        )
        elapsed = time.time() - t0
        assert waited.outcome == "cancelled"
        assert elapsed < 0.5, f"sticky entry check should be immediate, took {elapsed:.3f}s"
    finally:
        _cleanup_task_keys(task_id)


@pytest.mark.integration
@pytest.mark.requires_redis
def test_real_redis_live_index_and_ownership() -> None:
    """Live register → list → cancel status → unregister against real Redis."""
    task_id = _unique_id("live")
    tenant_id = f"tenant-{uuid.uuid4().hex[:8]}"
    principal_id = f"user-{uuid.uuid4().hex[:8]}"
    try:
        tc.register_live_task(
            task_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            conversation_id="conv-integration",
            command_type="core.agent_turn",
            root_command_id="cmd-root",
        )
        listed = tc.list_live_tasks(tenant_id=tenant_id, principal_id=principal_id)
        assert any(m.get("task_id") == task_id for m in listed)

        meta = tc.get_live_task(task_id, tenant_id=tenant_id)
        assert meta is not None
        assert tc.live_task_owned_by(meta, principal_id=principal_id) is True
        assert tc.live_task_owned_by(meta, principal_id="someone-else") is False

        tc.request_task_cancel(
            task_id,
            reason="operator",
            principal_id=principal_id,
            source="integration_test",
            tenant_id=tenant_id,
        )
        meta_after = tc.get_live_task(task_id, tenant_id=tenant_id)
        assert meta_after is not None
        assert meta_after.get("status") == "cancelled"

        tc.unregister_live_task(
            task_id, tenant_id=tenant_id, principal_id=principal_id
        )
        assert tc.get_live_task(task_id, tenant_id=tenant_id) is None
    finally:
        _cleanup_task_keys(
            task_id, tenant_id=tenant_id, principal_id=principal_id
        )


@pytest.mark.integration
@pytest.mark.requires_redis
def test_real_redis_wait_times_out_when_not_cancelled() -> None:
    """Waiter returns timeout when no cancel or result wake arrives."""
    task_id = _unique_id("stop")
    waiter_id = _unique_id("w")
    try:
        t0 = time.time()
        waited = tc.wait_for_command_outcome(
            task_id,
            waiter_id,
            timeout_seconds=0.4,
        )
        elapsed = time.time() - t0
        assert waited.outcome == "timeout"
        assert elapsed < 2.5, f"timeout exit too slow: {elapsed:.3f}s"
        assert tc.is_task_cancelled(task_id) is False
    finally:
        _cleanup_task_keys(task_id, waiter_ids=[waiter_id])


@pytest.mark.integration
@pytest.mark.requires_redis
def test_real_redis_wait_many_sticky_then_live_wake() -> None:
    """wait-many: already-done child is sticky; sibling completes via result wake."""
    task_id = _unique_id("many")
    sticky_id = _unique_id("sticky")
    live_id = _unique_id("live")
    outcome_box: List[Any] = [None]
    started = threading.Event()

    def _waiter() -> None:
        started.set()
        outcome_box[0] = tc.wait_for_command_outcomes(
            task_id,
            [sticky_id, live_id],
            timeout_seconds=5.0,
        )

    try:
        tc.signal_command_result(sticky_id)
        t = threading.Thread(target=_waiter, daemon=True)
        t.start()
        assert started.wait(timeout=2.0)
        time.sleep(0.1)
        tc.signal_command_result(live_id)
        t.join(timeout=5.0)
        waited = outcome_box[0]
        assert waited is not None
        assert waited[sticky_id].outcome == "completed"
        assert waited[live_id].outcome == "completed"
    finally:
        _cleanup_task_keys(task_id, waiter_ids=[sticky_id, live_id])


@pytest.mark.integration
@pytest.mark.requires_redis
def test_real_redis_wait_many_leftovers_complete_in_parallel() -> None:
    """Two live leftovers finish in ~max(child), not the sum of waits."""
    task_id = _unique_id("par")
    waiter_a = _unique_id("pa")
    waiter_b = _unique_id("pb")
    outcome_box: List[Any] = [None]
    started = threading.Event()

    def _waiter() -> None:
        started.set()
        outcome_box[0] = tc.wait_for_command_outcomes(
            task_id,
            [waiter_a, waiter_b],
            timeout_seconds=5.0,
        )

    try:
        t0 = time.time()
        t = threading.Thread(target=_waiter, daemon=True)
        t.start()
        assert started.wait(timeout=2.0)
        time.sleep(0.2)
        tc.signal_command_result(waiter_a)
        tc.signal_command_result(waiter_b)
        t.join(timeout=5.0)
        elapsed = time.time() - t0
        waited = outcome_box[0]
        assert waited is not None
        assert waited[waiter_a].outcome == "completed"
        assert waited[waiter_b].outcome == "completed"
        assert elapsed < 1.5
    finally:
        _cleanup_task_keys(task_id, waiter_ids=[waiter_a, waiter_b])


@pytest.mark.integration
@pytest.mark.requires_redis
def test_real_redis_wait_many_cancel_aborts_remaining() -> None:
    """Parent cancel stops wait-many without parking on leftover children."""
    task_id = _unique_id("abort")
    first_id = _unique_id("first")
    leftover_id = _unique_id("left")
    outcome_box: List[Any] = [None]
    started = threading.Event()

    def _waiter() -> None:
        started.set()
        outcome_box[0] = tc.wait_for_command_outcomes(
            task_id,
            [first_id, leftover_id],
            timeout_seconds=5.0,
            cancel_scopes=[task_id],
        )

    try:
        t = threading.Thread(target=_waiter, daemon=True)
        t.start()
        assert started.wait(timeout=2.0)
        time.sleep(0.1)
        tc.request_task_cancel(
            task_id,
            reason="abort remaining",
            source="integration_test",
        )
        t.join(timeout=5.0)
        waited = outcome_box[0]
        assert waited is not None
        assert waited[first_id].outcome == "cancelled"
        assert waited[leftover_id].outcome == "cancelled"
    finally:
        _cleanup_task_keys(task_id, waiter_ids=[first_id, leftover_id])


@pytest.mark.integration
@pytest.mark.requires_redis
def test_real_redis_collect_child_results_without_events() -> None:
    """Dropped completion events do not matter: payload is cmd:outcome."""
    from types import SimpleNamespace

    from unittest.mock import patch

    from motet.core.commands.concurrency import collect_child_wait_results
    from motet.core.distributed.redis_command_data_manager import (
        RedisCommandDataManager,
    )
    from motet.core.distributed.redis_manager import get_sync_binary_redis_client

    task_id = _unique_id("join")
    waiter_a = _unique_id("wa")
    waiter_b = _unique_id("wb")
    cmd_a = _unique_id("cmd-a")
    cmd_b = _unique_id("cmd-b")
    manager = RedisCommandDataManager(
        redis_client=get_sync_binary_redis_client("command_data_manager"),
        enable_encryption=False,
    )
    children = [
        SimpleNamespace(command_id=cmd_a, get_command_type=lambda: "cmd.a"),
        SimpleNamespace(command_id=cmd_b, get_command_type=lambda: "cmd.b"),
    ]
    try:
        manager.store_command_wait_outcome(
            cmd_a,
            {
                "status": "completed",
                "command_id": cmd_a,
                "command_type": "cmd.a",
                "result": {"status": "success", "data": {"v": "a"}, "metadata": {}},
            },
        )
        manager.store_command_wait_outcome(
            cmd_b,
            {
                "status": "completed",
                "command_id": cmd_b,
                "command_type": "cmd.b",
                "result": {"status": "success", "data": {"v": "b"}, "metadata": {}},
            },
        )
        tc.signal_command_result(waiter_a)
        tc.signal_command_result(waiter_b)

        with patch(
            "motet.core.distributed.redis_command_data_manager.get_redis_command_data_manager",
            return_value=manager,
        ), patch(
            "motet.core.workers.event_observer_manager.register_event_observer",
        ) as register_observer:
            collected = collect_child_wait_results(
                task_id=task_id,
                child_commands=children,  # type: ignore[arg-type]
                waiter_ids=[waiter_a, waiter_b],
                timeout_seconds=5.0,
            )
        register_observer.assert_not_called()
        assert [item["command_id"] for item in collected] == [cmd_a, cmd_b]
        assert [item["result"]["data"]["v"] for item in collected] == ["a", "b"]
        assert all(item["status"] == "success" for item in collected)
    finally:
        client = get_sync_redis_client("task_control")
        client.delete(
            manager._command_key("outcome", cmd_a),
            manager._command_key("outcome", cmd_b),
            manager._command_key("meta", cmd_a),
            manager._command_key("meta", cmd_b),
        )
        _cleanup_task_keys(task_id, waiter_ids=[waiter_a, waiter_b])


@pytest.mark.integration
@pytest.mark.requires_redis
def test_real_redis_collect_child_hydrates_result_pointer() -> None:
    """Gather fan-in follows cmd:result pointers stored in cmd:outcome."""
    from types import SimpleNamespace

    from unittest.mock import patch

    from motet.core.commands.concurrency import collect_child_wait_results
    from motet.core.distributed.redis_command_data_manager import (
        RedisCommandDataManager,
    )
    from motet.core.distributed.redis_manager import get_sync_binary_redis_client

    task_id = _unique_id("join-hydrate")
    waiter_id = _unique_id("wh")
    command_id = _unique_id("cmd-h")
    manager = RedisCommandDataManager(
        redis_client=get_sync_binary_redis_client("command_data_manager"),
        enable_encryption=False,
    )
    domain = {
        "status": "success",
        "data": {"tool_name": "core.tools_search", "hits": 2},
        "metadata": {},
    }
    result_key = manager.store_command_result(
        command_id,
        domain,
        command_type="core.tool_execution",
    )
    children = [
        SimpleNamespace(
            command_id=command_id, get_command_type=lambda: "core.tool_execution"
        ),
    ]
    try:
        manager.store_command_wait_outcome(
            command_id,
            {
                "status": "completed",
                "command_id": command_id,
                "command_type": "core.tool_execution",
                "result": {"_redis_result_key": result_key},
            },
        )
        tc.signal_command_result(waiter_id)

        with patch(
            "motet.core.distributed.redis_command_data_manager.get_redis_command_data_manager",
            return_value=manager,
        ):
            collected = collect_child_wait_results(
                task_id=task_id,
                child_commands=children,  # type: ignore[arg-type]
                waiter_ids=[waiter_id],
                timeout_seconds=5.0,
            )

        assert collected[0]["status"] == "success"
        assert collected[0]["result"]["data"]["tool_name"] == "core.tools_search"
        assert collected[0]["result"]["data"]["hits"] == 2
    finally:
        client = get_sync_redis_client("task_control")
        client.delete(
            manager._command_key("outcome", command_id),
            manager._command_key("result", command_id),
            result_key,
        )
        _cleanup_task_keys(task_id, waiter_ids=[waiter_id])
