"""
Motet - Worker Lifecycle Commands

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Distributed command for worker lifecycle actions (start/stop/restart) executed
    on the dedicated cloud_lifecycle_management worker. This isolates platform
    execution (Docker or HTTP webhook) from the public HTTP interface
    while keeping lifecycle actions within the distributed command framework.

Dependencies:
    - structlog: Structured logging for lifecycle actions
    - motet.core.distributed.worker_lifecycle: WorkerLifecycleService (pluggable backend)
    - motet.core.workers.worker_utils: Canonical worker ID resolution
    - motet.core.commands.decorator: Distributed command wrapper
    - motet.core.commands.command_data_classes: BaseCommandData model

Usage:
    from motet.core.commands.builtin.worker_lifecycle import (
        worker_lifecycle, WorkerLifecycleData, WorkerLifecycleAction
    )

    cmd = worker_lifecycle(
        task_id="task-123",
        conversation_id="",
        data=WorkerLifecycleData(
            worker_id="cloud_worker1",
            action=WorkerLifecycleAction.RESTART,
            requested_by="admin-user"
        )
    )

Notes:
    - Intended to run on the cloud_lifecycle_management worker only.
    - WorkerLifecycleService uses Docker or HTTP backend from MOTET_LIFECYCLE_BACKEND.
"""

from __future__ import annotations

import json
from typing import Any, Dict

import structlog
from motet import motet
from motet.core.commands.capabilities import WorkerCapability
from motet.core.commands.command_data_classes import (
    WorkerLifecycleAction,
    WorkerLifecycleData,
)

logger = structlog.get_logger(__name__)


def _normalize_lifecycle_result(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            logger.warning(
                "worker_lifecycle_action_unparseable_result",
                result_preview=result[:500],
            )
            return {"success": False, "error": result}
    return {"success": False, "error": f"Unexpected result type: {type(result).__name__}"}


@motet.command(
    description="Run a worker lifecycle management action (drain, warmup, status) on the management worker.",
    timeout_seconds=120,
    required_capabilities=[WorkerCapability.WORKER_LIFECYCLE_MANAGEMENT],
)
def worker_lifecycle(
    data: WorkerLifecycleData,
) -> Dict[str, Any]:
    """
    Execute worker lifecycle action on the management worker.

    Args:
        data: WorkerLifecycleData with worker ID and action.

    Returns:
        Dict containing lifecycle action results.
    """
    try:
        from motet.core.distributed.worker_lifecycle import get_lifecycle_service

        from motet.core.workers.worker_utils import get_worker_id, get_lifecycle_worker_id

        lifecycle_worker_id = get_lifecycle_worker_id()
        current_worker_id = get_worker_id()
        if current_worker_id != lifecycle_worker_id:
            logger.error(
                "worker_lifecycle_wrong_worker",
                worker_id=data.worker_id,
                action=data.action.value,
                current_worker_id=current_worker_id,
                expected_worker_id=lifecycle_worker_id,
            )
            raise RuntimeError(
                "Worker lifecycle commands must run on the lifecycle management worker."
            )

        logger.info(
            "worker_lifecycle_action_requested",
            worker_id=data.worker_id,
            action=data.action.value,
            requested_by=data.requested_by,
        )

        lifecycle_service = get_lifecycle_service()
        if data.action == WorkerLifecycleAction.START:
            result = lifecycle_service.start_worker(data.worker_id)
        elif data.action == WorkerLifecycleAction.STOP:
            result = lifecycle_service.stop_worker(
                data.worker_id,
                timeout_seconds=data.timeout_seconds,
            )
        elif data.action == WorkerLifecycleAction.RESTART:
            result = lifecycle_service.restart_worker(data.worker_id)
        else:
            raise ValueError(f"Unsupported worker lifecycle action: {data.action}")

        normalized = _normalize_lifecycle_result(result)
        if not normalized.get("success", False):
            error_message = normalized.get("error", "Worker lifecycle action failed")
            logger.error(
                "worker_lifecycle_action_failed",
                worker_id=data.worker_id,
                action=data.action.value,
                error=error_message,
            )
            raise RuntimeError(error_message)

        logger.info(
            "worker_lifecycle_action_completed",
            worker_id=data.worker_id,
            action=data.action.value,
        )

        return {
            "worker_id": data.worker_id,
            "action": data.action.value,
            "result": normalized,
        }
    except Exception as exc:
        logger.error(
            "worker_lifecycle_action_exception",
            worker_id=data.worker_id,
            action=data.action.value,
            error=str(exc),
            exc_info=True,
        )
        raise RuntimeError(f"Worker lifecycle command failed: {exc}") from exc
