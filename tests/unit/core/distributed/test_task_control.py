"""
Motet - Task Control Unit Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Unit tests for ADR-0131 sticky task cancel + per-waiter BLPOP wakes + live
    index, plus gather/map wait-many (issue #242).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Union
from unittest.mock import MagicMock, patch

import pytest

from motet.core.distributed import task_control as tc


class _FakeRedis:
    def __init__(self) -> None:
        self.kv: Dict[str, Any] = {}
        self.sets: Dict[str, set] = {}
        self.lists: Dict[str, list] = {}
        self.streams: Dict[str, list] = {}

    def exists(self, *keys: str) -> int:
        return sum(1 for key in keys if key in self.kv)

    def delete(self, *keys: str) -> int:
        n = 0
        for key in keys:
            if key in self.kv:
                del self.kv[key]
                n += 1
            if key in self.lists:
                del self.lists[key]
                n += 1
        return n

    def expire(self, key: str, ttl: int) -> bool:
        return True

    def set(self, key: str, value: Any, ex: Optional[int] = None) -> bool:
        self.kv[key] = value
        return True

    def get(self, key: str) -> Any:
        return self.kv.get(key)

    def lpush(self, key: str, value: Any) -> int:
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    def lpop(self, key: str) -> Optional[Any]:
        items = self.lists.get(key) or []
        if not items:
            return None
        return items.pop(0)

    def blpop(
        self,
        keys: Union[str, Sequence[str]],
        timeout: int = 0,
    ) -> Optional[tuple]:
        key_list: List[str] = [keys] if isinstance(keys, str) else list(keys)
        for key in key_list:
            items = self.lists.get(key) or []
            if items:
                return (key, items.pop(0))
        return None

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

    def xadd(self, key: str, fields: Dict[str, Any], maxlen: int = 0) -> str:
        self.streams.setdefault(key, []).append(fields)
        return "1-0"

    def scan(self, cursor: int, match: str, count: int = 50):
        import fnmatch

        found = [k for k in self.kv if fnmatch.fnmatch(k, match)]
        return 0, found


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    fake = _FakeRedis()

    def _store(service: str, key: str, payload: Dict[str, Any], format_type: str = "json_string") -> None:
        fake.kv[key] = dict(payload)

    def _retrieve(service: str, key: str, format_type: str = "json_string") -> Optional[Dict[str, Any]]:
        val = fake.kv.get(key)
        return dict(val) if isinstance(val, dict) else None

    monkeypatch.setattr(
        "motet.core.distributed.redis_manager.get_sync_redis_client",
        lambda _service="default": fake,
    )
    monkeypatch.setattr(
        "motet.core.distributed.redis_manager.store_structured_data_sync",
        _store,
    )
    monkeypatch.setattr(
        "motet.core.distributed.redis_manager.retrieve_structured_data_sync",
        _retrieve,
    )
    return fake


def test_empty_task_id_is_never_cancelled(fake_redis: _FakeRedis) -> None:
    assert tc.is_task_cancelled("") is False
    assert tc.is_task_cancelled(None) is False
    with pytest.raises(ValueError):
        tc.request_task_cancel("")


def test_request_task_cancel_wakes_registered_waiters(fake_redis: _FakeRedis) -> None:
    tc.register_live_task(
        "task-abc",
        tenant_id="t1",
        principal_id="user-1",
        command_type="core.agent_turn",
        root_command_id="root-1",
    )
    tc.register_command_waiter("task-abc", "celery-1", tenant_id="t1")
    tc.register_command_waiter("task-abc", "celery-2", tenant_id="t1")
    payload = tc.request_task_cancel(
        "task-abc",
        reason="stop",
        principal_id="user-1",
        source="test",
        tenant_id="t1",
    )
    assert payload["action"] == "cancel"
    assert tc.is_task_cancelled("task-abc", tenant_id="t1") is True
    assert fake_redis.lists.get(tc.cancel_wake_key("celery-1"))
    assert fake_redis.lists.get(tc.cancel_wake_key("celery-2"))
    assert fake_redis.streams.get("t1:task:task-abc:response")


def test_subscribe_after_publish_still_sees_sticky(fake_redis: _FakeRedis) -> None:
    tc.request_task_cancel("task-race", reason="early", source="test")
    # Waiter starts after cancel — sticky EXISTS must still win.
    waited = tc.wait_for_command_outcome(
        "task-race", "celery-race", timeout_seconds=0.05
    )
    assert waited.outcome == "cancelled"
    assert waited.cancelled_scope == "task-race"


def test_wait_for_command_outcome_completed(fake_redis: _FakeRedis) -> None:
    tc.signal_command_result("celery-done")
    waited = tc.wait_for_command_outcome(
        "task-1",
        "celery-done",
        timeout_seconds=1.0,
    )
    assert waited.outcome == "completed"


def test_wait_for_command_outcome_cancelled(fake_redis: _FakeRedis) -> None:
    # Pre-register so cancel LPUSHes this waiter's key, then wait.
    tc.register_command_waiter("task-cancel", "celery-c")
    tc.request_task_cancel("task-cancel", reason="stop", source="test")
    waited = tc.wait_for_command_outcome(
        "task-cancel",
        "celery-c",
        timeout_seconds=1.0,
    )
    assert waited.outcome == "cancelled"
    assert waited.cancelled_scope == "task-cancel"


def test_live_index_register_list_unregister(fake_redis: _FakeRedis) -> None:
    tc.register_live_task(
        "task-live",
        tenant_id="t1",
        principal_id="u1",
        conversation_id="c1",
        command_type="core.agent_turn",
    )
    listed = tc.list_live_tasks(tenant_id="t1", principal_id="u1")
    assert len(listed) == 1
    assert listed[0]["task_id"] == "task-live"
    assert tc.live_task_owned_by(listed[0], principal_id="u1") is True
    assert tc.live_task_owned_by(listed[0], principal_id="other") is False
    assert tc.live_task_owned_by(listed[0], principal_id="") is False

    tc.unregister_live_task("task-live", tenant_id="t1", principal_id="u1")
    assert tc.list_live_tasks(tenant_id="t1", principal_id="u1") == []


def test_live_index_hides_cancelled_by_default(fake_redis: _FakeRedis) -> None:
    tc.register_live_task(
        "task-cxl",
        tenant_id="t1",
        principal_id="u1",
        command_type="core.agent_turn",
    )
    tc.request_task_cancel("task-cxl", reason="stop", source="test", tenant_id="t1")
    assert tc.list_live_tasks(tenant_id="t1", principal_id="u1") == []
    listed = tc.list_live_tasks(
        tenant_id="t1", principal_id="u1", include_cancelled=True
    )
    assert len(listed) == 1
    assert listed[0]["status"] == "cancelled"


def test_build_task_cancelled_response() -> None:
    resp = tc.build_task_cancelled_response(
        command_id="cmd-1",
        command_type="core.agentic_loop",
        task_id="task-1",
        reason="stopped",
    )
    assert resp["status"] == "error"
    assert resp["error"]["details"]["code"] == "task_cancelled"


def test_dispatch_gate_refuses_cancelled_task(fake_redis: _FakeRedis) -> None:
    """process_distributed_command gate returns a cancelled Celery payload."""
    from motet.core.workers.command_tasks import dispatch_cancel_gate_result

    tc.request_task_cancel("task-gate", reason="operator", source="test")
    cmd = MagicMock()
    cmd.command_id = "c1"
    cmd.get_command_type.return_value = "core.agent_loop"
    cmd.distributed_context = MagicMock(
        task_id="task-gate",
        parent_command_id="root-1",
        cancel_scopes=["task-gate", "root-1"],
    )
    payload = dispatch_cancel_gate_result(
        cmd,
        start_time=0.0,
        worker_id="w1",
        celery_task_id="celery-1",
        cancel_scopes=["task-gate", "root-1"],
        motet_task_id="task-gate",
    )
    assert payload is not None
    assert payload["cancelled"] is True
    assert payload["result"]["error"]["details"]["code"] == "task_cancelled"
    assert payload["result"]["error"]["details"]["task_id"] == "task-gate"


def test_dispatch_gate_refuses_cancelled_workflow(fake_redis: _FakeRedis) -> None:
    from motet.core.workers.command_tasks import dispatch_cancel_gate_result
    from motet.core.workflow.checkpoint import WORKFLOW_CANCELLED_CODE

    tc.request_scope_cancel("wfrun-gate", reason="ops", source="test")
    cmd = MagicMock()
    cmd.command_id = "c-wf"
    cmd.get_command_type.return_value = "core.workflow_execution"
    payload = dispatch_cancel_gate_result(
        cmd,
        start_time=0.0,
        worker_id="w1",
        celery_task_id="celery-wf",
        cancel_scopes=["task-other", "wfrun-gate"],
        motet_task_id="task-other",
    )
    assert payload is not None
    assert payload["result"]["error"]["details"]["code"] == WORKFLOW_CANCELLED_CODE


def test_dispatch_gate_allows_active_command(fake_redis: _FakeRedis) -> None:
    from motet.core.workers.command_tasks import dispatch_cancel_gate_result

    cmd = MagicMock()
    cmd.command_id = "c-ok"
    cmd.get_command_type.return_value = "core.agent_loop"
    payload = dispatch_cancel_gate_result(
        cmd,
        start_time=0.0,
        worker_id="w1",
        celery_task_id="celery-ok",
        cancel_scopes=["task-ok"],
        motet_task_id="task-ok",
    )
    assert payload is None


def test_communicator_pre_send_refuses_cancelled(fake_redis: _FakeRedis) -> None:
    from motet.core.workers.routing.worker_communicator import WorkerCommunicator

    tc.request_task_cancel("task-pre", reason="stop", source="test")

    cmd = MagicMock()
    cmd.command_id = "cmd-1"
    cmd.get_command_type.return_value = "core.tool_execution"
    cmd.distributed_context = MagicMock(
        task_id="task-pre",
        timeout_seconds=30,
        metadata={},
        tenant_id="t1",
        motet_id="default",
        cancel_scopes=["task-pre"],
        own_cancel_scope=None,
        parent_command_id=None,
    )
    cmd.serialize_for_transport.return_value = "{}"

    comm = WorkerCommunicator()
    with patch.object(comm, "_cancelled_result", wraps=comm._cancelled_result) as wrapped:
        result = comm.send_command({"worker_id": "w1"}, cmd)
    assert result["error_code"] == "task_cancelled"
    assert "pre-send" in result["error"]
    wrapped.assert_called()


def test_communicator_timeout_cancels_waiter_own_scope(
    fake_redis: _FakeRedis,
) -> None:
    from motet.core.workers.routing.worker_communicator import WorkerCommunicator

    cmd = MagicMock()
    cmd.command_id = "child-1"
    cmd.get_command_type.return_value = "core.tool_execution"
    cmd.distributed_context = MagicMock(
        task_id="task-to",
        timeout_seconds=1,
        metadata={},
        tenant_id="t1",
        motet_id="default",
        parent_command_id="parent-1",
        cancel_scopes=["task-to", "parent-1"],
        own_cancel_scope=None,
    )
    cmd.serialize_for_transport.return_value = "{}"

    celery_result = MagicMock()
    celery_result.id = "celery-to"
    celery_app = MagicMock()
    celery_app.send_task.return_value = celery_result

    comm = WorkerCommunicator()
    with patch(
        "motet.core.workers.celery_app.get_celery_app", return_value=celery_app
    ), patch(
        "motet.core.distributed.task_control.wait_for_command_outcome",
        return_value="timeout",
    ), patch.object(
        comm, "_cancel_waiter_own_scope"
    ) as cancel_own:
        result = comm.send_command({"worker_id": "w1"}, cmd)
        assert result.get("status") == "error"
        cancel_own.assert_called_once()
        assert cancel_own.call_args.kwargs.get("source") == "communicator_timeout"


def test_wait_for_command_outcome_scope_cancelled(fake_redis: _FakeRedis) -> None:
    tc.request_scope_cancel("wfrun-1", reason="ops", source="test")
    waited = tc.wait_for_command_outcome(
        "task-other",
        "celery-wf",
        timeout_seconds=1.0,
        cancel_scopes=["task-other", "wfrun-1"],
    )
    assert waited.outcome == "cancelled"
    assert waited.cancelled_scope == "wfrun-1"


def test_request_task_cancel_bridges_workflow_runs(fake_redis: _FakeRedis) -> None:
    calls: List[Dict[str, Any]] = []

    def _fake_bridge(**kwargs: Any) -> Dict[str, Any]:
        calls.append(kwargs)
        return {"status": "cancel_requested", "applied": False}

    with patch(
        "motet.core.workflow.checkpoint.list_workflow_runs_for_task",
        return_value=[
            {
                "tenant_id": "t1",
                "motet_id": "default",
                "workflow_run_id": "wfrun-bridge",
            }
        ],
    ), patch(
        "motet.core.workflow.checkpoint.request_workflow_run_control",
        side_effect=_fake_bridge,
    ):
        tc.request_task_cancel("task-bridge", reason="stop", source="test")

    assert len(calls) == 1
    assert calls[0]["workflow_run_id"] == "wfrun-bridge"
    assert calls[0]["action"] == "cancel"


def test_probe_unknown_on_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        def exists(self, *keys: str) -> int:
            raise ConnectionError("Too many connections")

    monkeypatch.setattr(
        "motet.core.distributed.redis_manager.get_sync_redis_client",
        lambda _service="default": _Boom(),
    )
    assert tc.probe_task_cancelled("task-x") == "unknown"
    assert tc.is_task_cancelled("task-x") is True
    assert tc.probe_command_result_done("w1") == "unknown"
    assert tc.is_command_result_done("w1") is False
    assert tc.probe_task_cancelled("") == "active"
    assert tc.is_task_cancelled("") is False
    assert tc.probe_scopes_cancelled(["a", "b"]) == "unknown"
    assert tc.is_cancelled(["a", "b"]) is True


def test_wait_unknown_does_not_look_like_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: List[float] = []

    class _Boom(_FakeRedis):
        def exists(self, *keys: str) -> int:
            raise ConnectionError("Too many connections")

        def blpop(
            self,
            keys: Union[str, Sequence[str]],
            timeout: int = 0,
        ) -> Optional[tuple]:
            raise ConnectionError("Too many connections")

    boom = _Boom()
    monkeypatch.setattr(
        "motet.core.distributed.redis_manager.get_sync_redis_client",
        lambda _service="default": boom,
    )
    monkeypatch.setattr(tc, "_cooperative_sleep", lambda s: sleeps.append(s))

    waited = tc.wait_for_command_outcome(
        "task-x", "celery-x", timeout_seconds=0.15
    )
    assert waited.outcome == "timeout"
    assert sleeps
    assert sleeps[0] == tc.WAIT_BLPOP_ERROR_BACKOFF_INITIAL_SECONDS
    if len(sleeps) > 1:
        assert sleeps[1] > sleeps[0]


def test_blpop_timeout_never_zero_and_caps_at_chunk() -> None:
    assert tc._blpop_timeout_seconds(0.4, next_ready_in=None, chunk=15) == 1
    assert tc._blpop_timeout_seconds(60.0, next_ready_in=None, chunk=15) == 15
    assert tc._blpop_timeout_seconds(60.0, next_ready_in=8.0, chunk=15) == 8


def test_signal_command_result_wake_hook_uses_shared_task_name() -> None:
    from motet.core.constants import CELERY_PROCESS_COMMAND_TASK
    from motet.core.workers.command_tasks import _signal_command_result_wake

    sender = MagicMock()
    sender.name = CELERY_PROCESS_COMMAND_TASK
    args = [{"envelope": {"command_id": "cmd-1", "command_type": "core.gather"}}]
    with patch(
        "motet.core.workers.command_tasks._persist_missing_wait_outcome"
    ) as persist:
        _signal_command_result_wake(sender=sender, task_id="abc", args=args)
        persist.assert_called_once()
        assert persist.call_args.kwargs["waiter_id"] == "abc"
        assert persist.call_args.kwargs["identity"]["command_id"] == "cmd-1"

    sender.name = "imf.other.task"
    with patch(
        "motet.core.workers.command_tasks._persist_missing_wait_outcome"
    ) as persist:
        _signal_command_result_wake(sender=sender, task_id="abc", args=args)
        persist.assert_not_called()

    sender.name = CELERY_PROCESS_COMMAND_TASK
    with patch(
        "motet.core.workers.command_tasks._persist_missing_wait_outcome",
        side_effect=RuntimeError("redis down"),
    ):
        _signal_command_result_wake(sender=sender, task_id="abc", args=args)


def test_signal_command_result_wake_hook_skips_without_identity() -> None:
    from motet.core.constants import CELERY_PROCESS_COMMAND_TASK
    from motet.core.workers.command_tasks import _signal_command_result_wake

    sender = MagicMock()
    sender.name = CELERY_PROCESS_COMMAND_TASK
    with patch(
        "motet.core.distributed.task_control.signal_command_result"
    ) as sig:
        _signal_command_result_wake(sender=sender, task_id="abc")
        sig.assert_not_called()


def test_persist_missing_wait_outcome_signals_when_envelope_exists() -> None:
    from motet.core.workers.command_tasks import _persist_missing_wait_outcome

    manager = MagicMock()
    manager.has_command_wait_outcome.return_value = True
    with patch(
        "motet.core.distributed.redis_command_data_manager.get_redis_command_data_manager",
        return_value=manager,
    ), patch(
        "motet.core.distributed.task_control.signal_command_result"
    ) as sig:
        _persist_missing_wait_outcome(
            identity={"command_id": "g1", "tenant_id": "t1"},
            waiter_id="celery-1",
        )
    manager.store_command_wait_outcome.assert_not_called()
    sig.assert_called_once_with("celery-1")


def test_persist_missing_wait_outcome_writes_error_when_missing() -> None:
    from motet.core.workers.command_tasks import (
        WAIT_OUTCOME_MISSING_ERROR,
        _persist_missing_wait_outcome,
    )

    manager = MagicMock()
    manager.has_command_wait_outcome.return_value = False
    with patch(
        "motet.core.distributed.redis_command_data_manager.get_redis_command_data_manager",
        return_value=manager,
    ), patch(
        "motet.core.distributed.task_control.signal_command_result"
    ) as sig:
        _persist_missing_wait_outcome(
            identity={
                "command_id": "g1",
                "command_type": "core.gather",
                "tenant_id": "t1",
                "motet_id": "default",
            },
            waiter_id="celery-1",
            state="FAILURE",
        )
    stored = manager.store_command_wait_outcome.call_args.kwargs
    assert stored["command_id"] == "g1"
    assert stored["envelope"]["status"] == "error"
    assert WAIT_OUTCOME_MISSING_ERROR in stored["envelope"]["error"]
    sig.assert_called_once_with("celery-1")


def test_communicator_send_task_uses_shared_task_name(
    fake_redis: _FakeRedis,
) -> None:
    from motet.core.constants import CELERY_PROCESS_COMMAND_TASK
    from motet.core.workers.routing.worker_communicator import WorkerCommunicator

    cmd = MagicMock()
    cmd.command_id = "cmd-1"
    cmd.get_command_type.return_value = "core.tool_execution"
    cmd.distributed_context = MagicMock(
        task_id="task-name",
        timeout_seconds=30,
        metadata={},
        tenant_id="t1",
        motet_id="default",
        cancel_scopes=["task-name"],
        own_cancel_scope=None,
        parent_command_id=None,
    )
    cmd.serialize_for_transport.return_value = "{}"

    celery_result = MagicMock()
    celery_result.id = "celery-name"
    celery_app = MagicMock()
    celery_app.send_task.return_value = celery_result
    envelope = {"status": "completed", "result": {"data": {}}, "command_id": "cmd-1"}
    manager = MagicMock()
    manager.retrieve_command_wait_outcome.return_value = envelope

    comm = WorkerCommunicator()
    with patch(
        "motet.core.workers.celery_app.get_celery_app", return_value=celery_app
    ), patch(
        "motet.core.distributed.task_control.wait_for_command_outcome",
        return_value="completed",
    ), patch(
        "motet.core.distributed.redis_command_data_manager.get_redis_command_data_manager",
        return_value=manager,
    ), patch(
        "motet.core.commands.distributed.DistributedCommand.rehydrate_command_result",
        side_effect=lambda r: r,
    ):
        comm.send_command({"worker_id": "w1"}, cmd)

    assert celery_app.send_task.call_args[0][0] == CELERY_PROCESS_COMMAND_TASK
    assert celery_app.send_task.call_args.kwargs.get("ignore_result") is True
    manager.retrieve_command_wait_outcome.assert_called_once()
    celery_result.ready.assert_not_called()
    celery_result.successful.assert_not_called()


def test_apply_command_cancel_scopes_root_pushes_command_id() -> None:
    from types import SimpleNamespace

    ctx = SimpleNamespace(
        task_id="task-1",
        parent_command_id=None,
        cancel_scopes=[],
        own_cancel_scope=None,
    )
    cmd = SimpleNamespace(command_id="root-1", distributed_context=ctx)
    tc.apply_command_cancel_scopes(cmd)
    assert ctx.cancel_scopes == ["task-1", "root-1"]
    assert ctx.own_cancel_scope == "root-1"


def test_apply_command_cancel_scopes_child_inherits_only() -> None:
    from types import SimpleNamespace

    ctx = SimpleNamespace(
        task_id="task-1",
        parent_command_id="root-1",
        cancel_scopes=["task-1", "root-1"],
        own_cancel_scope=None,
    )
    cmd = SimpleNamespace(command_id="child-1", distributed_context=ctx)
    tc.apply_command_cancel_scopes(cmd)
    assert ctx.cancel_scopes == ["task-1", "root-1"]
    assert ctx.own_cancel_scope is None


def test_cancel_own_scope_noop_without_own_scope(fake_redis: _FakeRedis) -> None:
    from types import SimpleNamespace

    ctx = SimpleNamespace(
        own_cancel_scope=None,
        task_id="task-1",
        principal_id="u1",
    )
    cmd = SimpleNamespace(command_id="child-1", distributed_context=ctx)
    with patch.object(tc, "request_scope_cancel") as cancel:
        assert tc.cancel_own_scope_for_command(cmd, reason="x", source="test") is False
        cancel.assert_not_called()


def test_cancel_own_scope_root_also_writes_task_id(fake_redis: _FakeRedis) -> None:
    from types import SimpleNamespace

    ctx = SimpleNamespace(
        own_cancel_scope="root-1",
        task_id="task-1",
        principal_id="u1",
    )
    cmd = SimpleNamespace(command_id="root-1", distributed_context=ctx)
    with patch.object(tc, "request_scope_cancel") as cancel:
        assert tc.cancel_own_scope_for_command(cmd, reason="x", source="test") is True
        assert cancel.call_count == 2
        assert cancel.call_args_list[0].args[0] == "root-1"
        assert cancel.call_args_list[1].args[0] == "task-1"


def test_cancel_own_scope_workflow_does_not_write_task_id(
    fake_redis: _FakeRedis,
) -> None:
    from types import SimpleNamespace

    ctx = SimpleNamespace(
        own_cancel_scope="wfrun-1",
        task_id="task-1",
        principal_id="u1",
    )
    cmd = SimpleNamespace(command_id="wf-cmd-1", distributed_context=ctx)
    with patch.object(tc, "request_scope_cancel") as cancel:
        assert tc.cancel_own_scope_for_command(cmd, reason="x", source="test") is True
        assert cancel.call_count == 1
        assert cancel.call_args.args[0] == "wfrun-1"


def test_is_cancelled_variadic_exists(fake_redis: _FakeRedis) -> None:
    tc.request_scope_cancel("scope-b", reason="stop", source="test")
    assert tc.is_cancelled(["scope-a", "scope-b"]) is True
    assert tc.probe_scopes_cancelled(["scope-a", "scope-b"]) == "cancelled"
    assert tc.is_cancelled(["scope-a", "scope-c"]) is False
    assert tc.probe_scopes_cancelled(["scope-a", "scope-c"]) == "active"
    assert tc.first_cancelled_scope(["scope-a", "scope-b"]) == "scope-b"
    assert tc.is_cancelled([]) is False
    assert tc.probe_scopes_cancelled([]) == "active"


def test_unregister_waiter_deletes_result_done_key(fake_redis: _FakeRedis) -> None:
    tc.signal_command_result("celery-leak")
    assert tc.result_done_key("celery-leak") in fake_redis.kv
    tc.unregister_command_waiter("task-1", "celery-leak")
    assert tc.result_done_key("celery-leak") not in fake_redis.kv


def test_live_unowned_row_visible_to_same_tenant(fake_redis: _FakeRedis) -> None:
    tc.register_live_task(
        "task-unowned",
        tenant_id="t1",
        principal_id="",
        command_type="core.agent_turn",
    )
    listed = tc.list_live_tasks(tenant_id="t1", principal_id="u1")
    assert any(m.get("task_id") == "task-unowned" for m in listed)
    meta = tc.get_live_task("task-unowned", tenant_id="t1")
    assert meta is not None
    assert tc.live_task_owned_by(meta, principal_id="u1", tenant_id="t1") is True
    assert tc.live_task_owned_by(meta, principal_id="u1", tenant_id="other") is False
    assert tc.live_task_owned_by(meta, principal_id="") is False


def test_unregister_root_live_task_on_error_keeps_cancelled(
    fake_redis: _FakeRedis,
) -> None:
    from motet.core.workers.command_tasks import (
        _unregister_root_live_task_unless_cancelled,
    )

    tc.register_live_task(
        "task-err",
        tenant_id="t1",
        principal_id="u1",
        command_type="core.agent_turn",
    )
    cmd = MagicMock()
    cmd.distributed_context = MagicMock(
        task_id="task-err",
        parent_command_id="",
        tenant_id="t1",
        principal_id="u1",
    )
    _unregister_root_live_task_unless_cancelled(cmd)
    assert tc.get_live_task("task-err", tenant_id="t1") is None

    tc.register_live_task(
        "task-cxl-keep",
        tenant_id="t1",
        principal_id="u1",
        command_type="core.agent_turn",
    )
    tc.request_task_cancel(
        "task-cxl-keep", reason="stop", source="test", tenant_id="t1"
    )
    cmd.distributed_context.task_id = "task-cxl-keep"
    _unregister_root_live_task_unless_cancelled(cmd)
    kept = tc.get_live_task("task-cxl-keep", tenant_id="t1")
    assert kept is not None
    assert kept.get("status") == "cancelled"


def test_bind_task_key_tenant_prefixes_control_keys(fake_redis: _FakeRedis) -> None:
    token = tc.bind_task_key_tenant("acme")
    try:
        assert tc.control_key("scope-1") == "acme:task:control:scope-1"
        tc.request_scope_cancel("scope-1", reason="stop", source="test")
        assert tc.is_cancelled(["scope-1"]) is True
        assert fake_redis.kv.get("acme:task:control:scope-1")
    finally:
        tc.reset_task_key_tenant(token)
    assert tc.control_key("scope-1") == "task:control:scope-1"


def test_persist_command_wait_outcome_stores_then_signals() -> None:
    from motet.core.workers.command_tasks import persist_command_wait_outcome

    order: List[str] = []
    manager = MagicMock()

    def _store(**_kwargs: Any) -> str:
        order.append("store")
        return "acme:cmd:outcome:cmd-1"

    manager.store_command_wait_outcome.side_effect = _store
    cmd = MagicMock()
    cmd.command_id = "cmd-1"
    cmd.distributed_context = MagicMock(
        tenant_id="acme", motet_id="default", timeout_seconds=30
    )
    envelope = {
        "status": "completed",
        "command_id": "cmd-1",
        "command_type": "core.tool_execution",
        "result": {"ok": True},
    }
    with patch(
        "motet.core.distributed.redis_command_data_manager.get_redis_command_data_manager",
        return_value=manager,
    ), patch(
        "motet.core.distributed.task_control.signal_command_result",
        side_effect=lambda _wid: order.append("signal"),
    ):
        persist_command_wait_outcome(cmd, envelope, waiter_id="celery-1")

    assert order == ["store", "signal"]
    manager.store_command_wait_outcome.assert_called_once()
    assert manager.store_command_wait_outcome.call_args.kwargs["command_id"] == "cmd-1"


def test_persist_command_wait_outcome_does_not_signal_without_command_id() -> None:
    from motet.core.workers.command_tasks import persist_command_wait_outcome

    manager = MagicMock()
    with patch(
        "motet.core.distributed.redis_command_data_manager.get_redis_command_data_manager",
        return_value=manager,
    ), patch(
        "motet.core.distributed.task_control.signal_command_result"
    ) as sig:
        persist_command_wait_outcome(
            None, {"status": "error", "command_id": "unknown"}, waiter_id="celery-1"
        )
    manager.store_command_wait_outcome.assert_not_called()
    sig.assert_not_called()


def test_process_distributed_command_ignores_celery_result() -> None:
    from motet.core.workers.command_tasks import process_distributed_command

    assert process_distributed_command.ignore_result is True


def test_communicator_raises_on_motet_error_envelope(
    fake_redis: _FakeRedis,
) -> None:
    from motet.core.workers.routing.worker_communicator import WorkerCommunicator

    cmd = MagicMock()
    cmd.command_id = "cmd-err"
    cmd.get_command_type.return_value = "core.tool_execution"
    cmd.distributed_context = MagicMock(
        task_id="task-err",
        timeout_seconds=30,
        metadata={},
        tenant_id="t1",
        motet_id="default",
        cancel_scopes=["task-err"],
        own_cancel_scope=None,
        parent_command_id=None,
    )
    cmd.serialize_for_transport.return_value = "{}"

    celery_result = MagicMock()
    celery_result.id = "celery-err"
    celery_app = MagicMock()
    celery_app.send_task.return_value = celery_result
    manager = MagicMock()
    manager.retrieve_command_wait_outcome.return_value = {
        "status": "error",
        "error": "boom",
        "command_id": "cmd-err",
    }

    comm = WorkerCommunicator()
    with patch(
        "motet.core.workers.celery_app.get_celery_app", return_value=celery_app
    ), patch(
        "motet.core.distributed.task_control.wait_for_command_outcome",
        return_value="completed",
    ), patch(
        "motet.core.distributed.redis_command_data_manager.get_redis_command_data_manager",
        return_value=manager,
    ):
        result = comm.send_command({"worker_id": "w1"}, cmd)

    assert result.get("status") == "error"
    assert "boom" in str(result.get("error") or result)
    celery_result.ready.assert_not_called()
    celery_result.result = None
    celery_result.successful.assert_not_called()


def test_wait_for_command_outcomes_uses_sticky_then_unary(
    fake_redis: _FakeRedis,
) -> None:
    tc.signal_command_result("celery-a")
    waited = tc.wait_for_command_outcomes(
        "task-1",
        ["celery-a", "celery-b"],
        timeout_seconds=0.05,
    )
    assert waited["celery-a"].outcome == "completed"
    assert waited["celery-b"].outcome == "timeout"


def test_wait_for_command_outcomes_cancel_aborts_remaining(
    fake_redis: _FakeRedis,
) -> None:
    tc.request_task_cancel("task-many", reason="stop", source="test")
    waited = tc.wait_for_command_outcomes(
        "task-many",
        ["celery-x", "celery-y"],
        timeout_seconds=1.0,
        cancel_scopes=["task-many"],
    )
    assert waited["celery-x"].outcome == "cancelled"
    assert waited["celery-y"].outcome == "cancelled"
    assert waited["celery-x"].cancelled_scope == "task-many"


def test_wait_for_command_outcomes_empty() -> None:
    assert tc.wait_for_command_outcomes("task-1", [], timeout_seconds=1.0) == {}


def test_wait_for_command_outcomes_leftovers_wait_in_parallel(
    fake_redis: _FakeRedis,
) -> None:
    """Two unfinished children BLPOP at the same time, not one after the other."""
    import time

    started: list[float] = []

    def _slow_wait(*_args: Any, **_kwargs: Any) -> tc.CommandWaitResult:
        started.append(time.time())
        time.sleep(0.15)
        return tc.CommandWaitResult("timeout")

    t0 = time.time()
    with patch(
        "motet.core.distributed.task_control.wait_for_command_outcome",
        side_effect=_slow_wait,
    ):
        waited = tc.wait_for_command_outcomes(
            "task-1",
            ["celery-slow-a", "celery-slow-b"],
            timeout_seconds=2.0,
        )
    elapsed = time.time() - t0
    assert waited["celery-slow-a"].outcome == "timeout"
    assert waited["celery-slow-b"].outcome == "timeout"
    assert len(started) == 2
    assert elapsed < 0.26
