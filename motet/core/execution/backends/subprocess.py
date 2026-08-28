"""
Motet - Subprocess execution backend (dev / Phase 1 default)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-05

Description:
    Runs ExecutionRequest via subprocess.run(shell=False) in the current worker
    environment. Not the multi-tenant production default; replaced by container
    / microVM backends in later phases.

Dependencies:
    - subprocess, os

Notes:
    - Respects MOTET_WORKER_EXEC_CWD_ALLOWLIST when set (comma-separated paths).
    - After allowlist validation, creates cwd with mode 0o700 if missing (containers, local dev without image RUN).
    - Recommended prefix in Docker: /var/motet/worker-exec (mode 0700; also created in worker/API images).
"""

from __future__ import annotations

import os
import subprocess

from ..capture import truncate_output_pair
from ..cwd_allowlist import worker_exec_cwd_allowed
from ..models import ExecutionRequest, ExecutionResult


def run_subprocess(request: ExecutionRequest) -> ExecutionResult:
    """Execute request in a disposable subprocess; cwd allowlist required."""
    cwd_abs = os.path.abspath(request.cwd.strip())
    ok_cwd, deny = worker_exec_cwd_allowed(cwd_abs)
    if not ok_cwd:
        return ExecutionResult(
            exit_code=-1,
            backend="subprocess",
            error=deny,
        )

    if request.network not in ("inherit",):
        return ExecutionResult(
            exit_code=-1,
            backend="subprocess",
            error=f"subprocess backend does not support network policy {request.network!r}",
        )

    try:
        os.makedirs(cwd_abs, mode=0o700, exist_ok=True)
    except OSError as e:
        return ExecutionResult(
            exit_code=-1,
            backend="subprocess",
            error=f"cannot create cwd {cwd_abs!r}: {e}",
        )

    timeout_s = (
        request.timeout_seconds
        if request.timeout_seconds is not None
        else int(os.getenv("MOTET_WORKER_EXEC_DEFAULT_TIMEOUT", "120"))
    )

    try:
        proc = subprocess.run(
            request.argv,
            cwd=cwd_abs,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            shell=False,
            check=False,
            stdin=subprocess.PIPE if request.stdin is not None else None,
            input=request.stdin,
        )
    except subprocess.TimeoutExpired:
        return ExecutionResult(
            exit_code=-1,
            timed_out=True,
            backend="subprocess",
        )
    except FileNotFoundError as e:
        return ExecutionResult(
            exit_code=-1,
            backend="subprocess",
            error=str(e),
        )
    except Exception as e:
        return ExecutionResult(
            exit_code=-1,
            backend="subprocess",
            error=str(e),
        )

    out = proc.stdout or ""
    err = proc.stderr or ""
    out, err, otrunc, etrunc = truncate_output_pair(out, err, request.max_output_bytes)

    return ExecutionResult(
        exit_code=int(proc.returncode),
        stdout=out,
        stderr=err,
        timed_out=False,
        stdout_truncated=otrunc,
        stderr_truncated=etrunc,
        backend="subprocess",
    )


__all__ = ["run_subprocess"]
