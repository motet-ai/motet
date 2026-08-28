"""
Motet - Canonical worker execution package

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Stable internal API for argv-style execution in the worker domain.
    Tools such as core.worker_exec and future skill runners build
    ExecutionRequest values and call run_execution() (per-call) or
    run_in_workspace.
"""

from __future__ import annotations

from .environment_manager import run_in_workspace, run_one_shot, run_stateful_in_workspace
from .models import ExecutionInputFile, ExecutionRequest, ExecutionResult
from .runner import run_execution

__all__ = [
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutionInputFile",
    "run_execution",
    "run_in_workspace",
    "run_one_shot",
    "run_stateful_in_workspace",
]
