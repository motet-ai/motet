"""
Motet - Execution runner (backend dispatch)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-07

Description:
    Single entrypoint run_execution() dispatches to a backend from
    MOTET_EXEC_BACKEND (default subprocess). Docker and Kata-class backends
    use the Docker Engine API where configured.

Dependencies:
    - motet.core.execution.backends

Notes:
    - MOTET_EXEC_BACKEND: subprocess (default); docker; kata|kata-fc (Kata runtime via Engine API).
"""

from __future__ import annotations

import os
from .backends.docker import run_docker
from .backends.kata_docker import run_kata_docker
from .backends.subprocess import run_subprocess
from .models import ExecutionRequest, ExecutionResult


def run_execution(request: ExecutionRequest) -> ExecutionResult:
    """
    Run a worker-side execution request using the configured backend.

    Backends must return ExecutionResult with consistent semantics.
    """
    backend_id = (os.getenv("MOTET_EXEC_BACKEND") or "subprocess").strip().lower()

    if backend_id in ("subprocess", ""):
        return run_subprocess(request)

    if backend_id in ("docker", "container"):
        return run_docker(request)

    if backend_id == "kata-fc":
        return run_kata_docker(request, backend_label="kata-fc")

    if backend_id == "kata":
        return run_kata_docker(request, backend_label="kata")

    return ExecutionResult(
        exit_code=-1,
        backend=backend_id,
        error=f"unknown MOTET_EXEC_BACKEND {backend_id!r}",
    )


__all__ = ["run_execution"]
