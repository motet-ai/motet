"""
Motet - Kata / Firecracker class execution via Docker Engine (Phase 4)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-07

Description:
    One-shot ``ExecutionRequest`` runs using the same Docker Engine HTTP API as
    ``run_docker``, but sets ``HostConfig.Runtime`` to the Kata containerd shim
    (default ``io.containerd.kata.v2``), which typically uses Firecracker as the
    hypervisor on Linux + KVM nodes.

Dependencies:
    - motet.core.execution.backends.docker (run_docker)

Usage:
    Set ``MOTET_EXEC_BACKEND=kata-fc`` (or ``kata``) and
    ``MOTET_KATA_DOCKER_RUNTIME`` if your daemon uses a non-default runtime name.

Notes:
    - Requires Docker (or compatible) daemon with Kata runtime registered; see
      upstream Kata + Docker / containerd docs. Not available on macOS host
      without a Linux VM providing the socket.
    - This is integration, not a standalone Firecracker API client.
"""

from __future__ import annotations

import os

from ..models import ExecutionRequest, ExecutionResult
from .docker import run_docker


def run_kata_docker(request: ExecutionRequest, *, backend_label: str = "kata-fc") -> ExecutionResult:
    """Run argv in a disposable container using the Kata Docker Engine runtime."""
    rt = (os.getenv("MOTET_KATA_DOCKER_RUNTIME") or "io.containerd.kata.v2").strip()
    if not rt:
        return ExecutionResult(
            exit_code=-1,
            backend=backend_label,
            error=(
                "MOTET_KATA_DOCKER_RUNTIME is empty; set the Docker Engine runtime id "
                "(e.g. io.containerd.kata.v2) for Kata + Firecracker-backed workloads"
            ),
        )
    return run_docker(request, backend_label=backend_label, container_runtime=rt)


__all__ = ["run_kata_docker"]
