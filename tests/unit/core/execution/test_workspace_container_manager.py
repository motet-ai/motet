"""
Motet - WorkspaceContainerManager unit tests (ADR-0106 Slice A)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-25

These tests pin the lifecycle contract of the per-workspace container primitive:

    * mode != "cold" returns a structured error (Slice A scope)
    * Lazy create on first dispatch; reuse on second dispatch
    * Dead container in registry is replaced (ADR-0106 §rule 6)
    * release() removes the container and unbinds
    * reap_idle() removes both idle-by-time and dead-by-substrate bindings
    * Tenant cap eviction kills oldest-idle by last_active_at
    * Idle reaper skips bindings with active exec markers
    * Session dispatch can materialize manager-owned input files before exec

The tests mock the Docker engine layer (docker_client.*) so they do not
require a Docker socket, an image, or a daemon. The registry is a real
WorkspaceContainerRegistry backed by an in-memory Redis stub.
"""

from __future__ import annotations

import http.client
import json
from typing import Any, Dict, Iterator, List, Optional, Set
from unittest.mock import patch

import pytest

from motet.core.distributed.workspace_container_registry import (
    WorkspaceContainerBinding,
    WorkspaceContainerRegistry,
)
from motet.core.execution.models import ExecutionInputFile, ExecutionRequest
from motet.core.execution.workspace_container_manager import (
    WorkspaceContainerManager,
    is_workspace_container_enabled,
)


# ---------------------------------------------------------------------------
# In-memory Redis stub (same shape as the registry test, kept local on purpose)
# ---------------------------------------------------------------------------


class _FakePipeline:
    def __init__(self, redis: "_FakeRedis") -> None:
        self._redis = redis
        self._ops: List[tuple] = []

    def delete(self, key: str) -> "_FakePipeline":
        self._ops.append(("delete", key))
        return self

    def hset(
        self,
        key: str,
        field: Any = None,
        value: Any = None,
        *,
        mapping: Optional[Dict[str, Any]] = None,
    ) -> "_FakePipeline":
        if mapping is not None:
            self._ops.append(("hset", key, dict(mapping)))
        elif field is not None:
            self._ops.append(("hset_field", key, field, value))
        return self

    def hincrby(self, key: str, field: str, amount: int) -> "_FakePipeline":
        self._ops.append(("hincrby", key, field, int(amount)))
        return self

    def expire(self, key: str, seconds: int) -> "_FakePipeline":
        self._ops.append(("expire", key, int(seconds)))
        return self

    def sadd(self, key: str, *members: str) -> "_FakePipeline":
        self._ops.append(("sadd", key, list(members)))
        return self

    def srem(self, key: str, *members: str) -> "_FakePipeline":
        self._ops.append(("srem", key, list(members)))
        return self

    def execute(self) -> List[Any]:
        results: List[Any] = []
        for op in self._ops:
            kind = op[0]
            if kind == "delete":
                results.append(self._redis.delete(op[1]))
            elif kind == "hset":
                results.append(self._redis.hset(op[1], mapping=op[2]))
            elif kind == "hset_field":
                results.append(self._redis.hset(op[1], op[2], op[3]))
            elif kind == "hincrby":
                results.append(self._redis.hincrby(op[1], op[2], op[3]))
            elif kind == "expire":
                results.append(self._redis.expire(op[1], op[2]))
            elif kind == "sadd":
                results.append(self._redis.sadd(op[1], *op[2]))
            elif kind == "srem":
                results.append(self._redis.srem(op[1], *op[2]))
        self._ops.clear()
        return results


class _FakeRedis:
    def __init__(self) -> None:
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.sets: Dict[str, Set[str]] = {}
        self.ttls: Dict[str, int] = {}

    def hset(self, key, field=None, value=None, *, mapping=None):
        bucket = self.hashes.setdefault(key, {})
        if mapping is not None:
            for k, v in mapping.items():
                bucket[k] = v
            return len(mapping)
        if field is not None:
            bucket[field] = value if value is not None else ""
            return 1
        return 0

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def exists(self, key):
        return 1 if key in self.hashes or key in self.sets else 0

    def delete(self, key):
        deleted = 0
        if key in self.hashes:
            del self.hashes[key]; deleted += 1
        if key in self.sets:
            del self.sets[key]; deleted += 1
        self.ttls.pop(key, None)
        return deleted

    def expire(self, key, seconds):
        if key in self.hashes or key in self.sets:
            self.ttls[key] = int(seconds)
            return 1
        return 0

    def hincrby(self, key, field, amount):
        bucket = self.hashes.setdefault(key, {})
        current = int(bucket.get(field, "0") or "0")
        current += int(amount)
        bucket[field] = str(current)
        return current

    def sadd(self, key, *members):
        bucket = self.sets.setdefault(key, set())
        before = len(bucket)
        bucket.update(members)
        return len(bucket) - before

    def srem(self, key, *members):
        bucket = self.sets.get(key)
        if bucket is None:
            return 0
        removed = 0
        for m in members:
            if m in bucket:
                bucket.discard(m); removed += 1
        if not bucket:
            del self.sets[key]
        return removed

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def keys(self, pattern):
        prefix = pattern.rstrip("*")
        return [k for k in list(self.hashes.keys()) + list(self.sets.keys()) if k.startswith(prefix)]

    def scan_iter(self, match):
        import fnmatch

        for k in list(self.hashes.keys()) + list(self.sets.keys()):
            if fnmatch.fnmatch(k, match):
                yield k

    def pipeline(self):
        return _FakePipeline(self)


# ---------------------------------------------------------------------------
# Stub distributed lock (registers as if always acquired)
# ---------------------------------------------------------------------------


class _FakeLock:
    """Always-acquired lock; release_sync is a no-op.

    The manager treats the return value of acquire_distributed_lock_sync as
    "lock object on success, None on contention". For Slice A unit tests we
    simulate the success path; the contention path is exercised via the
    'lock_unavailable' test below by patching to return None.
    """

    def release_sync(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_redis() -> _FakeRedis:
    return _FakeRedis()


@pytest.fixture
def registry(
    fake_redis: _FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> WorkspaceContainerRegistry:
    monkeypatch.setenv("MOTET_WORKSPACE_CONTAINER_IDLE_TTL_SECONDS", "60")
    with patch(
        "motet.core.distributed.workspace_container_registry.get_sync_redis_client",
        return_value=fake_redis,
    ):
        return WorkspaceContainerRegistry()


@pytest.fixture
def docker_mock() -> Iterator[Any]:
    """Replace the docker_client module symbols the manager depends on.

    Default behavior: socket exists, runtime is empty, container creation
    succeeds with id "abc1234567890def", container start returns 204,
    exec_create returns CREATED with an Id, exec_start returns OK with an
    empty mux frame, exec_inspect reports ExitCode 0.
    """
    with patch("motet.core.execution.workspace_container_manager.docker_client") as mod:
        mod.docker_socket_path.return_value = ("/var/run/docker.sock", None)
        mod.api_prefix.return_value = "/v1.41"
        mod.docker_engine_container_runtime.return_value = ""
        mod.auto_pull_enabled.return_value = False
        mod.create_failed_missing_image.return_value = False
        mod.daemon_error.side_effect = lambda st, body: f"docker error {st}: {body!r}"
        mod.docker_pull_image.return_value = (True, None)
        mod.docker_container_running.return_value = True
        mod.docker_remove_container.return_value = None
        mod.build_tar_archive.side_effect = lambda entries: b"<tar>"
        mod.docker_put_archive.return_value = (http.client.OK, b"")

        # default create-container response
        mod.docker_request.side_effect = _make_docker_request(default_cid="abc1234567890def")

        # exec_create returns CREATED + Id
        def _exec_create(*args, **kwargs):
            return (http.client.CREATED, json.dumps({"Id": "exec-id-1"}).encode("utf-8"))

        mod.docker_exec_create.side_effect = _exec_create

        # exec_start returns OK with no payload (demux returns empty)
        mod.docker_exec_start.return_value = (http.client.OK, b"")
        mod.demux_docker_stream.return_value = (b"", b"")
        mod.docker_exec_inspect.return_value = (
            http.client.OK,
            json.dumps({"ExitCode": 0}).encode("utf-8"),
        )

        yield mod


def _make_docker_request(default_cid: str = "abc1234567890def"):
    """Construct a side_effect that handles container/create + start.

    Returns CREATED with {"Id": default_cid} for create-container; NO_CONTENT
    for /start; and a benign OK for everything else.
    """

    def _side_effect(sock, method, path, body=None):
        if "/containers/create" in path:
            return (http.client.CREATED, json.dumps({"Id": default_cid}).encode("utf-8"))
        if "/start" in path:
            return (http.client.NO_CONTENT, b"")
        return (http.client.OK, b"")

    return _side_effect


@pytest.fixture
def fs_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force os.path.exists(socket) to be true (we won't actually open it)."""
    real_exists = __import__("os").path.exists

    def _exists(p: str) -> bool:
        if p == "/var/run/docker.sock":
            return True
        return real_exists(p)

    monkeypatch.setattr("os.path.exists", _exists)


@pytest.fixture
def lock_mock() -> Iterator[Any]:
    with patch(
        "motet.core.execution.workspace_container_manager.acquire_distributed_lock_sync",
        return_value=_FakeLock(),
    ) as m:
        yield m


@pytest.fixture
def manager(
    registry: WorkspaceContainerRegistry, docker_mock: Any, fs_mock: None, lock_mock: Any
) -> WorkspaceContainerManager:
    return WorkspaceContainerManager(registry=registry, worker_id="worker-test-1")


# ---------------------------------------------------------------------------
# is_workspace_container_enabled
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, True),
        ("true", True),
        ("True", True),
        ("1", True),
        ("0", False),
        ("false", False),
        ("False", False),
        ("no", False),
        ("off", False),
    ],
)
def test_is_workspace_container_enabled_env(
    monkeypatch: pytest.MonkeyPatch, value: Optional[str], expected: bool
) -> None:
    if value is None:
        monkeypatch.delenv("MOTET_WORKSPACE_CONTAINER_ENABLED", raising=False)
    else:
        monkeypatch.setenv("MOTET_WORKSPACE_CONTAINER_ENABLED", value)
    assert is_workspace_container_enabled() is expected


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def _request(argv: Optional[List[str]] = None) -> ExecutionRequest:
    return ExecutionRequest(
        argv=argv or ["echo", "hi"],
        cwd="/scratch",
        timeout_seconds=5,
        tenant_id="t1",
        conversation_id_unused=None,  # type: ignore[call-arg]
    ) if False else ExecutionRequest(
        argv=argv or ["echo", "hi"],
        cwd="/scratch",
        timeout_seconds=5,
        tenant_id="t1",
    )


def test_dispatch_rejects_stateful_mode_caller_must_use_dispatch_warm(
    manager: WorkspaceContainerManager,
) -> None:
    """``dispatch`` is argv-shaped; warm callers must use ``dispatch_warm``.

    The wire shapes are different enough (params dict vs argv list) that
    funneling them through one method would obscure the contract. We
    verify the rejection error nudges callers to the right entry point.
    """
    res = manager.dispatch(
        _request(),
        tenant_id="t1",
        conversation_id="c1",
        image_stack="python-minimal",
        mode="warm",  # type: ignore[arg-type]
    )
    assert res.exit_code == -1
    assert res.backend == "workspace-container"
    assert res.error and "dispatch_warm" in res.error


def test_dispatch_lazy_creates_container_and_binds(
    manager: WorkspaceContainerManager,
    docker_mock: Any,
    registry: WorkspaceContainerRegistry,
) -> None:
    # No binding exists yet: docker_container_running is irrelevant for the
    # nonexistent-binding path, but the post-create check returns True.
    docker_mock.docker_container_running.return_value = True

    res = manager.dispatch(
        _request(["python", "-c", "print('hi')"]),
        tenant_id="t1",
        conversation_id="c1",
        image_stack="python-minimal",
        mode="cold",
        oci_image_ref="python:3.11-slim",
    )

    assert res.exit_code == 0
    assert res.backend == "workspace-container"
    assert res.backend_ref == "abc123456789"  # truncated to 12 chars
    assert res.oci_image_ref == "python:3.11-slim"

    # Registry now holds the binding
    binding = registry.lookup(
        tenant_id="t1", conversation_id="c1", image_stack="python-minimal"
    )
    assert binding is not None
    assert binding.container_id == "abc1234567890def"
    assert binding.image == "python:3.11-slim"
    assert binding.mode == "cold"
    assert binding.worker_attribution == "worker-test-1"


def test_dispatch_reuses_existing_running_binding(
    manager: WorkspaceContainerManager,
    docker_mock: Any,
    registry: WorkspaceContainerRegistry,
) -> None:
    # Pre-bind so the manager finds an existing binding.
    registry.bind(
        WorkspaceContainerBinding(
            tenant_id="t1",
            conversation_id="c1",
            image_stack="python-minimal",
            container_id="reusedcid000000000",
            image="python:3.11-slim",
            mode="cold",
        )
    )
    docker_mock.docker_container_running.return_value = True

    # Make sure we never create a new container in this test:
    create_call_count = {"n": 0}
    real_side_effect = docker_mock.docker_request.side_effect

    def _wrapped(sock, method, path, body=None):
        if "/containers/create" in path:
            create_call_count["n"] += 1
        return real_side_effect(sock, method, path, body)

    docker_mock.docker_request.side_effect = _wrapped

    res = manager.dispatch(
        _request(),
        tenant_id="t1",
        conversation_id="c1",
        image_stack="python-minimal",
        mode="cold",
    )

    assert res.exit_code == 0
    assert res.backend_ref == "reusedcid000"
    assert create_call_count["n"] == 0


def test_dispatch_replaces_dead_container_binding(
    manager: WorkspaceContainerManager,
    docker_mock: Any,
    registry: WorkspaceContainerRegistry,
) -> None:
    """ADR-0106 §rule 6: if the container died, the next call observes
    a fresh container with empty /scratch."""
    registry.bind(
        WorkspaceContainerBinding(
            tenant_id="t1",
            conversation_id="c1",
            image_stack="python-minimal",
            container_id="deadcid0000000000",
            image="python:3.11-slim",
            mode="cold",
        )
    )

    # The first running-check (existing container is dead) returns False;
    # subsequent checks (in the locked re-read path, and the post-create
    # path where we don't actually call running-check, only used by reaper)
    # also return False then True. Easier approach: list of return values.
    docker_mock.docker_container_running.side_effect = [False, False, True]

    res = manager.dispatch(
        _request(),
        tenant_id="t1",
        conversation_id="c1",
        image_stack="python-minimal",
        mode="cold",
    )
    assert res.exit_code == 0
    binding = registry.lookup(
        tenant_id="t1", conversation_id="c1", image_stack="python-minimal"
    )
    assert binding is not None
    assert binding.container_id == "abc1234567890def"


def test_dispatch_returns_error_when_lock_unavailable(
    registry: WorkspaceContainerRegistry,
    docker_mock: Any,
    fs_mock: None,
) -> None:
    """When acquire_distributed_lock_sync returns None, dispatch surfaces a
    structured error rather than falling through."""
    with patch(
        "motet.core.execution.workspace_container_manager.acquire_distributed_lock_sync",
        return_value=None,
    ):
        mgr = WorkspaceContainerManager(registry=registry, worker_id="wid")
        res = mgr.dispatch(
            _request(),
            tenant_id="t1",
            conversation_id="c1",
            image_stack="python-minimal",
            mode="cold",
        )
    assert res.exit_code == -1
    assert res.error and "another worker is currently creating" in res.error


def test_dispatch_returns_error_when_socket_path_unresolved(
    registry: WorkspaceContainerRegistry, lock_mock: Any
) -> None:
    """No docker socket — return structured error, never crash."""
    with patch(
        "motet.core.execution.workspace_container_manager.docker_client"
    ) as mod:
        mod.docker_socket_path.return_value = (None, "no socket configured")
        mgr = WorkspaceContainerManager(registry=registry, worker_id="wid")
        res = mgr.dispatch(
            _request(),
            tenant_id="t1",
            conversation_id="c1",
            image_stack="python-minimal",
            mode="cold",
        )
    assert res.exit_code == -1
    assert res.error == "no socket configured"


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


def test_release_unbinds_and_calls_remove(
    manager: WorkspaceContainerManager,
    docker_mock: Any,
    registry: WorkspaceContainerRegistry,
) -> None:
    registry.bind(
        WorkspaceContainerBinding(
            tenant_id="t1",
            conversation_id="c1",
            image_stack="python-minimal",
            container_id="releasecid000000000",
            image="python:3.11-slim",
            mode="cold",
        )
    )

    assert manager.release(
        tenant_id="t1", conversation_id="c1", image_stack="python-minimal"
    ) is True

    docker_mock.docker_remove_container.assert_called_once()
    assert registry.lookup(
        tenant_id="t1", conversation_id="c1", image_stack="python-minimal"
    ) is None


def test_release_noop_when_no_binding(manager: WorkspaceContainerManager) -> None:
    assert manager.release(
        tenant_id="t1", conversation_id="missing", image_stack="python-minimal"
    ) is False


# ---------------------------------------------------------------------------
# reap_idle
# ---------------------------------------------------------------------------


def test_reap_idle_removes_dead_and_idle_bindings(
    manager: WorkspaceContainerManager,
    docker_mock: Any,
    registry: WorkspaceContainerRegistry,
) -> None:
    """A dead container (substrate-gone) is unbound; an idle-but-alive one
    is killed and unbound; a fresh-and-alive one survives."""
    # Fresh: last_active just now
    fresh = WorkspaceContainerBinding(
        tenant_id="t1",
        conversation_id="fresh",
        image_stack="python-minimal",
        container_id="freshcid000000000",
        image="python:3.11-slim",
        mode="cold",
    )
    registry.bind(fresh)

    # Idle: last_active well in the past
    idle = WorkspaceContainerBinding(
        tenant_id="t1",
        conversation_id="idle",
        image_stack="python-minimal",
        container_id="idlecid0000000000",
        image="python:3.11-slim",
        mode="cold",
    )
    registry.bind(idle)
    # Force last_active far enough in the past that reap_idle considers it idle.
    key = "t1:workspace:container:t1:idle:__manual__:__manual__:python-minimal"
    # Reach into the registry's redis for direct mutation.
    registry.redis.hset(key, "last_active_at", "0")  # type: ignore[attr-defined]

    # Dead: container disappeared from substrate
    dead = WorkspaceContainerBinding(
        tenant_id="t1",
        conversation_id="dead",
        image_stack="python-minimal",
        container_id="deadcid0000000000",
        image="python:3.11-slim",
        mode="cold",
    )
    registry.bind(dead)

    def _running(sock, prefix, cid):
        if cid == "deadcid0000000000":
            return False
        return True

    docker_mock.docker_container_running.side_effect = _running

    report = manager.reap_idle()

    assert report["scanned"] == 3
    assert report["reaped_dead"] == 1
    assert report["reaped_idle"] == 1

    # Fresh survives
    assert registry.lookup(
        tenant_id="t1", conversation_id="fresh", image_stack="python-minimal"
    ) is not None
    # Idle and dead are gone
    assert registry.lookup(
        tenant_id="t1", conversation_id="idle", image_stack="python-minimal"
    ) is None
    assert registry.lookup(
        tenant_id="t1", conversation_id="dead", image_stack="python-minimal"
    ) is None

    # The idle one had its container removed; the dead one didn't (it was
    # already gone), so we expect exactly one remove call.
    docker_mock.docker_remove_container.assert_called_once()


def test_reap_idle_returns_empty_report_when_no_socket(
    registry: WorkspaceContainerRegistry,
) -> None:
    with patch(
        "motet.core.execution.workspace_container_manager.docker_client"
    ) as mod:
        mod.docker_socket_path.return_value = (None, "no socket")
        mgr = WorkspaceContainerManager(registry=registry, worker_id="wid")
        report = mgr.reap_idle()
    assert report == {"scanned": 0, "reaped_idle": 0, "reaped_dead": 0}


def test_reap_idle_skips_bindings_with_active_execs(
    manager: WorkspaceContainerManager,
    docker_mock: Any,
    registry: WorkspaceContainerRegistry,
) -> None:
    binding = WorkspaceContainerBinding(
        tenant_id="t1",
        conversation_id="busy",
        image_stack="python-minimal",
        container_id="busycid000000000",
        image="python:3.11-slim",
        mode="cold",
        active_execs=1,
    )
    registry.bind(binding)
    registry.redis.hset(  # type: ignore[attr-defined]
        "t1:workspace:container:t1:busy:__manual__:__manual__:python-minimal",
        "last_active_at",
        "0",
    )
    registry.redis.hset(  # type: ignore[attr-defined]
        "t1:workspace:container:t1:busy:__manual__:__manual__:python-minimal",
        "active_execs",
        "1",
    )

    report = manager.reap_idle()

    assert report["scanned"] == 1
    assert report["reaped_idle"] == 0
    assert registry.lookup(
        tenant_id="t1", conversation_id="busy", image_stack="python-minimal"
    ) is not None
    docker_mock.docker_remove_container.assert_not_called()


# ---------------------------------------------------------------------------
# Tenant cap eviction
# ---------------------------------------------------------------------------


def test_tenant_cap_eviction_removes_oldest_idle(
    registry: WorkspaceContainerRegistry,
    docker_mock: Any,
    fs_mock: None,
    lock_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a tenant is at cap, _enforce_tenant_cap evicts the oldest by
    last_active_at and frees a slot for the new container."""
    monkeypatch.setenv("MOTET_WORKSPACE_CONTAINER_MAX_PER_TENANT", "2")

    # Pre-fill at cap with two bindings, both alive.
    registry.bind(
        WorkspaceContainerBinding(
            tenant_id="t1",
            conversation_id="oldest",
            image_stack="python-minimal",
            container_id="oldestcid00000000",
            image="python:3.11-slim",
            mode="cold",
        )
    )
    registry.redis.hset(  # type: ignore[attr-defined]
        "t1:workspace:container:t1:oldest:__manual__:__manual__:python-minimal",
        "last_active_at",
        "100",
    )
    registry.bind(
        WorkspaceContainerBinding(
            tenant_id="t1",
            conversation_id="newer",
            image_stack="python-minimal",
            container_id="newercid000000000",
            image="python:3.11-slim",
            mode="cold",
        )
    )
    registry.redis.hset(  # type: ignore[attr-defined]
        "t1:workspace:container:t1:newer:__manual__:__manual__:python-minimal",
        "last_active_at",
        "200",
    )

    docker_mock.docker_container_running.return_value = True
    mgr = WorkspaceContainerManager(registry=registry, worker_id="wid")

    res = mgr.dispatch(
        _request(),
        tenant_id="t1",
        conversation_id="brand-new",
        image_stack="python-minimal",
        mode="cold",
    )
    assert res.exit_code == 0

    # Oldest evicted; newer still present; brand-new now bound.
    assert registry.lookup(
        tenant_id="t1", conversation_id="oldest", image_stack="python-minimal"
    ) is None
    assert registry.lookup(
        tenant_id="t1", conversation_id="newer", image_stack="python-minimal"
    ) is not None
    assert registry.lookup(
        tenant_id="t1", conversation_id="brand-new", image_stack="python-minimal"
    ) is not None

    # The cap eviction should have triggered exactly one remove for "oldest".
    removed_cids = [
        call.args[2] for call in docker_mock.docker_remove_container.call_args_list
    ]
    assert "oldestcid00000000" in removed_cids


def test_dispatch_materializes_input_files_before_exec(
    manager: WorkspaceContainerManager,
    docker_mock: Any,
) -> None:
    res = manager.dispatch(
        ExecutionRequest(
            argv=["python3", "/motet/run_once.py"],
            cwd="/scratch",
            timeout_seconds=5,
            tenant_id="t1",
            input_files=[
                ExecutionInputFile(
                    path="/motet/run_once.py",
                    content=b"print('hello')\n",
                )
            ],
        ),
        tenant_id="t1",
        conversation_id="c1",
        image_stack="python-minimal",
        mode="cold",
    )

    assert res.exit_code == 0
    docker_mock.docker_put_archive.assert_called_once()
