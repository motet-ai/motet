"""
Motet - Docker Engine API execution backend (Phase 2 one-shot)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-25

Description:
    Runs ExecutionRequest in a disposable container via the Docker Engine HTTP API
    over a Unix socket (default /var/run/docker.sock). Host cwd is bind-mounted
    into the container at a fixed workdir. Intended for MOTET_EXEC_BACKEND=docker
    when the worker has Docker socket access (e.g. Compose).

Dependencies:
    - motet.core.execution.docker_client
    - json, os, threading, urllib.parse

Notes:
    - Same MOTET_WORKER_EXEC_CWD_ALLOWLIST as the subprocess backend.
    - MOTET_WORKER_EXEC_DOCKER_IMAGE defaults to python:3.11-slim.
    - stdin is not supported yet (returns a clear error if request.stdin is set).
    - MOTET_DOCKER_HOST: unix://path or absolute socket path; tcp URLs are rejected for now.
    - MOTET_DOCKER_API_VERSION defaults to v1.44 (matches daemons that reject <1.44); override for older engines.
    - MOTET_WORKER_EXEC_DOCKER_AUTO_PULL (default 1): on container/create \"no such image\", pull via
      POST /images/create then retry create once (similar to docker run).
    - MOTET_DOCKER_CONTAINER_RUNTIME: optional Engine ``HostConfig.Runtime`` when
      MOTET_EXEC_BACKEND=docker (e.g. kata runtime name if the daemon default is runc).
    - MOTET_WORKER_EXEC_DOCKER_HOST_CWD_ROOT maps the worker-visible cwd
      root to the host-visible path used by the Docker daemon bind mount.
    - Phase 4: MOTET_EXEC_BACKEND=kata|kata-fc uses run_kata_docker (same Engine API) with
      MOTET_KATA_DOCKER_RUNTIME (default io.containerd.kata.v2).
"""

from __future__ import annotations

import http.client
import json
import os
import threading
from typing import Any, Dict, List
from urllib.parse import quote

from .. import docker_client
from ..capture import truncate_output_pair
from ..cwd_allowlist import worker_exec_cwd_allowed
from ..models import ExecutionRequest, ExecutionResult


def _network_mode(network: str) -> str:
    if network == "none":
        return "none"
    if network == "inherit":
        return "default"
    if network == "restricted":
        custom = (os.getenv("MOTET_WORKER_EXEC_DOCKER_NETWORK") or "").strip()
        return custom or "bridge"
    return "default"


def _docker_bind_source(cwd_abs: str) -> tuple[str | None, str | None]:
    """Resolve the host-visible source path for Docker bind mounts.

    Workers often talk to the host Docker daemon through /var/run/docker.sock.
    In that topology, a worker-container path such as /var/motet/worker-exec
    is not necessarily visible to the host daemon at the same absolute path.
    MOTET_WORKER_EXEC_DOCKER_HOST_CWD_ROOT provides the host-side root for the
    same directory tree so files staged by the worker are mounted into /work.
    """
    host_root = (os.getenv("MOTET_WORKER_EXEC_DOCKER_HOST_CWD_ROOT") or "").strip()
    if not host_root:
        return cwd_abs, None

    allow_root = (os.getenv("MOTET_WORKER_EXEC_DOCKER_CONTAINER_CWD_ROOT") or "").strip()
    if not allow_root:
        allowlist = (os.getenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST") or "").strip()
        allow_root = next((part.strip() for part in allowlist.split(",") if part.strip()), "")
    if not allow_root:
        return None, (
            "MOTET_WORKER_EXEC_DOCKER_HOST_CWD_ROOT requires "
            "MOTET_WORKER_EXEC_CWD_ALLOWLIST or MOTET_WORKER_EXEC_DOCKER_CONTAINER_CWD_ROOT"
        )

    container_root = os.path.abspath(allow_root)
    try:
        rel = os.path.relpath(cwd_abs, container_root)
    except ValueError:
        return None, f"cwd {cwd_abs!r} cannot be mapped from container root {container_root!r}"
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None, f"cwd {cwd_abs!r} is outside Docker bind root {container_root!r}"

    return os.path.abspath(os.path.join(host_root, rel)), None


def run_docker(
    request: ExecutionRequest,
    *,
    backend_label: str = "docker",
    container_runtime: str | None = None,
) -> ExecutionResult:
    cwd_abs = os.path.abspath(request.cwd.strip())
    ok_cwd, deny = worker_exec_cwd_allowed(cwd_abs)
    if not ok_cwd:
        return ExecutionResult(
            exit_code=-1,
            backend=backend_label,
            error=deny,
        )

    if request.stdin is not None:
        return ExecutionResult(
            exit_code=-1,
            backend=backend_label,
            error="docker execution backend does not support stdin yet",
        )

    sock_path, sock_err = docker_client.docker_socket_path()
    if sock_err:
        return ExecutionResult(exit_code=-1, backend=backend_label, error=sock_err)
    assert sock_path is not None

    if not os.path.exists(sock_path):
        return ExecutionResult(
            exit_code=-1,
            backend=backend_label,
            error=f"Docker unix socket not found at {sock_path!r}",
        )

    try:
        os.makedirs(cwd_abs, mode=0o700, exist_ok=True)
    except OSError as e:
        return ExecutionResult(
            exit_code=-1,
            backend=backend_label,
            error=f"cannot create cwd {cwd_abs!r}: {e}",
        )

    image = (request.oci_image_ref or "").strip() or (
        os.getenv("MOTET_WORKER_EXEC_DOCKER_IMAGE") or "python:3.11-slim"
    ).strip()
    workdir = (os.getenv("MOTET_WORKER_EXEC_DOCKER_WORKDIR") or "/work").strip() or "/work"

    bind_source, bind_error = _docker_bind_source(cwd_abs)
    if bind_error:
        return ExecutionResult(
            exit_code=-1,
            backend=backend_label,
            oci_image_ref=image,
            error=bind_error,
        )
    assert bind_source is not None

    bind = f"{bind_source}:{workdir}:rw"
    net_mode = _network_mode(request.network)

    rt = (container_runtime or "").strip() or None
    if rt is None:
        rt = docker_client.docker_engine_container_runtime(for_mcp=False)

    host_cfg: Dict[str, Any] = {
        "Binds": [bind],
        "NetworkMode": net_mode,
        "AutoRemove": False,
    }
    if rt:
        host_cfg["Runtime"] = rt

    engine_runtime_resolved: str | None = rt if rt else None

    create_body: Dict[str, Any] = {
        "Image": image,
        "Cmd": list(request.argv),
        "WorkingDir": workdir,
        "AttachStdout": True,
        "AttachStderr": True,
        "Tty": False,
        "OpenStdin": False,
        "HostConfig": host_cfg,
    }

    prefix = docker_client.api_prefix()
    create_json = json.dumps(create_body).encode("utf-8")

    def _create_container() -> tuple[int, bytes]:
        return docker_client.docker_request(
            sock_path,
            "POST",
            f"{prefix}/containers/create",
            body=create_json,
        )

    status, data = _create_container()
    if (
        status != http.client.CREATED
        and docker_client.auto_pull_enabled()
        and docker_client.create_failed_missing_image(status, data)
    ):
        pulled, pull_err = docker_client.docker_pull_image(sock_path, prefix, image)
        if not pulled:
            return ExecutionResult(
                exit_code=-1,
                backend=backend_label,
                oci_image_ref=image,
                engine_runtime=engine_runtime_resolved,
                error=f"{docker_client.daemon_error(status, data)} (auto-pull: {pull_err})",
            )
        status, data = _create_container()

    if status != http.client.CREATED:
        return ExecutionResult(
            exit_code=-1,
            backend=backend_label,
            oci_image_ref=image,
            engine_runtime=engine_runtime_resolved,
            error=docker_client.daemon_error(status, data),
        )

    try:
        created = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ExecutionResult(
            exit_code=-1,
            backend=backend_label,
            error=f"invalid create response: {data[:300]!r}",
        )

    cid = created.get("Id")
    if not cid or not isinstance(cid, str):
        return ExecutionResult(
            exit_code=-1,
            backend=backend_label,
            oci_image_ref=image,
            engine_runtime=engine_runtime_resolved,
            error=f"create response missing Id: {created!r}",
        )

    timeout_s = (
        request.timeout_seconds
        if request.timeout_seconds is not None
        else int(os.getenv("MOTET_WORKER_EXEC_DEFAULT_TIMEOUT", "120"))
    )

    status, data = docker_client.docker_request(
        sock_path, "POST", f"{prefix}/containers/{quote(cid, safe='')}/start"
    )
    if status not in (http.client.NO_CONTENT, http.client.OK):
        docker_client.docker_request(
            sock_path,
            "DELETE",
            f"{prefix}/containers/{quote(cid, safe='')}?force=1",
        )
        return ExecutionResult(
            exit_code=-1,
            backend=backend_label,
            backend_ref=cid[:12],
            oci_image_ref=image,
            engine_runtime=engine_runtime_resolved,
            error=docker_client.daemon_error(status, data),
        )

    wait_body: Dict[str, Any] = {}
    wait_err: List[str] = []
    done = threading.Event()

    def _wait_thread() -> None:
        try:
            st, body = docker_client.docker_request(
                sock_path,
                "POST",
                f"{prefix}/containers/{quote(cid, safe='')}/wait",
            )
            if st != http.client.OK:
                wait_err.append(docker_client.daemon_error(st, body))
                return
            try:
                wait_body.update(json.loads(body.decode("utf-8")))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                wait_err.append(f"wait response: {e}")
        finally:
            done.set()

    th = threading.Thread(target=_wait_thread, name="docker-wait", daemon=True)
    th.start()
    timed_out = not done.wait(timeout=float(timeout_s))
    if timed_out:
        docker_client.docker_request(
            sock_path,
            "POST",
            f"{prefix}/containers/{quote(cid, safe='')}/kill",
        )
        done.wait(timeout=15.0)

    log_q = "stdout=1&stderr=1&timestamps=0"
    st_logs, log_raw = docker_client.docker_request(
        sock_path,
        "GET",
        f"{prefix}/containers/{quote(cid, safe='')}/logs?{log_q}",
    )
    stdout_b, stderr_b = (b"", b"")
    if st_logs == http.client.OK:
        stdout_b, stderr_b = docker_client.demux_docker_stream(log_raw)
    else:
        if not wait_err:
            wait_err.append(docker_client.daemon_error(st_logs, log_raw))

    docker_client.docker_request(
        sock_path,
        "DELETE",
        f"{prefix}/containers/{quote(cid, safe='')}?force=1&v=1",
    )

    if timed_out:
        return ExecutionResult(
            exit_code=-1,
            backend=backend_label,
            backend_ref=cid[:12],
            oci_image_ref=image,
            engine_runtime=engine_runtime_resolved,
            timed_out=True,
        )

    if wait_err:
        return ExecutionResult(
            exit_code=-1,
            backend=backend_label,
            backend_ref=cid[:12],
            oci_image_ref=image,
            engine_runtime=engine_runtime_resolved,
            error="; ".join(wait_err),
        )

    code = int(wait_body.get("StatusCode", -1))
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    stdout, stderr, otrunc, etrunc = truncate_output_pair(
        stdout, stderr, request.max_output_bytes
    )

    return ExecutionResult(
        exit_code=code,
        stdout=stdout,
        stderr=stderr,
        timed_out=False,
        stdout_truncated=otrunc,
        stderr_truncated=etrunc,
        backend=backend_label,
        backend_ref=cid[:12],
        oci_image_ref=image,
        engine_runtime=engine_runtime_resolved,
    )


__all__ = ["run_docker"]
