"""
Motet - Canonical execution protocol models

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Pydantic models for worker-side code execution (ExecutionRequest /
    ExecutionResult). Internal platform contract; backends (subprocess, container,
    microVM) implement the same shape. Session-backed execution may also carry
    small input files that the backend materializes before invoking argv.

Dependencies:
    - pydantic: request/result validation and staged input-file modeling

Usage:
    from motet.core.execution import (
        ExecutionInputFile,
        ExecutionRequest,
        ExecutionResult,
        run_execution,
    )

    result = run_execution(
        ExecutionRequest(argv=[\"python\", \"-c\", \"print(1)\"], cwd=\"/tmp\")
    )

    request = ExecutionRequest(
        argv=[\"python3\", \"/motet/_cold_handle_once.py\"],
        cwd=\"/scratch\",
        input_files=[
            ExecutionInputFile(
                path=\"/motet/_cold_handle_once.py\",
                content=b\"print('shim')\\n\",
            )
        ],
    )

Notes:
    - Request/result shape is the worker execution protocol; backends implement the same models.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class ExecutionInputFile(BaseModel):
    """A file the backend should materialize before running ``argv``."""

    path: str = Field(
        ...,
        description="Absolute destination path inside the execution domain",
    )
    content: bytes = Field(
        ...,
        description="Exact bytes written to ``path`` before argv starts",
    )
    mode: int = Field(
        default=0o600,
        ge=0,
        le=0o777,
        description="POSIX mode bits to apply when the file is written",
    )


class ExecutionRequest(BaseModel):
    """Worker-side run request (argv, no shell)."""

    argv: List[str] = Field(
        ...,
        description="Executable and arguments only (no shell invocation)",
    )
    cwd: str = Field(
        ...,
        description="Working directory inside the worker execution domain",
    )
    stdin: Optional[str] = Field(
        default=None,
        description="Optional UTF-8 text passed to the process stdin",
    )
    timeout_seconds: Optional[int] = Field(
        default=None,
        ge=1,
        description="Hard timeout in seconds; None uses backend default",
    )
    max_output_bytes: int = Field(
        default=1_048_576,
        ge=1024,
        description="Max combined stdout+stderr capture before truncation",
    )
    tenant_id: Optional[str] = Field(default=None, description="Tenant for quotas / audit")
    correlation_id: Optional[str] = Field(default=None, description="Trace / command correlation")
    bundle_id: Optional[str] = Field(default=None, description="Bundle context when applicable")
    bundle_version: Optional[str] = Field(default=None, description="Bundle version when applicable")
    oci_image_ref: Optional[str] = Field(
        default=None,
        description="Pinned image ref@digest when backend uses OCI (Phase 3+)",
    )
    exec_artifact_digest: Optional[str] = Field(
        default=None,
        description="Optional bundle exec artifact digest (Phase 3+)",
    )
    network: Literal["none", "restricted", "inherit"] = Field(
        default="inherit",
        description="Policy hint; subprocess backend may only support inherit",
    )
    input_files: List[ExecutionInputFile] = Field(
        default_factory=list,
        description=(
            "Optional files to materialize before argv runs. Workspace containers "
            "use this for runner scripts and one-shot invocation shims."
        ),
    )


class ExecutionResult(BaseModel):
    """Normalized outcome of a worker execution run."""

    exit_code: int = Field(..., description="Process exit code, or -1 if not started")
    stdout: str = Field(default="", description="Captured stdout (possibly truncated)")
    stderr: str = Field(default="", description="Captured stderr (possibly truncated)")
    timed_out: bool = Field(default=False, description="True if killed by timeout")
    canceled: bool = Field(default=False, description="Reserved for async cancellation")
    stdout_truncated: bool = Field(default=False, description="stdout exceeded capture policy")
    stderr_truncated: bool = Field(default=False, description="stderr exceeded capture policy")
    backend: str = Field(
        default="subprocess",
        description="Backend id (subprocess, docker, kata, …)",
    )
    backend_ref: Optional[str] = Field(
        default=None,
        description="Opaque id for support (container id prefix, job name, …)",
    )
    oci_image_ref: Optional[str] = Field(
        default=None,
        description="OCI image reference passed to the engine (docker / kata backends only)",
    )
    engine_runtime: Optional[str] = Field(
        default=None,
        description="Docker Engine HostConfig.Runtime when set (e.g. io.containerd.kata.v2)",
    )
    error: Optional[str] = Field(
        default=None,
        description="Scheduling / validation error; no process ran if set",
    )


__all__ = ["ExecutionInputFile", "ExecutionRequest", "ExecutionResult"]
