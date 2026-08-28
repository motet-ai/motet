"""
Motet - Workflow Executor Lifecycle Mixin (compatibility shim)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Backward-compatible combined mixin. Prefer importing
    ``WorkflowSuspendMixin`` / ``WorkflowResumeMixin`` directly; the executor
    facade composes those two modules.

Dependencies:
    - executor_suspend / executor_resume: split lifecycle mixins

Usage:
    from motet.core.workflow.executor_lifecycle import (
        WorkflowLifecycleMixin,  # compat
    )

Notes:
    - Implementation lives in executor_suspend.py and executor_resume.py.
"""

from __future__ import annotations

from .executor_resume import WorkflowResumeMixin
from .executor_suspend import WorkflowSuspendMixin

__all__ = [
    "WorkflowLifecycleMixin",
    "WorkflowSuspendMixin",
    "WorkflowResumeMixin",
]


class WorkflowLifecycleMixin(WorkflowSuspendMixin, WorkflowResumeMixin):
    """Combined suspend + resume mixin (compatibility only)."""
