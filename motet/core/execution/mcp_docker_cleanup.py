"""
Motet - MCP Docker container labels and orphan sweep (Phase 2)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Labels all Motet-started MCP Docker containers and sweeps stale containers
    so a manager crash or ``docker restart mcp-manager`` does not leave HTTP
    sidecars holding published ports (or stdio children) on the host.

Dependencies:
    - motet.core.execution.docker_client
    - motet.core.execution.mcp_backend

Usage:
    from motet.core.execution.mcp_docker_cleanup import (
        mcp_docker_container_labels,
        sweep_mcp_containers_for_worker,
        sweep_mcp_http_sidecars,
    )

Notes:
    - Container labels prefer the MCP/stream ``worker_id`` passed from
      ``MCPInstanceManager`` / ``MotetMCPProxy``. Manager ids such as
      ``mcp-local-default`` are stored as-is; sweep matches both the raw id
      and the ``cloud_``-prefixed form used by ``get_worker_id()``.
    - HTTP sidecars also carry ``motet.mcp.service_id``. Start reclaims
      leftovers by service id or published host port before bind.
"""

from __future__ import annotations

import http.client
import json
import os
import structlog
import urllib.parse
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote

from motet.core.execution.docker_client import api_prefix, docker_request, docker_socket_path

logger = structlog.get_logger(__name__)

MCP_DOCKER_LABEL = "motet.mcp"
MCP_DOCKER_LABEL_VALUE = "1"
MCP_WORKER_LABEL = "motet.worker_id"
MCP_SERVICE_LABEL = "motet.mcp.service_id"


def _label_filter_worker_ids(worker_id: str) -> List[str]:
    """
    Worker/manager ids that may appear on ``motet.worker_id``.

    Compose workers are often labeled ``cloud_worker1`` while the manager
    process labels sidecars with the explicit ``manager_id``
    (``mcp-local-default``). Startup sweep must match both.
    """
    w = (worker_id or "").strip()
    if not w:
        return []
    ids = [w]
    if not w.startswith("edge_") and not w.startswith("cloud_"):
        ids.append(f"cloud_{w}")
    return ids


def mcp_docker_container_labels(
    worker_id: Optional[str] = None,
    service_id: Optional[str] = None,
) -> Dict[str, str]:
    """Labels applied to MCP stdio and HTTP sidecar containers."""
    from motet.core.workers.worker_utils import get_worker_id

    explicit = (worker_id or "").strip()
    wid = explicit if explicit else get_worker_id()
    labels = {MCP_DOCKER_LABEL: MCP_DOCKER_LABEL_VALUE, MCP_WORKER_LABEL: wid}
    sid = (service_id or "").strip()
    if sid:
        labels[MCP_SERVICE_LABEL] = sid
    return labels


def _docker_ready() -> Optional[tuple[str, str]]:
    from motet.core.execution.mcp_backend import mcp_exec_uses_docker

    if not mcp_exec_uses_docker():
        return None
    sock_path, sock_err = docker_socket_path()
    if sock_err or not sock_path or not os.path.exists(sock_path):
        return None
    return sock_path, api_prefix()


def _list_motet_mcp_containers(sock_path: str, prefix: str) -> List[Dict[str, Any]]:
    filters_obj: Dict[str, List[str]] = {
        "label": [f"{MCP_DOCKER_LABEL}={MCP_DOCKER_LABEL_VALUE}"],
    }
    filters_q = urllib.parse.quote(json.dumps(filters_obj))
    status, raw = docker_request(
        sock_path,
        "GET",
        f"{prefix}/containers/json?all=1&filters={filters_q}",
    )
    if status != http.client.OK:
        logger.debug(
            "mcp_docker_sweep_list_failed",
            status=status,
            detail=(raw[:300] if isinstance(raw, bytes) else str(raw))[:300],
        )
        return []
    try:
        containers: List[Dict[str, Any]] = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning("mcp_docker_sweep_list_parse_failed", error=str(e))
        return []
    return containers


def _container_public_ports(container: Dict[str, Any]) -> Set[int]:
    ports: Set[int] = set()
    for entry in container.get("Ports") or []:
        if not isinstance(entry, dict):
            continue
        public = entry.get("PublicPort")
        if public is None:
            continue
        try:
            ports.add(int(public))
        except (TypeError, ValueError):
            continue
    return ports


def _remove_mcp_container(
    sock_path: str,
    prefix: str,
    container: Dict[str, Any],
    *,
    reason: str,
    **log_fields: Any,
) -> None:
    cid = container.get("Id")
    if not cid:
        return
    names = container.get("Names") or []
    logger.info(
        "mcp_docker_sweep_removing_orphan",
        container_id=cid[:12],
        names=names,
        reason=reason,
        **log_fields,
    )
    try:
        st_stop, _ = docker_request(
            sock_path,
            "POST",
            f"{prefix}/containers/{quote(cid, safe='')}/stop?t=3",
        )
        if st_stop not in (http.client.NO_CONTENT, http.client.OK, http.client.NOT_FOUND):
            logger.debug("mcp_docker_sweep_stop_status", status=st_stop, cid=cid[:12])
    except Exception as e:
        logger.debug("mcp_docker_sweep_stop_error", cid=cid[:12], error=str(e))
    try:
        st_rm, _ = docker_request(
            sock_path,
            "DELETE",
            f"{prefix}/containers/{quote(cid, safe='')}?force=1",
        )
        if st_rm not in (http.client.NO_CONTENT, http.client.OK, http.client.NOT_FOUND):
            logger.debug("mcp_docker_sweep_rm_status", status=st_rm, cid=cid[:12])
    except Exception as e:
        logger.debug("mcp_docker_sweep_rm_error", cid=cid[:12], error=str(e))


def sweep_mcp_containers_for_worker(worker_id: str) -> None:
    """
    Stop and remove MCP containers tagged for this worker or manager id.

    Called when the MCP instance manager starts with Docker backend so a prior
    crash or killed manager process does not leave containers running.
    """
    wanted = set(_label_filter_worker_ids(worker_id))
    if not wanted:
        return
    ready = _docker_ready()
    if ready is None:
        return
    sock_path, prefix = ready
    for container in _list_motet_mcp_containers(sock_path, prefix):
        labels = container.get("Labels") or {}
        labeled = (labels.get(MCP_WORKER_LABEL) or "").strip()
        if labeled not in wanted:
            continue
        _remove_mcp_container(
            sock_path,
            prefix,
            container,
            reason="worker_or_manager_id",
            worker_id=labeled,
            sweep_ids=sorted(wanted),
        )


def sweep_mcp_http_sidecars(
    *,
    service_id: Optional[str] = None,
    host_port: Optional[int] = None,
) -> int:
    """
    Remove Motet MCP containers that own this HTTP service or published port.

    Used before ``start_mcp_http_sidecar`` so a leftover sidecar from a previous
    manager process cannot steal the host port. Only containers labeled
    ``motet.mcp=1`` are removed.
    """
    sid = (service_id or "").strip()
    port: Optional[int] = None
    if host_port is not None:
        try:
            port = int(host_port)
        except (TypeError, ValueError):
            port = None
    if not sid and port is None:
        return 0
    ready = _docker_ready()
    if ready is None:
        return 0
    sock_path, prefix = ready
    removed = 0
    for container in _list_motet_mcp_containers(sock_path, prefix):
        labels = container.get("Labels") or {}
        match_service = bool(sid) and (labels.get(MCP_SERVICE_LABEL) or "").strip() == sid
        match_port = port is not None and port in _container_public_ports(container)
        if not (match_service or match_port):
            continue
        _remove_mcp_container(
            sock_path,
            prefix,
            container,
            reason="http_sidecar_reclaim",
            service_id=sid or None,
            host_port=port,
            matched_service=match_service,
            matched_port=match_port,
        )
        removed += 1
    return removed


__all__ = [
    "MCP_DOCKER_LABEL",
    "MCP_DOCKER_LABEL_VALUE",
    "MCP_SERVICE_LABEL",
    "MCP_WORKER_LABEL",
    "mcp_docker_container_labels",
    "sweep_mcp_containers_for_worker",
    "sweep_mcp_http_sidecars",
]
