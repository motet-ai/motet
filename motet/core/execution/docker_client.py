"""
Motet - shared Docker Engine API client (Unix socket)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Minimal HTTP-over-Unix-socket client for the Docker Engine API, shared by
    ``core.worker_exec`` docker backend (Phase 2), MCP container lifecycle
    (Phase 2), and the per-workspace container manager.

Dependencies:
    - http.client, io, json, os, socket, struct, tarfile, urllib.parse

Usage:
    from motet.core.execution.docker_client import docker_request, docker_socket_path

Notes:
    - Optional ``HostConfig.Runtime`` (e.g. ``io.containerd.kata.v2``): see
      ``docker_engine_container_runtime`` for Phase 4 Kata + Firecracker via Docker.
    - TCP MOTET_DOCKER_HOST is not supported here (same limitation as worker exec).
    - exec helpers (``docker_exec_create`` / ``docker_exec_start`` /
      ``docker_exec_inspect``) wrap the Engine /exec endpoints used to dispatch
      argv into long-lived per-workspace containers.
    - Slice B uses ``docker_put_archive`` to ship the warm supervisor
      and the user's skill module into the per-workspace container without
      needing an interactive (hijacked-stdin) exec channel.
    - uses ``docker_get_archive`` to capture declared workspace
      shell outputs back into Motet artifacts.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import socket
import struct
import tarfile
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, unix_path: str) -> None:
        super().__init__("localhost")
        self.unix_path = unix_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.unix_path)


def docker_socket_path() -> Tuple[Optional[str], Optional[str]]:
    """Return (socket_path, error_message)."""
    raw = (os.getenv("MOTET_DOCKER_HOST") or "").strip()
    if not raw:
        return "/var/run/docker.sock", None
    if raw.startswith("unix://"):
        return raw[len("unix://") :], None
    if raw.startswith("tcp://") or raw.startswith("http://"):
        return None, (
            "MOTET_DOCKER_HOST tcp/http is not supported; "
            "use unix:///var/run/docker.sock or leave unset"
        )
    return raw, None


def api_prefix() -> str:
    ver = (os.getenv("MOTET_DOCKER_API_VERSION") or "v1.44").strip().lstrip("/")
    return f"/{ver}"


def docker_request(
    sock_path: str,
    method: str,
    path: str,
    body: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[int, bytes]:
    conn = UnixHTTPConnection(sock_path)
    try:
        hdrs = dict(headers or {})
        if body is not None and "Content-Type" not in hdrs:
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, data
    finally:
        conn.close()


def daemon_error(status: int, body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8"))
        if isinstance(payload, dict) and payload.get("message"):
            return f"Docker daemon ({status}): {payload['message']}"
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return f"Docker API HTTP {status}: {body[:500]!r}"


def auto_pull_enabled() -> bool:
    v = (os.getenv("MOTET_WORKER_EXEC_DOCKER_AUTO_PULL") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _api_message_lower(body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8"))
        if isinstance(payload, dict) and payload.get("message"):
            return str(payload["message"]).lower()
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return body.decode("utf-8", errors="replace").lower()


def create_failed_missing_image(status: int, body: bytes) -> bool:
    if status == http.client.CREATED:
        return False
    return "no such image" in _api_message_lower(body)


def docker_pull_image(sock_path: str, prefix: str, image: str) -> Tuple[bool, str]:
    q = quote(image, safe="")
    st, raw = docker_request(sock_path, "POST", f"{prefix}/images/create?fromImage={q}")
    if st != http.client.OK:
        return False, daemon_error(st, raw)
    return True, ""


def docker_engine_container_runtime(*, for_mcp: bool = False) -> Optional[str]:
    """
    Return ``HostConfig.Runtime`` for Docker Engine container/create, or ``None``.

    Phase 4: when ``MOTET_EXEC_BACKEND`` is ``kata`` or ``kata-fc``, defaults to
    ``MOTET_KATA_DOCKER_RUNTIME`` (default ``io.containerd.kata.v2``) so one-shot
    worker exec and MCP sidecars use the Kata stack (often Firecracker-backed).

    For plain ``docker`` backend, only ``MOTET_DOCKER_CONTAINER_RUNTIME`` applies.
    MCP can override with ``MOTET_MCP_DOCKER_CONTAINER_RUNTIME`` (checked first).
    """
    if for_mcp:
        v = (os.getenv("MOTET_MCP_DOCKER_CONTAINER_RUNTIME") or "").strip()
        if v:
            return v
    exe = (os.getenv("MOTET_EXEC_BACKEND") or "").strip().lower()
    if exe in ("kata", "kata-fc"):
        r = (os.getenv("MOTET_KATA_DOCKER_RUNTIME") or "io.containerd.kata.v2").strip()
        return r or None
    r = (os.getenv("MOTET_DOCKER_CONTAINER_RUNTIME") or "").strip()
    return r or None


def docker_container_exists(sock_path: str, prefix: str, container_id: str) -> bool:
    """Return True if the container is present on the daemon (any state).

    Used by the WorkspaceContainerManager (ADR-0106) before reusing a binding:
    if the daemon has reaped the container behind our back (host restart,
    docker prune, OOM-kill cleanup), we MUST detect it here so the next
    call lazily creates a fresh container instead of issuing exec into a
    container that no longer exists.
    """
    st, _ = docker_request(
        sock_path, "GET", f"{prefix}/containers/{quote(container_id, safe='')}/json"
    )
    return st == http.client.OK


def docker_container_running(sock_path: str, prefix: str, container_id: str) -> bool:
    """Return True if the container is present *and* currently running.

    ADR-0106 §rule 6 ("container death = session reset") relies on this.
    A stopped-but-not-yet-removed container is treated as dead because the
    WorkspaceContainerManager would not be able to ``docker exec`` into it,
    and reusing the binding would silently swallow scratch state from the
    operator's perspective.
    """
    import http.client as _http

    st, body = docker_request(
        sock_path, "GET", f"{prefix}/containers/{quote(container_id, safe='')}/json"
    )
    if st != _http.OK:
        return False
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    state = payload.get("State") if isinstance(payload, dict) else None
    if not isinstance(state, dict):
        return False
    return bool(state.get("Running"))


def docker_remove_container(
    sock_path: str, prefix: str, container_id: str, force: bool = True
) -> None:
    """Best-effort container removal (used by WorkspaceContainerManager reapers).

    Errors are swallowed by design: the reaper retries on the next sweep,
    and a missing container is the desired terminal state regardless.
    """
    q = "force=1&v=1" if force else "v=1"
    docker_request(
        sock_path,
        "DELETE",
        f"{prefix}/containers/{quote(container_id, safe='')}?{q}",
    )


def docker_exec_create(
    sock_path: str,
    prefix: str,
    container_id: str,
    *,
    cmd: List[str],
    workdir: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    attach_stdin: bool = False,
) -> Tuple[int, bytes]:
    """POST /containers/{id}/exec — create an exec instance.

    Caller starts the instance with ``docker_exec_start``. ADR-0106's
    ``mode: cold`` dispatch uses this pair: create + start, then read the
    multiplexed stream and inspect for exit code.
    """
    body: Dict[str, Any] = {
        "Cmd": list(cmd),
        "AttachStdin": bool(attach_stdin),
        "AttachStdout": True,
        "AttachStderr": True,
        "Tty": False,
    }
    if workdir is not None:
        body["WorkingDir"] = workdir
    if env:
        body["Env"] = [f"{k}={v}" for k, v in env.items()]
    return docker_request(
        sock_path,
        "POST",
        f"{prefix}/containers/{quote(container_id, safe='')}/exec",
        body=json.dumps(body).encode("utf-8"),
    )


def docker_exec_start(
    sock_path: str,
    prefix: str,
    exec_id: str,
    *,
    detach: bool = False,
    stdin: Optional[bytes] = None,
) -> Tuple[int, bytes]:
    """POST /exec/{id}/start — run the exec instance and stream the result.

    Returns the multiplexed (status, stream) tuple. The stream is in the
    same Docker mux frame format that ``demux_docker_stream`` understands.
    """
    body_payload: Dict[str, Any] = {
        "Detach": bool(detach),
        "Tty": False,
    }
    body = json.dumps(body_payload).encode("utf-8")

    if stdin is not None:
        # The Engine API requires raw bytes after the JSON envelope when
        # AttachStdin=True; ship a hijacked-style request via Upgrade.
        # For Slice A (workspace exec), stdin is unused; stateful mode (Slice B)
        # will revisit this path. Reject explicitly so misuse is loud.
        raise NotImplementedError(
            "docker_exec_start: stdin streaming is not implemented in Slice A; "
            "Slice B (mode: warm) will add the hijacked-connection path"
        )

    return docker_request(
        sock_path,
        "POST",
        f"{prefix}/exec/{quote(exec_id, safe='')}/start",
        body=body,
    )


def docker_exec_inspect(sock_path: str, prefix: str, exec_id: str) -> Tuple[int, bytes]:
    """GET /exec/{id}/json — used to read the ExitCode after start completes."""
    return docker_request(
        sock_path, "GET", f"{prefix}/exec/{quote(exec_id, safe='')}/json"
    )


def build_tar_archive(entries: Iterable[Tuple[str, bytes, int]]) -> bytes:
    """Build an in-memory tar archive from ``(name, data, mode)`` triples.

    Used by ADR-0106 Slice B to ship the warm supervisor + the runner's
    skill module into a workspace container in a single Engine API call
    (``PUT /containers/{id}/archive``). Returning bytes (rather than
    streaming) is fine: the only artifacts we ship are small Python
    files, well under a megabyte each.
    """
    buf = io.BytesIO()
    now = int(time.time())
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data, mode in entries:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = mode
            info.mtime = now
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def docker_put_archive(
    sock_path: str,
    prefix: str,
    container_id: str,
    *,
    target_path: str,
    tar_bytes: bytes,
) -> Tuple[int, bytes]:
    """PUT /containers/{id}/archive — extract a tar into ``target_path``.

    The Engine extracts the tar at ``target_path`` (which MUST already
    exist on the container's rootfs). Used by the stateful bootstrap
    in :class:`WorkspaceContainerManager` to drop the supervisor source
    and the user's skill module into ``/motet/`` without an interactive
    exec channel.

    Returns ``(status, body)``; the Engine returns 200 OK on success
    and 404/500 with a JSON error envelope otherwise.
    """
    qpath = quote(target_path, safe="/")
    return docker_request(
        sock_path,
        "PUT",
        f"{prefix}/containers/{quote(container_id, safe='')}/archive?path={qpath}",
        body=tar_bytes,
        headers={"Content-Type": "application/x-tar"},
    )


def docker_get_archive(
    sock_path: str,
    prefix: str,
    container_id: str,
    *,
    path: str,
) -> Tuple[int, bytes]:
    """GET /containers/{id}/archive — read a file or directory as a tar stream."""
    qpath = quote(path, safe="/")
    return docker_request(
        sock_path,
        "GET",
        f"{prefix}/containers/{quote(container_id, safe='')}/archive?path={qpath}",
    )


def demux_docker_stream(raw: bytes) -> Tuple[bytes, bytes]:
    out_chunks: List[bytes] = []
    err_chunks: List[bytes] = []
    i = 0
    while i + 8 <= len(raw):
        stream_type = raw[i]
        size = struct.unpack_from(">I", raw, i + 4)[0]
        i += 8
        if i + size > len(raw):
            break
        chunk = raw[i : i + size]
        i += size
        if stream_type == 1:
            out_chunks.append(chunk)
        elif stream_type == 2:
            err_chunks.append(chunk)
    return b"".join(out_chunks), b"".join(err_chunks)


__all__ = [
    "UnixHTTPConnection",
    "api_prefix",
    "auto_pull_enabled",
    "build_tar_archive",
    "create_failed_missing_image",
    "daemon_error",
    "demux_docker_stream",
    "docker_container_exists",
    "docker_container_running",
    "docker_engine_container_runtime",
    "docker_exec_create",
    "docker_get_archive",
    "docker_exec_inspect",
    "docker_exec_start",
    "docker_pull_image",
    "docker_put_archive",
    "docker_remove_container",
    "docker_request",
    "docker_socket_path",
]
