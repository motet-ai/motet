"""Tests for Docker Engine API execution backend (mocked HTTP over unix)."""

from __future__ import annotations

import json
from typing import List, Tuple

import http.client

import pytest

from motet.core.execution import ExecutionRequest
from motet.core.execution import docker_client
from motet.core.execution.backends import docker as docker_backend


def _mux(stdout: bytes, stderr: bytes) -> bytes:
    frames = []
    if stdout:
        frames.append(b"\x01\x00\x00\x00" + len(stdout).to_bytes(4, "big") + stdout)
    if stderr:
        frames.append(b"\x02\x00\x00\x00" + len(stderr).to_bytes(4, "big") + stderr)
    return b"".join(frames)


def test_docker_rejects_stdin(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    root = tmp_path / "a"
    root.mkdir()
    monkeypatch.setenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", str(root))
    calls: List[Tuple[str, str]] = []

    def _fake_request(
        sock_path: str, method: str, path: str, body: bytes | None = None, headers=None
    ):
        calls.append((method, path))
        return 500, b"{}"

    monkeypatch.setattr(docker_client, "docker_request", _fake_request)
    r = docker_backend.run_docker(
        ExecutionRequest(argv=["echo", "hi"], cwd=str(root), stdin="nope")
    )
    assert r.error and "stdin" in r.error.lower()
    assert not calls


def test_docker_rejects_tcp_docker_host(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    root = tmp_path / "a"
    root.mkdir()
    monkeypatch.setenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", str(root))
    monkeypatch.setenv("MOTET_DOCKER_HOST", "tcp://127.0.0.1:2375")
    r = docker_backend.run_docker(ExecutionRequest(argv=["true"], cwd=str(root)))
    assert r.error and "tcp" in r.error.lower()


def test_docker_happy_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.setenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", str(root))
    monkeypatch.setenv("MOTET_WORKER_EXEC_DOCKER_IMAGE", "python:3.11-slim")

    cid = "deadbeefcafebabe0123456789abcdef0123456789abcdef0123456789ab"

    def _fake_request(
        sock_path: str,
        method: str,
        path: str,
        body: bytes | None = None,
        headers=None,
    ):
        if method == "POST" and path.endswith("/containers/create"):
            return http.client.CREATED, json.dumps({"Id": cid}).encode()
        if method == "POST" and f"/containers/{cid}/start" in path:
            return http.client.NO_CONTENT, b""
        if method == "POST" and f"/containers/{cid}/wait" in path:
            return http.client.OK, json.dumps({"StatusCode": 0}).encode()
        if method == "GET" and "/logs" in path:
            return http.client.OK, _mux(b"out\n", b"err\n")
        if method == "DELETE" and f"/containers/{cid}" in path:
            return http.client.NO_CONTENT, b""
        return 500, json.dumps({"message": f"unexpected {method} {path}"}).encode()

    monkeypatch.setattr(docker_client, "docker_request", _fake_request)
    r = docker_backend.run_docker(
        ExecutionRequest(argv=["python", "-c", "print(1)"], cwd=str(root))
    )
    assert r.exit_code == 0
    assert r.backend == "docker"
    assert r.stdout == "out\n"
    assert r.stderr == "err\n"
    assert r.backend_ref == cid[:12]
    assert r.oci_image_ref == "python:3.11-slim"
    assert r.engine_runtime is None


def test_docker_maps_worker_cwd_to_host_bind_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Worker-container cwds can be translated before calling the host daemon."""
    worker_root = tmp_path / "container" / "worker-exec"
    host_root = tmp_path / "host" / "worker-exec"
    run_dir = worker_root / "runs" / "abc"
    run_dir.mkdir(parents=True)
    host_root.mkdir(parents=True)

    monkeypatch.setenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", str(worker_root))
    monkeypatch.setenv("MOTET_WORKER_EXEC_DOCKER_HOST_CWD_ROOT", str(host_root))

    captured: dict = {}
    cid = "f00d" * 16

    def _fake_request(
        sock_path: str,
        method: str,
        path: str,
        body: bytes | None = None,
        headers=None,
    ):
        if method == "POST" and path.endswith("/containers/create"):
            captured["create"] = json.loads(body.decode("utf-8"))
            return http.client.CREATED, json.dumps({"Id": cid}).encode()
        if method == "POST" and f"/containers/{cid}/start" in path:
            return http.client.NO_CONTENT, b""
        if method == "POST" and f"/containers/{cid}/wait" in path:
            return http.client.OK, json.dumps({"StatusCode": 0}).encode()
        if method == "GET" and "/logs" in path:
            return http.client.OK, b""
        if method == "DELETE" and f"/containers/{cid}" in path:
            return http.client.NO_CONTENT, b""
        return 500, json.dumps({"message": f"unexpected {method} {path}"}).encode()

    monkeypatch.setattr(docker_client, "docker_request", _fake_request)
    r = docker_backend.run_docker(ExecutionRequest(argv=["true"], cwd=str(run_dir)))

    assert r.exit_code == 0
    assert captured["create"]["HostConfig"]["Binds"] == [
        f"{host_root / 'runs' / 'abc'}:/work:rw"
    ]


def test_docker_auto_pull_then_create(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """First create returns no such image; pull runs; second create succeeds."""
    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.setenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", str(root))
    monkeypatch.setenv("MOTET_WORKER_EXEC_DOCKER_IMAGE", "python:3.11-slim")
    cid = "bbbbccccddddeeeeffffaaaabbbbccccddddeeeeffffaaaabbbbcccc"

    calls: list[str] = []

    def _fake_request(
        sock_path: str,
        method: str,
        path: str,
        body: bytes | None = None,
        headers=None,
    ):
        calls.append(path)
        if method == "POST" and path.endswith("/containers/create"):
            if calls.count(path) == 1:
                return 404, json.dumps(
                    {"message": "No such image: python:3.11-slim"}
                ).encode()
            return http.client.CREATED, json.dumps({"Id": cid}).encode()
        if method == "POST" and "/images/create" in path:
            return http.client.OK, b'{"status":"Pull complete"}\n'
        if method == "POST" and "/start" in path:
            return http.client.NO_CONTENT, b""
        if method == "POST" and "/wait" in path:
            return http.client.OK, json.dumps({"StatusCode": 0}).encode()
        if method == "GET" and "/logs" in path:
            return http.client.OK, b""
        if method == "DELETE":
            return http.client.NO_CONTENT, b""
        return 500, b"{}"

    monkeypatch.setattr(docker_client, "docker_request", _fake_request)
    r = docker_backend.run_docker(ExecutionRequest(argv=["true"], cwd=str(root)))
    assert r.exit_code == 0
    assert sum(1 for p in calls if p.endswith("/containers/create")) == 2
    assert any("/images/create" in p for p in calls)


def test_run_execution_docker_backend_mocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    root = tmp_path / "r"
    root.mkdir()
    monkeypatch.setenv("MOTET_EXEC_BACKEND", "docker")
    monkeypatch.setenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", str(root))
    monkeypatch.setenv("MOTET_WORKER_EXEC_DOCKER_IMAGE", "alpine:3.19")

    cid = "aaaabbbbccccddddeeeeffffaaaabbbbccccddddeeeeffffaaaabbbb"

    def _fake_request(sock_path, method, path, body=None, headers=None):
        if method == "POST" and path.endswith("/containers/create"):
            return http.client.CREATED, json.dumps({"Id": cid}).encode()
        if method == "POST" and "/start" in path:
            return http.client.NO_CONTENT, b""
        if method == "POST" and "/wait" in path:
            return http.client.OK, json.dumps({"StatusCode": 42}).encode()
        if method == "GET" and "/logs" in path:
            return http.client.OK, b""
        if method == "DELETE":
            return http.client.NO_CONTENT, b""
        return 500, b"{}"

    monkeypatch.setattr(docker_client, "docker_request", _fake_request)
    from motet.core.execution import run_execution

    res = run_execution(
        ExecutionRequest(argv=["/bin/sh", "-c", "exit 0"], cwd=str(root))
    )
    assert res.exit_code == 42
    assert res.backend == "docker"
    assert res.oci_image_ref == "alpine:3.19"
    assert res.engine_runtime is None


def test_docker_create_includes_optional_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.setenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", str(root))
    monkeypatch.setenv("MOTET_DOCKER_CONTAINER_RUNTIME", "io.containerd.kata.v2")

    captured: dict = {}

    def _fake_request(
        sock_path: str,
        method: str,
        path: str,
        body: bytes | None = None,
        headers=None,
    ):
        if method == "POST" and path.endswith("/containers/create"):
            captured["create"] = json.loads(body.decode("utf-8"))
            return http.client.CREATED, json.dumps({"Id": "ab" * 32}).encode()
        if method == "POST" and "/start" in path:
            return http.client.NO_CONTENT, b""
        if method == "POST" and "/wait" in path:
            return http.client.OK, json.dumps({"StatusCode": 0}).encode()
        if method == "GET" and "/logs" in path:
            return http.client.OK, b""
        if method == "DELETE":
            return http.client.NO_CONTENT, b""
        return 500, b"{}"

    monkeypatch.setenv("MOTET_EXEC_BACKEND", "docker")
    monkeypatch.setattr(docker_client, "docker_request", _fake_request)
    r = docker_backend.run_docker(ExecutionRequest(argv=["true"], cwd=str(root)))
    assert r.exit_code == 0
    assert captured["create"]["HostConfig"]["Runtime"] == "io.containerd.kata.v2"
    assert r.engine_runtime == "io.containerd.kata.v2"
    assert r.oci_image_ref == "python:3.11-slim"


def test_run_execution_kata_fc_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.setenv("MOTET_EXEC_BACKEND", "kata-fc")
    monkeypatch.setenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", str(root))

    captured: dict = {}

    def _fake_request(
        sock_path: str,
        method: str,
        path: str,
        body: bytes | None = None,
        headers=None,
    ):
        if method == "POST" and path.endswith("/containers/create"):
            captured["create"] = json.loads(body.decode("utf-8"))
            return http.client.CREATED, json.dumps({"Id": "cd" * 32}).encode()
        if method == "POST" and "/start" in path:
            return http.client.NO_CONTENT, b""
        if method == "POST" and "/wait" in path:
            return http.client.OK, json.dumps({"StatusCode": 7}).encode()
        if method == "GET" and "/logs" in path:
            return http.client.OK, b""
        if method == "DELETE":
            return http.client.NO_CONTENT, b""
        return 500, b"{}"

    monkeypatch.setattr(docker_client, "docker_request", _fake_request)
    from motet.core.execution import run_execution

    res = run_execution(ExecutionRequest(argv=["true"], cwd=str(root)))
    assert res.exit_code == 7
    assert res.backend == "kata-fc"
    assert captured["create"]["HostConfig"]["Runtime"] == "io.containerd.kata.v2"
    assert res.engine_runtime == "io.containerd.kata.v2"
    assert res.oci_image_ref == "python:3.11-slim"
