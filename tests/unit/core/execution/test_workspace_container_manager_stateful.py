"""
Motet — WorkspaceContainerManager stateful-mode tests (ADR-0106 Slice B)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-25

These tests pin the *manager-side* contract for warm dispatch:

    * First call lazily creates the container, ships supervisor + skill
      module via put-archive, starts the supervisor in the background,
      and waits for the bootstrap marker.
    * Second call with the same script SHA reuses the same container
      WITHOUT re-bootstrapping.
    * A different script SHA forces the binding to be replaced (the
      container is removed and a fresh one is bootstrapped).
    * Supervisor envelope on the warm client's stdout is parsed and
      passed through; ``transport_error`` is set when the daemon path
      itself fails.

The Docker engine layer (``docker_client.*``) is fully mocked. The
fixtures here mirror ``test_workspace_container_manager.py`` so the
in-memory Redis stub and lock stub stay consistent with Slice A tests.
"""

from __future__ import annotations

import http.client
import json
import struct
from typing import Any, Dict, Iterator, List, Optional, Set
from unittest.mock import patch

import pytest

from motet.core.distributed.workspace_container_registry import WorkspaceContainerRegistry
from motet.core.execution.workspace_container_manager import (
    WorkspaceContainerManager,
    WarmBootstrapPlan,
)


# ---------------------------------------------------------------------------
# In-memory Redis stub (kept local to avoid a cross-file fixture coupling)
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
        return [
            k
            for k in list(self.hashes.keys()) + list(self.sets.keys())
            if k.startswith(prefix)
        ]

    def scan_iter(self, match):
        prefix = match.rstrip("*")
        for k in list(self.hashes.keys()) + list(self.sets.keys()):
            if k.startswith(prefix):
                yield k

    def pipeline(self):
        return _FakePipeline(self)


class _FakeLock:
    def release_sync(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------


def _mux_stdout(payload: bytes) -> bytes:
    """Wrap a payload as a single stdout-stream Docker mux frame."""
    header = struct.pack(">BxxxI", 1, len(payload))
    return header + payload


def _supervisor_ok_envelope(*, request_id: str, result: Dict[str, Any]) -> bytes:
    return json.dumps(
        {
            "id": request_id,
            "ok": True,
            "result": result,
            "stdout": "",
            "stderr": "",
        }
    ).encode("utf-8")


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
    """Seed the docker_client mock with realistic stateful-mode responses."""
    with patch("motet.core.execution.workspace_container_manager.docker_client") as mod:
        mod.docker_socket_path.return_value = ("/var/run/docker.sock", None)
        mod.api_prefix.return_value = "/v1.41"
        mod.docker_engine_container_runtime.return_value = ""
        mod.auto_pull_enabled.return_value = False
        mod.create_failed_missing_image.return_value = False
        mod.daemon_error.side_effect = lambda st, body: f"docker error {st}: {body!r}"
        mod.docker_pull_image.return_value = (True, "")
        mod.docker_container_running.return_value = True
        mod.docker_remove_container.return_value = None
        mod.build_tar_archive.side_effect = lambda entries: b"<tar>"
        mod.docker_put_archive.return_value = (http.client.OK, b"")

        # /containers/create + /start
        def _docker_request(sock, method, path, body=None, headers=None):
            if "/containers/create" in path:
                return (
                    http.client.CREATED,
                    json.dumps({"Id": "warmcid000000001"}).encode("utf-8"),
                )
            if path.endswith("/start"):
                return (http.client.NO_CONTENT, b"")
            return (http.client.OK, b"")

        mod.docker_request.side_effect = _docker_request

        # Counter so we can return increasing exec ids per call.
        state = {"exec_seq": 0, "marker_calls": 0}

        def _exec_create(sock, prefix, container_id, *, cmd, **kwargs):
            state["exec_seq"] += 1
            cmd_kind = "other"
            if cmd[:1] == ["sh"] and len(cmd) >= 3 and "test -f" in cmd[2]:
                cmd_kind = "marker"
            elif cmd[:1] == ["sh"] and len(cmd) >= 3 and "mkdir" in cmd[2]:
                cmd_kind = "mkdir"
            elif cmd and cmd[0] == "python3" and any("supervisor" in c for c in cmd):
                cmd_kind = "supervisor"
            elif cmd and cmd[0] == "python3" and any("client" in c for c in cmd):
                cmd_kind = "client"
            exec_id = f"exec-{cmd_kind}-{state['exec_seq']}"
            return (
                http.client.CREATED,
                json.dumps({"Id": exec_id}).encode("utf-8"),
            )

        mod.docker_exec_create.side_effect = _exec_create

        # The marker check is the only exec whose ExitCode signals
        # ready/not-ready; we say "ready" on the first poll.
        def _exec_start(sock, prefix, exec_id, *, detach=False, stdin=None):
            if exec_id.startswith("exec-client-"):
                return (
                    http.client.OK,
                    _mux_stdout(_supervisor_ok_envelope(
                        request_id="default",
                        result={"value": 1, "exec_id": exec_id},
                    )),
                )
            return (http.client.OK, b"")

        mod.docker_exec_start.side_effect = _exec_start

        def _exec_inspect(sock, prefix, exec_id):
            # Marker check returns 0 (ready) on first poll.
            return (
                http.client.OK,
                json.dumps({"ExitCode": 0}).encode("utf-8"),
            )

        mod.docker_exec_inspect.side_effect = _exec_inspect
        mod.demux_docker_stream.side_effect = (
            lambda raw: __import__("motet").core.execution.docker_client.demux_docker_stream(
                raw
            )
        )

        yield mod


@pytest.fixture
def fs_mock(monkeypatch: pytest.MonkeyPatch) -> None:
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
    return WorkspaceContainerManager(registry=registry, worker_id="warm-worker-1")


def _plan(source: bytes = b"def handle(p):\n    return {'ok': True}\n") -> WarmBootstrapPlan:
    return WarmBootstrapPlan(script_source=source, script_logical_name="counter.py")


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def test_warm_dispatch_lazy_creates_and_bootstraps(
    manager: WorkspaceContainerManager,
    docker_mock: Any,
    registry: WorkspaceContainerRegistry,
) -> None:
    plan = _plan()
    envelope = manager.dispatch_warm(
        tenant_id="t1",
        conversation_id="c1",
        image_stack="python-minimal",
        oci_image_ref="python:3.11-slim",
        warm_plan=plan,
        params={"label": "first"},
        timeout_seconds=10,
        request_id="r1",
    )

    assert envelope["ok"] is True, envelope
    assert envelope["workspace_mode"] == "stateful"
    assert envelope["workspace_image_stack"] == "python-minimal"
    assert envelope["container_id"] == "warmcid000000001"
    assert envelope["script_sha256"] == plan.script_sha256

    # put-archive must have been called once with the warm dir as target
    assert docker_mock.docker_put_archive.call_count == 1
    _, _, cid = docker_mock.docker_put_archive.call_args.args
    target_kw = docker_mock.docker_put_archive.call_args.kwargs
    assert cid == "warmcid000000001"
    assert target_kw["target_path"] == "/motet"

    # Supervisor was started in detached mode at least once.
    detached_starts = [
        c for c in docker_mock.docker_exec_start.call_args_list
        if c.kwargs.get("detach") is True
    ]
    assert len(detached_starts) == 1

    # Binding was registered with warm metadata
    binding = registry.lookup(
        tenant_id="t1", conversation_id="c1", image_stack="python-minimal"
    )
    assert binding is not None
    assert binding.mode == "warm"
    assert (binding.metadata or {}).get("script_sha256") == plan.script_sha256
    assert (binding.metadata or {}).get("script_logical_name") == "counter.py"


def test_warm_dispatch_reuses_container_on_second_call_with_same_script(
    manager: WorkspaceContainerManager,
    docker_mock: Any,
    registry: WorkspaceContainerRegistry,
) -> None:
    plan = _plan()
    manager.dispatch_warm(
        tenant_id="t1",
        conversation_id="c1",
        image_stack="python-minimal",
        oci_image_ref="python:3.11-slim",
        warm_plan=plan,
        params={"label": "first"},
        request_id="r1",
    )
    # Reset call counts to focus on the second call's behavior.
    docker_mock.docker_put_archive.reset_mock()
    detach_count_before = sum(
        1
        for c in docker_mock.docker_exec_start.call_args_list
        if c.kwargs.get("detach") is True
    )

    envelope = manager.dispatch_warm(
        tenant_id="t1",
        conversation_id="c1",
        image_stack="python-minimal",
        oci_image_ref="python:3.11-slim",
        warm_plan=plan,
        params={"label": "second"},
        request_id="r2",
    )

    assert envelope["ok"] is True
    # No new bootstrap: no put-archive and no new detached supervisor.
    assert docker_mock.docker_put_archive.call_count == 0
    detach_count_after = sum(
        1
        for c in docker_mock.docker_exec_start.call_args_list
        if c.kwargs.get("detach") is True
    )
    assert detach_count_after == detach_count_before

    # And the binding is unchanged
    binding = registry.lookup(
        tenant_id="t1", conversation_id="c1", image_stack="python-minimal"
    )
    assert binding is not None
    assert binding.container_id == "warmcid000000001"


def test_warm_dispatch_replaces_container_when_script_sha_changes(
    manager: WorkspaceContainerManager,
    docker_mock: Any,
    registry: WorkspaceContainerRegistry,
) -> None:
    plan_v1 = _plan(b"def handle(p):\n    return {'v': 1}\n")
    plan_v2 = _plan(b"def handle(p):\n    return {'v': 2}\n")

    manager.dispatch_warm(
        tenant_id="t1",
        conversation_id="c1",
        image_stack="python-minimal",
        oci_image_ref="python:3.11-slim",
        warm_plan=plan_v1,
        params={},
        request_id="r1",
    )

    # Make the docker_request return a *different* container id on the
    # second create so we can prove it was replaced.
    def _docker_request_v2(sock, method, path, body=None, headers=None):
        if "/containers/create" in path:
            return (
                http.client.CREATED,
                json.dumps({"Id": "warmcid000000002"}).encode("utf-8"),
            )
        if path.endswith("/start"):
            return (http.client.NO_CONTENT, b"")
        return (http.client.OK, b"")

    docker_mock.docker_request.side_effect = _docker_request_v2

    envelope = manager.dispatch_warm(
        tenant_id="t1",
        conversation_id="c1",
        image_stack="python-minimal",
        oci_image_ref="python:3.11-slim",
        warm_plan=plan_v2,
        params={},
        request_id="r2",
    )
    assert envelope["ok"] is True

    binding = registry.lookup(
        tenant_id="t1", conversation_id="c1", image_stack="python-minimal"
    )
    assert binding is not None
    assert binding.container_id == "warmcid000000002"
    assert (binding.metadata or {}).get("script_sha256") == plan_v2.script_sha256

    # Old container must have been removed
    removed_ids = [c.args[2] for c in docker_mock.docker_remove_container.call_args_list]
    assert "warmcid000000001" in removed_ids


def test_warm_dispatch_returns_transport_error_when_put_archive_fails(
    manager: WorkspaceContainerManager,
    docker_mock: Any,
) -> None:
    docker_mock.docker_put_archive.return_value = (
        http.client.INTERNAL_SERVER_ERROR,
        b'{"message": "boom"}',
    )
    envelope = manager.dispatch_warm(
        tenant_id="t1",
        conversation_id="c1",
        image_stack="python-minimal",
        oci_image_ref="python:3.11-slim",
        warm_plan=_plan(),
        params={},
        request_id="r1",
    )
    assert envelope["ok"] is False
    assert envelope["transport_error"] is True
    assert "put-archive" in envelope["error"]


def test_warm_dispatch_returns_transport_error_when_marker_never_appears(
    manager: WorkspaceContainerManager,
    docker_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ExitCode != 0 means "marker not yet present"; the manager will spin
    # until timeout. Configure a tiny timeout so the test is fast.
    monkeypatch.setenv("MOTET_WORKSPACE_CONTAINER_WARM_BOOTSTRAP_TIMEOUT", "0.5")

    def _exec_inspect_never_ready(sock, prefix, exec_id):
        return (
            http.client.OK,
            json.dumps({"ExitCode": 1}).encode("utf-8"),
        )

    docker_mock.docker_exec_inspect.side_effect = _exec_inspect_never_ready

    envelope = manager.dispatch_warm(
        tenant_id="t1",
        conversation_id="c1",
        image_stack="python-minimal",
        oci_image_ref="python:3.11-slim",
        warm_plan=_plan(),
        params={},
        request_id="r1",
    )
    assert envelope["ok"] is False
    assert envelope["transport_error"] is True
    # Manager's bootstrap-timeout error references either the marker file
    # path or the supervisor's failure to write it; both are acceptable.
    err = envelope["error"].lower()
    assert "bootstrap" in err
    assert ".bootstrapped" in err or "marker" in err


def test_warm_dispatch_passes_params_via_request_envelope(
    manager: WorkspaceContainerManager,
    docker_mock: Any,
) -> None:
    """The base64-encoded request env var must round-trip params.

    We capture the env passed to ``docker_exec_create`` for the warm
    client invocation and decode it back to JSON.
    """
    captured_envs: List[Dict[str, str]] = []

    original_exec_create = docker_mock.docker_exec_create.side_effect

    def _capturing_exec_create(sock, prefix, container_id, *, cmd, env=None, **kwargs):
        if cmd[:1] == ["python3"] and len(cmd) >= 2 and "client" in cmd[1]:
            captured_envs.append(dict(env or {}))
        return original_exec_create(sock, prefix, container_id, cmd=cmd, env=env, **kwargs)

    docker_mock.docker_exec_create.side_effect = _capturing_exec_create

    manager.dispatch_warm(
        tenant_id="t1",
        conversation_id="c1",
        image_stack="python-minimal",
        oci_image_ref="python:3.11-slim",
        warm_plan=_plan(),
        params={"label": "hello", "n": 3},
        request_id="req-xyz",
    )

    assert captured_envs, "warm client exec was never invoked"
    last_env = captured_envs[-1]
    assert "MOTET_WARM_REQUEST_B64" in last_env
    assert last_env["MOTET_WARM_SOCKET"] == "/motet/warm.sock"

    import base64

    decoded = json.loads(base64.b64decode(last_env["MOTET_WARM_REQUEST_B64"]))
    assert decoded == {
        "id": "req-xyz",
        "params": {"label": "hello", "n": 3},
    }
