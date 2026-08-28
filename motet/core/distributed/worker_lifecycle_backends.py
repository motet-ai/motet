"""
Motet - Worker Lifecycle Backends

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Pluggable backends for worker lifecycle actions (start/stop/restart). Enables
    the same lifecycle command to work when the lifecycle container shares a Docker
    daemon with agent workers (Docker backend) or when workers run on another host
    or PaaS (HTTP backend - see).

Dependencies:
    - subprocess: Docker CLI for DockerLifecycleBackend
    - motet.core.workers.worker_utils: extract_hostname_from_worker_id

Usage:
    from motet.core.distributed.worker_lifecycle_backends import (
        get_lifecycle_backend,
        WorkerLifecycleBackend,
        DockerLifecycleBackend,
    )
    backend = get_lifecycle_backend()
    result = backend.start_worker("cloud_worker1")

Notes:
    - Backend selected via MOTET_LIFECYCLE_BACKEND (default: docker).
    - Readiness state updates (STOPPED/STARTING/RESTARTING) are done by
      WorkerLifecycleService, not by backends.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Dict, Optional, Protocol, runtime_checkable

import structlog

logger = structlog.get_logger(__name__)

# Env keys for backend selection (ADR-0067)
ENV_LIFECYCLE_BACKEND = "MOTET_LIFECYCLE_BACKEND"
DEFAULT_BACKEND = "docker"


@runtime_checkable
class WorkerLifecycleBackend(Protocol):
    """Protocol for worker lifecycle backends. All backends return the same result shape."""

    def start_worker(self, worker_id: str) -> Dict[str, Any]:
        """Start a worker. Returns { success, method?, container?, error?, note? }."""
        ...

    def stop_worker(self, worker_id: str, timeout_seconds: int = 30) -> Dict[str, Any]:
        """Stop a worker. Returns { success, method?, container?, error?, note? }."""
        ...

    def restart_worker(self, worker_id: str) -> Dict[str, Any]:
        """Restart a worker. Returns { success, method?, container?, error? }."""
        ...


class DockerLifecycleBackend:
    """
    Lifecycle backend that uses the local Docker daemon (same host as agent workers).
    Resolves worker_id to container via Compose labels or fallback mapping.
    """

    def start_worker(self, worker_id: str) -> Dict[str, Any]:
        """Start a worker container if stopped. Does not update readiness state."""
        try:
            container_name = self._get_worker_container(worker_id)
            if not container_name:
                return {"success": False, "error": "Could not find worker container"}

            is_running = self._is_container_running(container_name)
            if is_running:
                return {
                    "success": True,
                    "method": "docker_start",
                    "container": container_name,
                    "note": "already_running",
                }

            result = subprocess.run(
                ["docker", "start", container_name],
                capture_output=True,
                text=True,
                timeout=60,
            )

            if result.returncode == 0:
                return {
                    "success": True,
                    "method": "docker_start",
                    "container": container_name,
                }
            return {"success": False, "error": result.stderr or result.stdout}

        except Exception as e:
            return {"success": False, "error": str(e)}

    def stop_worker(self, worker_id: str, timeout_seconds: int = 30) -> Dict[str, Any]:
        """Stop a worker container if running. Does not update readiness state."""
        try:
            container_name = self._get_worker_container(worker_id)
            if not container_name:
                return {"success": False, "error": "Could not find worker container"}

            is_running = self._is_container_running(container_name)
            if is_running is False:
                return {
                    "success": True,
                    "method": "docker_stop",
                    "container": container_name,
                    "note": "already_stopped",
                }

            result = subprocess.run(
                ["docker", "stop", "-t", str(timeout_seconds), container_name],
                capture_output=True,
                text=True,
                timeout=60,
            )

            if result.returncode == 0:
                return {
                    "success": True,
                    "method": "docker_stop",
                    "container": container_name,
                }
            return {"success": False, "error": result.stderr or result.stdout}

        except Exception as e:
            return {"success": False, "error": str(e)}

    def restart_worker(self, worker_id: str) -> Dict[str, Any]:
        """Restart a worker container. Does not update readiness state."""
        try:
            container_name = self._get_worker_container(worker_id)
            if not container_name:
                return {"success": False, "error": "Could not find worker container"}

            result = subprocess.run(
                ["docker", "restart", container_name],
                capture_output=True,
                text=True,
                timeout=60,
            )

            if result.returncode == 0:
                return {
                    "success": True,
                    "method": "docker_restart",
                    "container": container_name,
                }
            return {"success": False, "error": result.stderr or result.stdout}

        except Exception as e:
            return {"success": False, "error": str(e)}

    def _get_worker_container(self, worker_id: str) -> Optional[str]:
        """Get Docker container name for worker (Compose label or fallback)."""
        try:
            from ..workers.worker_utils import extract_hostname_from_worker_id

            hostname = extract_hostname_from_worker_id(worker_id) or worker_id
            # Try explicit env override first (e.g. EC2 with custom project/service names)
            if "worker1" in worker_id or "worker-1" in worker_id:
                explicit = os.environ.get("MOTET_LIFECYCLE_DOCKER_CONTAINER_WORKER1")
                if explicit:
                    return explicit
            if "worker2" in worker_id or "worker-2" in worker_id:
                explicit = os.environ.get("MOTET_LIFECYCLE_DOCKER_CONTAINER_WORKER2")
                if explicit:
                    return explicit

            # Resolve by Compose label: try both "worker-1"/"worker-2" (distributed.yml) and "worker1"/"worker2" (EC2 install)
            candidate_services: list[str] = []
            if hostname.startswith("worker") and hostname[len("worker") :].isdigit():
                suffix = hostname[len("worker") :]
                candidate_services = [f"worker-{suffix}", f"worker{suffix}"]
            elif hostname.startswith("worker-"):
                candidate_services = [hostname]

            for service_name in candidate_services:
                result = subprocess.run(
                    [
                        "docker",
                        "ps",
                        "-a",
                        "--filter",
                        f"label=com.docker.compose.service={service_name}",
                        "--format",
                        "{{.Names}}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0:
                    container_names = [
                        name.strip()
                        for name in result.stdout.splitlines()
                        if name.strip()
                    ]
                    if container_names:
                        return container_names[0]

            # Fallback: use compose project from env (e.g. EC2: imf_ec2-worker1-1) or default motet_dev
            project = os.environ.get("MOTET_LIFECYCLE_DOCKER_COMPOSE_PROJECT", "motet_dev")
            if "worker-1" in worker_id or "worker1" in worker_id:
                # Try EC2-style (worker1) then distributed-style (worker-1)
                for suffix in ("worker1", "worker-1"):
                    name = f"{project}-{suffix}-1"
                    if self._is_container_running(name) is not None:
                        return name
                return f"{project}-worker-1-1"
            if "worker-2" in worker_id or "worker2" in worker_id:
                for suffix in ("worker2", "worker-2"):
                    name = f"{project}-{suffix}-1"
                    if self._is_container_running(name) is not None:
                        return name
                return f"{project}-worker-2-1"
        except Exception:
            pass  # best-effort Docker container name mapping
        return None

    def _is_container_running(self, container_name: str) -> Optional[bool]:
        """Return True if container is running, False if stopped, None on error."""
        try:
            result = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return None
            return result.stdout.strip().lower() == "true"
        except Exception:
            return None


def get_lifecycle_backend() -> WorkerLifecycleBackend:
    """
    Return the configured lifecycle backend (ADR-0067).

    - MOTET_LIFECYCLE_BACKEND=docker (default): Docker backend, same host.
    - MOTET_LIFECYCLE_BACKEND=http: HTTP backend (Phase 2); requires
      MOTET_LIFECYCLE_HTTP_BASE_URL.

    Returns:
        WorkerLifecycleBackend implementation.
    """
    backend_name = (
        os.getenv(ENV_LIFECYCLE_BACKEND, DEFAULT_BACKEND).strip().lower()
        or DEFAULT_BACKEND
    )
    if backend_name == "http":
        try:
            from .worker_lifecycle_backends_http import HttpLifecycleBackend

            base_url = os.getenv("MOTET_LIFECYCLE_HTTP_BASE_URL", "").strip()
            if not base_url:
                logger.warning(
                    "lifecycle_backend_http_missing_url",
                    message="MOTET_LIFECYCLE_HTTP_BASE_URL is required for http backend; falling back to docker",
                )
                return DockerLifecycleBackend()
            timeout = int(os.getenv("MOTET_LIFECYCLE_HTTP_TIMEOUT", "60"))
            return HttpLifecycleBackend(base_url=base_url, timeout_seconds=timeout)
        except ImportError:
            logger.warning(
                "lifecycle_backend_http_not_implemented",
                message="HttpLifecycleBackend not found; falling back to docker",
            )
            return DockerLifecycleBackend()
    return DockerLifecycleBackend()
