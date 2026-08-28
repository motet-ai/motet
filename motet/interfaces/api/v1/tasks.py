"""
Motet - Live Tasks API

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Product live-task surface: list in-flight orchestration tasks,
    fetch a live summary, and request cooperative task cancel. Authz v1 is
    owning principal; ``admin`` / ``motet-admin`` may cancel by id when live
    meta is missing (runaway-tree backstop). This is not debug task-flow history.

Dependencies:
    - fastapi: REST API
    - motet.core.distributed.task_control: sticky cancel + live index
    - interfaces.api.shared.auth: principal authentication / ADMIN_ROLES

Usage:
    from motet.interfaces.api.v1.tasks import router
    app.include_router(router)

Notes:
    - Live index is ephemeral Redis (register on root start, TTL safety net)
    - Cancel writes sticky ``task:control:{task_id}`` and push wake
    - ``GET /live`` omits ``status=cancelled`` unless ``include_cancelled=true``
    - Part of URL standardization: ``/api/v1/tasks``
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ....core.types import Principal
from ..shared.auth import ADMIN_ROLES, get_current_principal
from ..shared.identity import get_principal_context

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


class TaskCancelRequest(BaseModel):
    """Optional body for operator task cancel."""

    reason: Optional[str] = Field(
        default=None,
        description="Optional operator reason for the cancel.",
        json_schema_extra={"example": "user requested stop"},
    )


def _require_task_id(task_id: str) -> str:
    tid = (task_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="task_id is required")
    return tid


@router.get(
    "/live",
    summary="List in-flight tasks",
    description=(
        "Returns orchestration tasks currently registered in the live index "
        "for the calling principal. Gone when the task leaves the live set."
    ),
    responses={
        200: {"description": "Live tasks for the caller"},
        401: {"description": "Unauthorized"},
    },
)
async def list_live_tasks(
    conversation_id: Optional[str] = Query(
        default=None,
        description="Optional conversation filter",
    ),
    include_cancelled: bool = Query(
        default=False,
        description="Include live-index rows marked cancelled (TTL linger).",
    ),
    principal: Principal = Depends(get_current_principal),
) -> Dict[str, Any]:
    """List in-flight tasks visible to the calling principal."""
    from motet.core.distributed.task_control import list_live_tasks as _list

    _motet_id, tenant_id, principal_id = get_principal_context(principal)
    tasks = _list(
        tenant_id=tenant_id,
        principal_id=principal_id,
        conversation_id=conversation_id,
        include_cancelled=include_cancelled,
    )
    return {"tasks": tasks, "count": len(tasks)}


@router.get(
    "/{task_id}",
    summary="Get a live task",
    description="Live summary if the task is still in the live index; 404 when gone.",
    responses={
        200: {"description": "Live task summary"},
        403: {"description": "Not the owning principal"},
        404: {"description": "Task not live"},
    },
)
async def get_live_task(
    task_id: str,
    principal: Principal = Depends(get_current_principal),
) -> Dict[str, Any]:
    """Return live task meta if still running / recently cancelled."""
    from motet.core.distributed.task_control import (
        get_live_task as _get,
        live_task_owned_by,
    )

    tid = _require_task_id(task_id)
    _motet_id, tenant_id, principal_id = get_principal_context(principal)
    meta = _get(tid, tenant_id=tenant_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Task not found in live index")
    if not live_task_owned_by(meta, principal_id=principal_id, tenant_id=tenant_id):
        raise HTTPException(status_code=403, detail="Not authorized to view this task")
    return meta


@router.post(
    "/{task_id}/cancel",
    summary="Cancel a live task",
    description=(
        "Request cooperative cancel for an orchestration task (sticky Redis "
        "control + push wake). Owning principal only (v1)."
    ),
    responses={
        200: {"description": "Cancel requested"},
        403: {"description": "Not the owning principal"},
        404: {"description": "Task not found in live index"},
    },
)
async def cancel_task(
    task_id: str,
    req: TaskCancelRequest = Body(default_factory=TaskCancelRequest),
    principal: Principal = Depends(get_current_principal),
) -> Dict[str, Any]:
    """Sticky cancel + wake for the given Motet task_id."""
    from motet.core.distributed.task_control import (
        get_live_task,
        live_task_owned_by,
        request_task_cancel,
    )

    tid = _require_task_id(task_id)
    _motet_id, tenant_id, principal_id = get_principal_context(principal)
    meta = get_live_task(tid, tenant_id=tenant_id)
    is_admin = bool(set(principal.roles or []) & ADMIN_ROLES)
    if not meta:
        # Ownership proof normally comes from the live index. Admins may still
        # cancel by id when register failed / TTL expired (runaway-tree backstop).
        if not is_admin:
            raise HTTPException(
                status_code=404,
                detail="Task not found in live index",
            )
        logger.info(
            "task_cancel_admin_without_live_meta",
            task_id=tid,
            principal_id=principal_id,
        )
    elif not live_task_owned_by(
        meta, principal_id=principal_id, tenant_id=tenant_id
    ) and not is_admin:
        raise HTTPException(status_code=403, detail="Not authorized to cancel this task")

    try:
        payload = request_task_cancel(
            tid,
            reason=req.reason,
            principal_id=principal_id,
            source="api" if meta else "api_admin_no_live_meta",
            tenant_id=(meta or {}).get("tenant_id") or tenant_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(
            "task_cancel_api_failed",
            task_id=tid,
            error=str(e),
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=f"Failed to cancel task: {e}") from e

    return {
        "task_id": tid,
        "status": "cancelled",
        "control": payload,
    }
