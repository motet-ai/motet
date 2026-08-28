"""
Motet - MCP stdio server inside Docker (Phase 2)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Starts long-lived MCP servers as Docker containers with JSON-RPC over
    multiplexed attach (stdin/stdout/stderr), presenting an asyncio-compatible
    process surface for ``MotetMCPProxy``. A background ``/wait`` monitor plus
    attach-EOF marks ``returncode`` so a ``docker kill`` is visible to the
    manager health loop without blocking the shared asyncio event loop.

Dependencies:
    - motet.core.execution.docker_client
    - motet.core.execution.cwd_allowlist
    - asyncio, json, os, socket, struct, threading

Notes:
    - Requires Docker unix socket (same as worker_exec docker backend).
    - Default image ``MOTET_MCP_DOCKER_IMAGE`` (fallback ``node:20-bookworm-slim``).
    - Host working dir bind uses ``MOTET_WORKER_EXEC_CWD_ALLOWLIST``.
    - ``MOTET_MCP_DOCKER_NETWORK`` selects NetworkMode (builder uses ``motet_dev_network``).
    - ``MOTET_MCP_DOCKER_EXTRA_HOSTS`` is a comma-separated ExtraHosts list
      (e.g. ``host.docker.internal:host-gateway``).
    - Sets Docker ``Entrypoint`` to the MCP argv and ``Cmd`` to ```` so images with a
      worker ``ENTRYPOINT`` (e.g. ``/entrypoint.sh``) still run the MCP binary directly.
    - ``asyncio.StreamReader`` limits match ``MotetMCPProxy`` subprocess limits so large
      single-line JSON-RPC (OpenAPI tool catalogs) does not hit the default 64KiB cap.
    - Attach socket uses a short timeout so a dead peer cannot block ``sendall`` on
      the manager event loop.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import socket
import struct
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

import structlog

from motet.core.execution.cwd_allowlist import worker_exec_cwd_allowed
from motet.core.execution.docker_client import (
    api_prefix,
    auto_pull_enabled,
    create_failed_missing_image,
    daemon_error,
    docker_engine_container_runtime,
    docker_pull_image,
    docker_request,
    docker_socket_path,
)
from motet.core.execution.mcp_docker_cleanup import mcp_docker_container_labels

logger = structlog.get_logger(__name__)


def _default_mcp_image() -> str:
    return (
        os.getenv("MOTET_MCP_DOCKER_IMAGE")
        or os.getenv("MOTET_WORKER_EXEC_DOCKER_IMAGE")
        or "node:20-bookworm-slim"
    ).strip()


def _stdio_stream_reader_limit(command: str, args: List[str]) -> int:
    """Match ``MotetMCPProxy`` buffer limits (openapi_adapter lines can exceed 1 MiB)."""
    parts = [command] + list(args)
    if any("openapi_adapter" in str(a) for a in parts):
        return 10 * 1024 * 1024
    return 1024 * 1024


def _container_workdir(host_workdir_configured: bool) -> str:
    explicit = (os.getenv("MOTET_MCP_DOCKER_WORKDIR") or "").strip()
    if explicit:
        return explicit
    return "/work" if host_workdir_configured else "/"


_ATTACH_IO_TIMEOUT_SECONDS = 2.0


class DockerStdinWriter:
    """Minimal asyncio-compatible stdin for multiplexed Docker attach (stream 0)."""

    def __init__(self, sock: socket.socket, send_lock: threading.Lock) -> None:
        self._sock = sock
        self._send_lock = send_lock

    def write(self, data: bytes) -> None:
        if not data:
            return
        # Docker attach expects raw stdin bytes from client -> daemon.
        # Multiplexed 8-byte framing is only for daemon -> client stdout/stderr.
        # Framing stdin here prevents MCP servers from receiving valid JSON-RPC lines.
        with self._send_lock:
            try:
                self._sock.sendall(data)
            except (OSError, socket.timeout) as e:
                raise BrokenPipeError(
                    f"MCP docker attach stdin closed: {e}"
                ) from e

    async def drain(self) -> None:
        await asyncio.sleep(0)


class DockerMCPAsyncProcess:
    """
    Subset of ``asyncio.subprocess.Process`` used by ``MotetMCPProxy`` / stdio transport.
    """

    def __init__(
        self,
        *,
        stdin: DockerStdinWriter,
        stdout: asyncio.StreamReader,
        stderr: asyncio.StreamReader,
        raw_sock: socket.socket,
        container_id: str,
        sock_path: str,
        api_pfx: str,
    ) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self._raw_sock = raw_sock
        self._container_id = container_id
        self._sock_path = sock_path
        self._api_pfx = api_pfx
        try:
            self.pid = int(container_id[:8], 16) % (2**31 - 1)
        except ValueError:
            self.pid = 1
        self.returncode: Optional[int] = None
        self._exit_event = threading.Event()
        self._exit_lock = threading.Lock()
        self._wait_monitor: Optional[threading.Thread] = None

    def _set_returncode(self, code: int) -> None:
        with self._exit_lock:
            if self.returncode is None:
                self.returncode = code
        self._exit_event.set()

    def _close_attach(self) -> None:
        try:
            self._raw_sock.close()
        except OSError:
            pass

    def mark_attach_closed(self) -> None:
        """Attach mux ended — treat as process exit if ``/wait`` has not returned."""
        self._set_returncode(-1)
        self._close_attach()

    def start_exit_monitor(self) -> None:
        """Watch Docker ``/wait`` so ``returncode`` updates after crash or ``docker kill``."""
        if self._wait_monitor is not None:
            return
        self._wait_monitor = threading.Thread(
            target=self._sync_wait_exit,
            name=f"mcp-docker-wait-{self._container_id[:12]}",
            daemon=True,
        )
        self._wait_monitor.start()

    def _sync_wait_exit(self) -> None:
        code = -1
        try:
            st, body = docker_request(
                self._sock_path,
                "POST",
                f"{self._api_pfx}/containers/{quote(self._container_id, safe='')}/wait",
            )
            if st == http.client.OK:
                try:
                    payload = json.loads(body.decode("utf-8"))
                    code = int(payload.get("StatusCode", -1))
                except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                    code = -1
            elif st == http.client.NOT_FOUND:
                code = -1
        except Exception:
            code = -1
        finally:
            self._set_returncode(code)
            self._close_attach()

    def terminate(self) -> None:
        try:
            docker_request(
                self._sock_path,
                "POST",
                f"{self._api_pfx}/containers/{quote(self._container_id, safe='')}/stop?t=5",
            )
        except Exception as e:
            logger.debug("mcp_docker_stop", container_id=self._container_id[:12], error=str(e))

    def kill(self) -> None:
        try:
            docker_request(
                self._sock_path,
                "POST",
                f"{self._api_pfx}/containers/{quote(self._container_id, safe='')}/kill",
            )
        except Exception as e:
            logger.debug("mcp_docker_kill", container_id=self._container_id[:12], error=str(e))

    async def wait(self) -> int:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._exit_event.wait)
        return int(self.returncode if self.returncode is not None else -1)


def _attach_connect(sock_path: str, prefix: str, cid: str) -> Tuple[socket.socket, bytes]:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    path = f"{prefix}/containers/{quote(cid, safe='')}/attach?stream=1&stdin=1&stdout=1&stderr=1"
    req = (
        f"POST {path} HTTP/1.1\r\n"
        "Host: localhost\r\n"
        # Request HTTP hijack for full-duplex stdin/stdout over one socket.
        # Without upgrade headers, Docker may stream stdout but ignore client stdin.
        "Connection: Upgrade\r\n"
        "Upgrade: tcp\r\n"
        "Content-Length: 0\r\n"
        "\r\n"
    ).encode("ascii")
    s.sendall(req)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(65536)
        if not chunk:
            s.close()
            raise RuntimeError("docker attach closed before HTTP headers")
        buf += chunk
    split_at = buf.index(b"\r\n\r\n") + 4
    headers_text = buf[: split_at - 4].decode("utf-8", errors="replace")
    first = headers_text.split("\r\n", 1)[0] if headers_text else ""
    if (
        " 200 " not in first
        and not first.endswith(" 200 OK")
        and " 101 " not in first
        and not first.endswith(" 101 UPGRADED")
    ):
        s.close()
        raise RuntimeError(f"docker attach failed: {first!r}")
    remainder = buf[split_at:]
    return s, remainder


def _mux_reader_loop(
    sock: socket.socket,
    initial: bytes,
    stdout: asyncio.StreamReader,
    stderr: asyncio.StreamReader,
    loop: asyncio.AbstractEventLoop,
    on_done: Callable[[], None],
) -> None:
    buf = initial
    try:
        while True:
            while len(buf) >= 8:
                stream_type = buf[0]
                size = struct.unpack_from(">I", buf, 4)[0]
                if len(buf) < 8 + size:
                    break
                chunk = buf[8 : 8 + size]
                buf = buf[8 + size :]
                if stream_type == 1:
                    loop.call_soon_threadsafe(stdout.feed_data, chunk)
                elif stream_type == 2:
                    loop.call_soon_threadsafe(stderr.feed_data, chunk)
                # stream_type 0 (stdin) from daemon should not appear on read path
            try:
                data = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            buf += data
    finally:
        loop.call_soon_threadsafe(stdout.feed_eof)
        loop.call_soon_threadsafe(stderr.feed_eof)
        on_done()


def _container_env_for_docker_create(
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """
    MotetMCPProxy passes the worker's full ``os.environ`` into sidecars. That
    overrides image ``ENV``, including ``PATH`` — breaking images that prepend
    a venv (e.g. ``/app/.venv/bin`` for workspace-mcp). Omit host ``PATH`` so
    the OCI image's PATH wins.
    """
    return {k: v for k, v in (env or {}).items() if k != "PATH"}


def _build_binds(working_dir: Optional[str], container_wd: str) -> List[str]:
    if not working_dir or not str(working_dir).strip():
        return []
    host_abs = os.path.abspath(str(working_dir).strip())
    ok, err = worker_exec_cwd_allowed(host_abs)
    if not ok:
        raise RuntimeError(err or "MCP docker: working_dir not allowlisted")
    return [f"{host_abs}:{container_wd}:rw"]


async def start_mcp_stdio_docker(
    *,
    command: str,
    args: List[str],
    env: Dict[str, str],
    working_dir: Optional[str],
    exec_image: Optional[str],
    server_id: str,
    worker_id: Optional[str] = None,
) -> DockerMCPAsyncProcess:
    """
    Create and start a container with interactive attach for MCP stdio.
    """
    sock_path, sock_err = docker_socket_path()
    if sock_err:
        raise RuntimeError(sock_err)
    assert sock_path is not None
    if not os.path.exists(sock_path):
        raise RuntimeError(f"Docker unix socket not found at {sock_path!r}")

    image = (exec_image or "").strip() or _default_mcp_image()
    cmd = [command] + list(args)
    reader_limit = _stdio_stream_reader_limit(command, args)
    host_wd = bool(working_dir and str(working_dir).strip())
    container_wd = _container_workdir(host_wd)
    env_list = [f"{k}={v}" for k, v in _container_env_for_docker_create(env or {}).items()]
    binds = _build_binds(working_dir, container_wd)

    host_config: Dict[str, Any] = {
        "NetworkMode": (os.getenv("MOTET_MCP_DOCKER_NETWORK") or "bridge").strip() or "bridge",
        "AutoRemove": True,
    }
    extra_hosts_raw = (os.getenv("MOTET_MCP_DOCKER_EXTRA_HOSTS") or "").strip()
    if extra_hosts_raw:
        # Comma-separated "hostname:ip" (Docker accepts host-gateway as ip).
        hosts = [h.strip() for h in extra_hosts_raw.split(",") if h.strip()]
        if hosts:
            host_config["ExtraHosts"] = hosts
    if binds:
        host_config["Binds"] = binds
    rt = docker_engine_container_runtime(for_mcp=True)
    if rt:
        host_config["Runtime"] = rt

    create_body: Dict[str, Any] = {
        "Image": image,
        "Entrypoint": cmd,
        "Cmd": [],
        "Env": env_list,
        "Labels": mcp_docker_container_labels(worker_id, service_id=server_id),
        "WorkingDir": container_wd,
        "AttachStdin": True,
        "AttachStdout": True,
        "AttachStderr": True,
        "OpenStdin": True,
        "StdinOnce": False,
        "Tty": False,
        "HostConfig": host_config,
    }

    prefix = api_prefix()
    create_json = json.dumps(create_body).encode("utf-8")

    def _create() -> Tuple[int, bytes]:
        return docker_request(sock_path, "POST", f"{prefix}/containers/create", body=create_json)

    status, data = _create()
    if (
        status != http.client.CREATED
        and auto_pull_enabled()
        and create_failed_missing_image(status, data)
    ):
        pulled, pull_err = docker_pull_image(sock_path, prefix, image)
        if not pulled:
            raise RuntimeError(f"{daemon_error(status, data)} (auto-pull: {pull_err})")
        status, data = _create()
    if status != http.client.CREATED:
        raise RuntimeError(daemon_error(status, data))

    created = json.loads(data.decode("utf-8"))
    cid = created.get("Id")
    if not cid:
        raise RuntimeError(f"docker create missing Id: {created!r}")

    st_start, raw_start = docker_request(sock_path, "POST", f"{prefix}/containers/{quote(cid, safe='')}/start")
    if st_start not in (http.client.NO_CONTENT, http.client.OK):
        docker_request(sock_path, "DELETE", f"{prefix}/containers/{quote(cid, safe='')}?force=1")
        raise RuntimeError(daemon_error(st_start, raw_start))

    raw_sock, remainder = _attach_connect(sock_path, prefix, cid)
    raw_sock.settimeout(_ATTACH_IO_TIMEOUT_SECONDS)
    send_lock = threading.Lock()
    stdin_w = DockerStdinWriter(raw_sock, send_lock)
    stdout_r = asyncio.StreamReader(limit=reader_limit)
    stderr_r = asyncio.StreamReader(limit=reader_limit)
    loop = asyncio.get_running_loop()

    proc = DockerMCPAsyncProcess(
        stdin=stdin_w,
        stdout=stdout_r,
        stderr=stderr_r,
        raw_sock=raw_sock,
        container_id=cid,
        sock_path=sock_path,
        api_pfx=prefix,
    )

    reader_thread = threading.Thread(
        target=_mux_reader_loop,
        args=(raw_sock, remainder, stdout_r, stderr_r, loop, proc.mark_attach_closed),
        name=f"mcp-docker-mux-{server_id}",
        daemon=True,
    )
    reader_thread.start()
    proc.start_exit_monitor()

    logger.info(
        "mcp_stdio_docker_started",
        server_id=server_id,
        container_id=cid[:12],
        image=image,
    )
    return proc


__all__ = ["DockerMCPAsyncProcess", "DockerStdinWriter", "start_mcp_stdio_docker"]
