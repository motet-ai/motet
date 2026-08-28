"""
Motet - MCP HTTP server subprocess as Docker sidecar (Phase 2)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Runs ``start_server`` HTTP MCP binaries in a detached container with the
    MCP port published to the Docker host (default bind ``0.0.0.0``) so a Celery
    worker in another container can reach the sidecar via ``host.docker.internal``
    or ``MOTET_MCP_HTTP_CLIENT_HOST`` (see ``HTTPMCPTransport``).

Dependencies:
    - motet.core.execution.docker_client

Notes:
    - ``AutoRemove`` is false so ``terminate()`` can stop/delete reliably; containers are labeled for sweep.
    - Before bind, leftover Motet HTTP sidecars for the same ``service_id`` or
      published host port are removed so a manager restart cannot leave
      ``Failed to start http transport`` (port already allocated).
    - ``MOTET_MCP_HTTP_PORT_BIND_HOST`` (default ``0.0.0.0``) controls Docker port-publish ``HostIp``;
      use ``127.0.0.1`` only for host-local MCP (worker not in Docker).
    - ``Entrypoint`` is set to the MCP argv (``Cmd`` empty) so worker images with ``ENTRYPOINT`` still run the server directly.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import structlog

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
from motet.core.execution.mcp_docker_cleanup import (
    mcp_docker_container_labels,
    sweep_mcp_http_sidecars,
)

logger = structlog.get_logger(__name__)


def _default_mcp_image() -> str:
    return (
        os.getenv("MOTET_MCP_DOCKER_IMAGE")
        or os.getenv("MOTET_WORKER_EXEC_DOCKER_IMAGE")
        or "node:20-bookworm-slim"
    ).strip()


def _http_sidecar_port_bind_host_ip() -> str:
    """
    Docker ``HostIp`` for publishing the MCP HTTP port to the host.

    ``0.0.0.0`` allows containers that use ``host.docker.internal`` (or the
    bridge gateway) to reach the mapped port. ``127.0.0.1`` limits access to the
    host loopback only (breaks workers running inside Docker).
    """
    raw = (os.getenv("MOTET_MCP_HTTP_PORT_BIND_HOST") or "0.0.0.0").strip()
    return raw if raw else "0.0.0.0"


class DockerSidecarProcess:
    """Minimal asyncio.subprocess.Process stand-in for HTTP MCP sidecars."""

    def __init__(
        self,
        sock_path: str,
        prefix: str,
        container_id: str,
        *,
        host_port: int,
        container_port: int,
    ) -> None:
        self._sock_path = sock_path
        self._prefix = prefix
        self._container_id = container_id
        self.host_port = host_port
        self.container_port = container_port
        self.returncode: Optional[int] = None
        self.pid = 1
        self.stdout: Optional[asyncio.StreamReader] = None
        self.stderr: Optional[asyncio.StreamReader] = None

    def terminate(self) -> None:
        try:
            docker_request(
                self._sock_path,
                "POST",
                f"{self._prefix}/containers/{quote(self._container_id, safe='')}/stop?t=5",
            )
        except Exception as e:
            logger.debug("mcp_http_sidecar_stop", cid=self._container_id[:12], error=str(e))
        try:
            docker_request(
                self._sock_path,
                "DELETE",
                f"{self._prefix}/containers/{quote(self._container_id, safe='')}?force=1",
            )
        except Exception:
            pass

    def kill(self) -> None:
        """Compatibility shim for asyncio.subprocess.Process API."""
        self.terminate()

    async def wait(self) -> int:
        def _wait() -> int:
            st, body = docker_request(
                self._sock_path,
                "POST",
                f"{self._prefix}/containers/{quote(self._container_id, safe='')}/wait",
            )
            if st == http.client.OK:
                try:
                    payload = json.loads(body.decode("utf-8"))
                    return int(payload.get("StatusCode", 0))
                except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                    return -1
            # Container already removed after terminate/delete — treat as exited.
            if st == http.client.NOT_FOUND:
                return 0
            return -1

        loop = asyncio.get_running_loop()
        code = await loop.run_in_executor(None, _wait)
        self.returncode = code
        return code


async def start_mcp_http_sidecar(
    *,
    service_id: str,
    command: str,
    args: List[str],
    env: dict,
    port: int,
    exec_image: Optional[str],
    worker_id: Optional[str] = None,
) -> DockerSidecarProcess:
    sock_path, sock_err = docker_socket_path()
    if sock_err:
        raise RuntimeError(sock_err)
    assert sock_path is not None
    if not os.path.exists(sock_path):
        raise RuntimeError(f"Docker unix socket not found at {sock_path!r}")

    image = (exec_image or "").strip() or _default_mcp_image()
    cmd = [command] + list(args)
    from motet.core.tools.mcp_motet.proxy.mcp_docker_stdio import (
        _container_env_for_docker_create,
    )

    env_list = [f"{k}={v}" for k, v in _container_env_for_docker_create(env).items()]
    container_port = int(port)
    host_port = container_port
    # Avoid host-port collisions across workers (e.g. worker1=3301, worker2=3302).
    # Worker ids are typically cloud_workerN / workerN; non-numeric ids keep base port.
    m = re.search(r"(\d+)$", (worker_id or "").strip())
    if m:
        worker_index = max(1, int(m.group(1)))
        host_port = container_port + (worker_index - 1)
    container_port_s = str(container_port)
    host_port_s = str(host_port)
    exposed = f"{container_port_s}/tcp"

    http_host_cfg: Dict[str, Any] = {
        "PortBindings": {
            exposed: [{"HostIp": _http_sidecar_port_bind_host_ip(), "HostPort": host_port_s}],
        },
        "AutoRemove": False,
        "NetworkMode": (os.getenv("MOTET_MCP_DOCKER_NETWORK") or "bridge").strip() or "bridge",
    }
    extra_hosts_raw = (os.getenv("MOTET_MCP_DOCKER_EXTRA_HOSTS") or "").strip()
    if extra_hosts_raw:
        hosts = [h.strip() for h in extra_hosts_raw.split(",") if h.strip()]
        if hosts:
            http_host_cfg["ExtraHosts"] = hosts
    rt = docker_engine_container_runtime(for_mcp=True)
    if rt:
        http_host_cfg["Runtime"] = rt

    create_body = {
        "Image": image,
        "Entrypoint": cmd,
        "Cmd": [],
        "Env": env_list,
        "Labels": mcp_docker_container_labels(worker_id, service_id=service_id),
        "ExposedPorts": {exposed: {}},
        "HostConfig": http_host_cfg,
    }

    prefix = api_prefix()
    create_json = json.dumps(create_body).encode("utf-8")

    def _create() -> Tuple[int, bytes]:
        return docker_request(sock_path, "POST", f"{prefix}/containers/create", body=create_json)

    def _start_created(cid: str) -> Tuple[int, bytes]:
        return docker_request(
            sock_path,
            "POST",
            f"{prefix}/containers/{quote(cid, safe='')}/start",
        )

    # Leftover sidecars survive manager death (AutoRemove false). Reclaim the
    # service identity and the host port before bind so start is not a 30s
    # MotetMCPClient timeout on a Redis ``failed`` row.
    sweep_mcp_http_sidecars(service_id=service_id, host_port=host_port)

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

    st_start, raw_start = _start_created(cid)
    if st_start not in (http.client.NO_CONTENT, http.client.OK):
        start_err = daemon_error(st_start, raw_start)
        docker_request(sock_path, "DELETE", f"{prefix}/containers/{quote(cid, safe='')}?force=1")
        if "port is already allocated" in start_err.lower() or "address already in use" in start_err.lower():
            logger.warning(
                "mcp_http_sidecar_port_conflict_retry",
                service_id=service_id,
                host_port=host_port,
                error=start_err[:300],
            )
            sweep_mcp_http_sidecars(service_id=service_id, host_port=host_port)
            status, data = _create()
            if status != http.client.CREATED:
                raise RuntimeError(daemon_error(status, data))
            created = json.loads(data.decode("utf-8"))
            cid = created.get("Id")
            if not cid:
                raise RuntimeError(f"docker create missing Id: {created!r}")
            st_start, raw_start = _start_created(cid)
            if st_start not in (http.client.NO_CONTENT, http.client.OK):
                docker_request(sock_path, "DELETE", f"{prefix}/containers/{quote(cid, safe='')}?force=1")
                raise RuntimeError(daemon_error(st_start, raw_start))
        else:
            raise RuntimeError(start_err)

    logger.info(
        "mcp_http_sidecar_started",
        service_id=service_id,
        container_id=cid[:12],
        container_port=container_port,
        host_port=host_port,
        image=image,
        worker_id=worker_id,
    )
    return DockerSidecarProcess(
        sock_path,
        prefix,
        cid,
        host_port=host_port,
        container_port=container_port,
    )


__all__ = ["DockerSidecarProcess", "start_mcp_http_sidecar"]
