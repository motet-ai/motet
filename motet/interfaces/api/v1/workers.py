"""
Motet - Workers API

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Worker management API for the Motet distributed framework.
    Provides REST API endpoints for monitoring, managing, and terminating workers.
    Includes manager status reporting (MCP and Local Inference managers).
    Readiness rows include each worker's Motet product version.

Dependencies:
    - fastapi: Web framework for REST API
    - motet.core.distributed: Worker management and monitoring
    - interfaces.api.shared.auth: Principal authentication and role checks

Usage:
    from motet.interfaces.api.v1.workers import router
    
    # Include in FastAPI app
    app.include_router(router)

Notes:
    - Provides worker readiness, health, and termination endpoints
    - Integrates with worker readiness and termination services
    - Part of Phase 2: API Organization and URL Standardization
"""

import asyncio
import json
import os
from datetime import datetime
from typing import Dict, List, Optional, Any
from fastapi import APIRouter, HTTPException, Header, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import structlog
import time

from ..shared.auth import get_current_principal, require_admin_principal
from ....core.types import Principal

logger = structlog.get_logger(__name__)

# Note: Authentication not required for readiness/health checks (monitoring)
# but required for termination operations

router = APIRouter(prefix="/api/v1/workers", tags=["workers"])

WORKER_LIFECYCLE_TARGET = os.getenv(
    "MOTET_WORKER_LIFECYCLE_WORKER_ID",
    "cloud_lifecycle_management",
)
_LIFECYCLE_ADMIN_DETAIL = "Admin role required for worker lifecycle actions"


def _normalize_lifecycle_result(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass  # non-critical json parse; fall through to HTTPException
    raise HTTPException(
        status_code=500,
        detail="Worker lifecycle command returned unexpected response type",
    )


def _lifecycle_error_message(normalized: Dict[str, Any], fallback: str) -> str:
    error = normalized.get("error")
    if isinstance(error, dict):
        return error.get("message", fallback)
    if isinstance(error, str):
        return error or fallback
    return fallback


class WorkerTerminateRequest(BaseModel):
    """Request to terminate a worker."""
    reason: str = Field(
        default="manual_request",
        description="Reason for termination",
        json_schema_extra={"example": "manual_request"}
    )
    method: str = Field(
        default="graceful_shutdown",
        description="Termination method (graceful_shutdown, immediate, revoke_tasks)",
        json_schema_extra={"example": "graceful_shutdown"}
    )
    timeout_seconds: int = Field(
        default=60,
        description="Timeout in seconds for graceful shutdown",
        json_schema_extra={"example": 60}
    )


class WorkerHealthResponse(BaseModel):
    """Worker health status response."""
    healthy: bool = Field(..., description="Whether the worker is healthy", json_schema_extra={"example": True})
    last_heartbeat: float = Field(..., description="Timestamp of last heartbeat", json_schema_extra={"example": 1700000000.0})
    heartbeat_age_seconds: float = Field(..., description="Age of last heartbeat in seconds", json_schema_extra={"example": 5.2})
    success_rate: float = Field(..., description="Task success rate (0.0-1.0)", json_schema_extra={"example": 0.98})
    active_tasks: int = Field(..., description="Number of active tasks", json_schema_extra={"example": 3})
    stuck_tasks: int = Field(..., description="Number of stuck tasks", json_schema_extra={"example": 0})
    memory_usage_mb: float = Field(..., description="Memory usage in MB", json_schema_extra={"example": 512.5})
    cpu_usage_percent: float = Field(..., description="CPU usage percentage", json_schema_extra={"example": 45.2})
    error_count_last_hour: int = Field(..., description="Error count in last hour", json_schema_extra={"example": 0})
    uptime_seconds: float = Field(..., description="Worker uptime in seconds", json_schema_extra={"example": 3600.0})
    termination_reason: Optional[str] = Field(None, description="Reason for termination if unhealthy", json_schema_extra={"example": None})


class WorkerDetailResponse(BaseModel):
    """Detailed worker information."""
    state: str = Field(..., description="Worker state (ready, busy, etc.)", json_schema_extra={"example": "ready"})
    capabilities: List[str] = Field(..., description="Worker capabilities", json_schema_extra={"example": ["model_inference", "tool_execution"]})
    active_commands: int = Field(..., description="Number of active commands", json_schema_extra={"example": 2})
    max_concurrency: int = Field(..., description="Maximum concurrency", json_schema_extra={"example": 10})
    utilization_percent: float = Field(..., description="Worker utilization percentage", json_schema_extra={"example": 20.0})
    tool_count: int = Field(..., description="Number of loaded tools", json_schema_extra={"example": 15})
    mcp_tool_count: int = Field(..., description="Number of MCP tools", json_schema_extra={"example": 5})
    tools: List[str] = Field(..., description="List of tool names", json_schema_extra={"example": ["search", "calculator", "weather"]})
    instance_managers: Dict[str, Any] = Field(
        default_factory=dict,
        description="Instance manager telemetry keyed by manager type (e.g. mcp, local_inference)",
        json_schema_extra={"example": {"mcp": {"status": "running", "instances_total": 5}}},
    )
    warmup_completed: bool = Field(..., description="Whether warmup is completed", json_schema_extra={"example": True})
    warmup_duration_ms: float = Field(..., description="Warmup duration in milliseconds", json_schema_extra={"example": 1234.5})
    pool_type: str = Field(..., description="Worker pool type (fork, threads, eventlet, gevent)", json_schema_extra={"example": "fork"})
    last_heartbeat: float = Field(..., description="Timestamp of last heartbeat", json_schema_extra={"example": 1700000000.0})
    heartbeat_age_seconds: Optional[float] = Field(None, description="Age of last heartbeat in seconds", json_schema_extra={"example": 5.2})
    memory_usage_mb: float = Field(..., description="Memory usage in MB", json_schema_extra={"example": 512.5})
    cpu_usage_percent: float = Field(..., description="CPU usage percentage", json_schema_extra={"example": 45.2})
    uptime_seconds: float = Field(..., description="Worker uptime in seconds", json_schema_extra={"example": 3600.0})
    is_healthy: bool = Field(..., description="Whether the worker is healthy", json_schema_extra={"example": True})
    motet_version: Optional[str] = Field(
        None,
        description="Motet product version of this worker process",
        json_schema_extra={"example": "0.1.0"},
    )


@router.get(
    "/readiness",
    summary="Get worker readiness status",
    description="Get comprehensive worker readiness status including system stats and individual worker details",
    response_description="Worker readiness status and statistics"
)
async def worker_readiness_status():
    """
    Get worker readiness status and statistics.
    
    Returns comprehensive information about all workers including:
    - System-wide readiness statistics
    - Individual worker details (state, capabilities, utilization)
    - Health metrics (memory, CPU, uptime)
    - Tool inventory per worker
    
    This endpoint is used by monitoring systems and the operations dashboard.
    No authentication required (monitoring endpoint).
    """
    try:
        from ....core.distributed.worker_readiness import get_readiness_service
        from ....core.distributed.worker_lifecycle import get_lifecycle_service
        
        readiness_service = get_readiness_service()
        lifecycle_service = get_lifecycle_service()
        
        # Get overall stats
        stats = readiness_service.get_readiness_stats()
        
        # Get individual worker details
        all_workers = readiness_service.get_all_workers()
        worker_details = {}
        
        for worker_id, worker_info in all_workers.items():
            # Get health metrics from lifecycle service (which reads utilization data)
            health_metrics = lifecycle_service.get_worker_health_metrics(worker_id)
            
            # Calculate uptime from startup time
            uptime_seconds = time.time() - worker_info.startup_time if worker_info.startup_time > 0 else 0
            
            worker_details[worker_id] = {
                'state': worker_info.state.value,
                'capabilities': worker_info.capabilities,
                'active_commands': worker_info.active_commands,
                'max_concurrency': worker_info.max_concurrency,
                'utilization_percent': (worker_info.active_commands / worker_info.max_concurrency * 100) if worker_info.max_concurrency > 0 else 0,
                'tool_count': worker_info.tool_count,
                'mcp_tool_count': worker_info.mcp_tool_count,
                'tools': worker_info.tools,  # Detailed tool information for dashboard
                'instance_managers': worker_info.instance_managers or {},
                'warmup_completed': worker_info.warmup_completed,
                'warmup_duration_ms': worker_info.warmup_duration_ms,
                'pool_type': worker_info.pool_type,  # ADR-0033: Worker concurrency model
                'last_heartbeat': worker_info.last_heartbeat,
                'heartbeat_age_seconds': time.time() - worker_info.last_heartbeat if worker_info.last_heartbeat > 0 else None,
                
                # Health metrics from lifecycle service (reads utilization data)
                'memory_usage_mb': health_metrics.memory_usage_mb if health_metrics else 0.0,
                'cpu_usage_percent': health_metrics.cpu_usage_percent if health_metrics else 0.0,
                'uptime_seconds': health_metrics.uptime_seconds if health_metrics else uptime_seconds,
                'is_healthy': health_metrics.is_healthy() if health_metrics else True,
                'motet_version': worker_info.motet_version,
            }
        
        return JSONResponse({
            'status': 'success',
            'timestamp': datetime.now().isoformat(),
            'system_stats': stats,
            'workers': worker_details
        })
        
    except Exception as e:
        logger.error("Failed to get worker readiness status", error=str(e), exc_info=True)
        return JSONResponse({
            'status': 'error',
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        })


@router.post(
    "/{worker_id}/terminate",
    summary="Terminate a specific worker",
    description="Terminate a worker with specified reason and method (requires API key authentication)",
    response_description="Termination result"
)
async def terminate_worker(
    worker_id: str,
    request: WorkerTerminateRequest,
    principal: Principal = Depends(get_current_principal)
):
    """
    Terminate a specific worker.
    
    Allows manual termination of a worker with configurable method:
    - `graceful_shutdown`: Gracefully shutdown worker (wait for tasks to complete)
    - `immediate`: Immediately terminate worker (kill process)
    - `revoke_tasks`: Revoke active tasks and shutdown
    
    Requires authentication (destructive operation).
    
    Args:
        worker_id: ID of the worker to terminate
        request: Termination request with reason, method, and timeout
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        Termination result with success status and details
    """
    
    try:
        from ....core.distributed.worker_lifecycle import (
            get_lifecycle_service,
            TerminationReason,
            TerminationMethod,
        )

        require_admin_principal(principal, detail=_LIFECYCLE_ADMIN_DETAIL)

        # Validate inputs
        try:
            termination_reason = TerminationReason(request.reason)
            termination_method = TerminationMethod(request.method)
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail={
                    'error': f'Invalid parameter: {e}',
                    'valid_reasons': [r.value for r in TerminationReason],
                    'valid_methods': [m.value for m in TerminationMethod],
                },
            )
        
        lifecycle_service = get_lifecycle_service()
        result = lifecycle_service.terminate_worker(
            worker_id=worker_id,
            reason=termination_reason,
            method=termination_method,
            timeout_seconds=request.timeout_seconds
        )
        logger.info(
            "admin_audit",
            action="worker_terminated",
            worker_id=worker_id,
            reason=request.reason,
            method=request.method,
            principal_id=principal.id,
            tenant_id=principal.tenant_id,
        )
        
        return JSONResponse({
            'status': 'success',
            'termination_result': result,
            'timestamp': datetime.now().isoformat()
        })
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to terminate worker", worker_id=worker_id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to terminate worker: {str(e)}")


@router.post(
    "/{worker_id}/start",
    summary="Start a specific worker",
    description="Start a worker container by worker ID (requires authentication)",
    response_description="Start result",
    responses={
        200: {"description": "Worker started"},
        401: {"description": "Unauthorized"},
        404: {"description": "Worker container not found"},
        500: {"description": "Start failed"},
    },
)
async def start_worker(
    worker_id: str,
    principal: Principal = Depends(get_current_principal),
):
    """
    Start a specific worker.

    Requires authentication (destructive operation).
    """
    try:
        require_admin_principal(principal, detail=_LIFECYCLE_ADMIN_DETAIL)
        from uuid import uuid4
        from motet.core.commands.builtin.worker_lifecycle import (
            WorkerLifecycleAction,
            WorkerLifecycleData,
            worker_lifecycle,
        )
        from ....core.workers import global_invoker

        command = worker_lifecycle(
            task_id=str(uuid4()),
            conversation_id="",
            data=WorkerLifecycleData(
                worker_id=worker_id,
                action=WorkerLifecycleAction.START,
                requested_by=principal.id,
            ),
        )

        result = await asyncio.to_thread(
            global_invoker.execute_command,
            command,
            WORKER_LIFECYCLE_TARGET,
        )
        normalized = _normalize_lifecycle_result(result)

        if normalized.get("status") == "error":
            raise HTTPException(
                status_code=404,
                detail=_lifecycle_error_message(normalized, "Failed to start worker"),
            )

        return JSONResponse({
            'status': 'success',
            'start_result': normalized.get("data", normalized),
            'timestamp': datetime.now().isoformat()
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to start worker", worker_id=worker_id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to start worker: {str(e)}")


@router.post(
    "/{worker_id}/stop",
    summary="Stop a specific worker",
    description="Stop a worker container by worker ID (requires authentication)",
    response_description="Stop result",
    responses={
        200: {"description": "Worker stopped"},
        401: {"description": "Unauthorized"},
        404: {"description": "Worker container not found"},
        500: {"description": "Stop failed"},
    },
)
async def stop_worker(
    worker_id: str,
    principal: Principal = Depends(get_current_principal),
):
    """
    Stop a specific worker.

    Requires authentication (destructive operation).
    """
    try:
        require_admin_principal(principal, detail=_LIFECYCLE_ADMIN_DETAIL)
        from uuid import uuid4
        from motet.core.commands.builtin.worker_lifecycle import (
            WorkerLifecycleAction,
            WorkerLifecycleData,
            worker_lifecycle,
        )
        from ....core.workers import global_invoker

        command = worker_lifecycle(
            task_id=str(uuid4()),
            conversation_id="",
            data=WorkerLifecycleData(
                worker_id=worker_id,
                action=WorkerLifecycleAction.STOP,
                requested_by=principal.id,
            ),
        )

        result = await asyncio.to_thread(
            global_invoker.execute_command,
            command,
            WORKER_LIFECYCLE_TARGET,
        )
        normalized = _normalize_lifecycle_result(result)

        if normalized.get("status") == "error":
            raise HTTPException(
                status_code=404,
                detail=_lifecycle_error_message(normalized, "Failed to stop worker"),
            )

        return JSONResponse({
            'status': 'success',
            'stop_result': normalized.get("data", normalized),
            'timestamp': datetime.now().isoformat()
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to stop worker", worker_id=worker_id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to stop worker: {str(e)}")


@router.post(
    "/{worker_id}/restart",
    summary="Restart a specific worker",
    description="Restart a worker container by worker ID (requires authentication)",
    response_description="Restart result",
    responses={
        200: {"description": "Worker restarted"},
        401: {"description": "Unauthorized"},
        404: {"description": "Worker container not found"},
        500: {"description": "Restart failed"},
    },
)
async def restart_worker(
    worker_id: str,
    principal: Principal = Depends(get_current_principal),
):
    """
    Restart a specific worker.

    Requires authentication (destructive operation).
    """
    try:
        require_admin_principal(principal, detail=_LIFECYCLE_ADMIN_DETAIL)
        from uuid import uuid4
        from motet.core.commands.builtin.worker_lifecycle import (
            WorkerLifecycleAction,
            WorkerLifecycleData,
            worker_lifecycle,
        )
        from ....core.workers import global_invoker

        command = worker_lifecycle(
            task_id=str(uuid4()),
            conversation_id="",
            data=WorkerLifecycleData(
                worker_id=worker_id,
                action=WorkerLifecycleAction.RESTART,
                requested_by=principal.id,
            ),
        )

        result = await asyncio.to_thread(
            global_invoker.execute_command,
            command,
            WORKER_LIFECYCLE_TARGET,
        )
        normalized = _normalize_lifecycle_result(result)

        if normalized.get("status") == "error":
            raise HTTPException(
                status_code=404,
                detail=_lifecycle_error_message(normalized, "Failed to restart worker"),
            )

        return JSONResponse({
            'status': 'success',
            'restart_result': normalized.get("data", normalized),
            'timestamp': datetime.now().isoformat()
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to restart worker", worker_id=worker_id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to restart worker: {str(e)}")


@router.get(
    "/health",
    summary="Get worker health status",
    description="Get health status of all workers including metrics and unhealthy worker identification",
    response_description="Health status for all workers"
)
async def get_worker_health():
    """
    Get health status of all workers.
    
    Returns comprehensive health metrics for all workers including:
    - Overall health status (healthy/unhealthy)
    - Heartbeat status and age
    - Task success rate
    - Active and stuck tasks
    - Memory and CPU usage
    - Error counts
    - Uptime
    - Termination reason (if unhealthy)
    
    This endpoint is used for health monitoring and alerting.
    No authentication required (monitoring endpoint).
    """
    try:
        from ....core.distributed.worker_lifecycle import get_lifecycle_service
        from ....core.distributed.worker_readiness import get_readiness_service
        
        lifecycle_service = get_lifecycle_service()
        readiness_service = get_readiness_service()
        all_workers = readiness_service.get_all_workers()
        
        worker_health = {}
        unhealthy_workers = []
        
        for worker_id in all_workers.keys():
            metrics = lifecycle_service.get_worker_health_metrics(worker_id)
            if metrics:
                term_reason = metrics.get_termination_reason()
                worker_health[worker_id] = {
                    'healthy': metrics.is_healthy(),
                    'last_heartbeat': metrics.last_heartbeat,
                    'heartbeat_age_seconds': time.time() - metrics.last_heartbeat,
                    'success_rate': metrics.success_rate,
                    'active_tasks': metrics.active_tasks,
                    'stuck_tasks': metrics.stuck_tasks,
                    'memory_usage_mb': metrics.memory_usage_mb,
                    'cpu_usage_percent': metrics.cpu_usage_percent,
                    'error_count_last_hour': metrics.error_count_last_hour,
                    'uptime_seconds': metrics.uptime_seconds,
                    'termination_reason': term_reason.value if term_reason is not None else None,
                }
                
                if not metrics.is_healthy():
                    unhealthy_workers.append(worker_id)
        
        return JSONResponse({
            'status': 'success',
            'timestamp': datetime.now().isoformat(),
            'total_workers': len(worker_health),
            'healthy_workers': len(worker_health) - len(unhealthy_workers),
            'unhealthy_workers': len(unhealthy_workers),
            'unhealthy_worker_ids': unhealthy_workers,
            'worker_health': worker_health
        })
        
    except Exception as e:
        logger.error("Failed to get worker health", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get worker health: {str(e)}")


@router.post(
    "/terminate-unhealthy",
    summary="Terminate all unhealthy workers",
    description="Automatically terminate all workers that are identified as unhealthy (requires API key authentication)",
    response_description="Termination results for all unhealthy workers"
)
async def terminate_unhealthy_workers(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")
):
    """
    Automatically terminate all unhealthy workers.
    
    Identifies all workers that fail health checks and terminates them automatically.
    Useful for automated recovery and maintenance operations.
    
    Requires API key authentication (destructive operation).
    
    Args:
        x_api_key: API key for authentication
        
    Returns:
        Termination results for all unhealthy workers including success/failure counts
    """
    # Require API key for termination operations
    if not x_api_key:
        raise HTTPException(status_code=401, detail="API key required for worker termination")
    
    try:
        from ....core.distributed.worker_lifecycle import get_lifecycle_service
        
        lifecycle_service = get_lifecycle_service()
        results = lifecycle_service.auto_terminate_unhealthy_workers()
        
        successful_terminations = [r for r in results if r.get('success')]
        failed_terminations = [r for r in results if not r.get('success')]
        
        return JSONResponse({
            'status': 'success',
            'timestamp': datetime.now().isoformat(),
            'total_terminations': len(results),
            'successful_terminations': len(successful_terminations),
            'failed_terminations': len(failed_terminations),
            'termination_results': results
        })
        
    except Exception as e:
        logger.error("Failed to terminate unhealthy workers", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to terminate unhealthy workers: {str(e)}")


@router.get(
    "/termination-history",
    summary="Get worker termination history",
    description="Get historical records of worker terminations (requires API key authentication)",
    response_description="List of historical worker terminations"
)
async def get_termination_history(
    limit: int = 50,
    principal: Principal = Depends(get_current_principal)
):
    """
    Get history of worker terminations.
    
    Returns historical records of worker terminations including:
    - Worker ID
    - Termination timestamp
    - Termination reason
    - Termination method
    - Success status
    
    Useful for debugging and understanding worker lifecycle issues.
    
    Requires authentication.
    
    Args:
        limit: Maximum number of records to return (default: 50)
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        List of termination history records
    """
    
    try:
        from ....core.distributed.worker_lifecycle import get_lifecycle_service
        
        lifecycle_service = get_lifecycle_service()
        history = lifecycle_service.get_termination_history(limit)
        
        return JSONResponse({
            'status': 'success',
            'timestamp': datetime.now().isoformat(),
            'termination_history': history,
            'total_records': len(history)
        })
        
    except Exception as e:
        logger.error("Failed to get termination history", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get termination history: {str(e)}")


@router.get("/managers/status")
async def get_managers_status(
    principal: Principal = Depends(get_current_principal),
) -> JSONResponse:
    """
    Get status of all instance managers (MCP and Local Inference).

    Returns health and metrics data per manager, including instance counts,
    request stats, resource usage, and staleness detection.

    **Authentication:** JWT or service account. ``/readiness`` and
    ``/health`` remain unauthenticated for process probes.
    """
    from ....core.distributed.manager_status import ManagerStatusRegistry
    from ....core.distributed.worker_readiness import WorkerReadinessService

    try:
        registry = ManagerStatusRegistry()
        all_statuses = registry.get_all_statuses()

        # ADR-0105 §R3: served_workers is computed server-side by joining the
        # worker registry — a sibling manager cannot derive this itself because
        # workers post anonymously to its Redis Streams. We invert each worker's
        # published manager-id binding (mcp_manager_id / local_inference_manager_id)
        # to get the served set per manager. Both the MCP manager and the hoisted
        # LocalInferenceManager are sibling services (one manager serving N workers),
        # so both are computed by inversion. The manager's self-reported
        # served_workers is kept only as a fallback.
        readiness = WorkerReadinessService()
        all_workers = readiness.get_all_workers()
        mcp_served_by_manager: Dict[str, List[str]] = {}
        local_inference_served_by_manager: Dict[str, List[str]] = {}
        for w in all_workers.values():
            if w.mcp_manager_id:
                mcp_served_by_manager.setdefault(w.mcp_manager_id, []).append(
                    w.worker_id
                )
            if w.local_inference_manager_id:
                local_inference_served_by_manager.setdefault(
                    w.local_inference_manager_id, []
                ).append(w.worker_id)

        served_by_manager_for_type = {
            "mcp": mcp_served_by_manager,
            "local_inference": local_inference_served_by_manager,
        }

        managers = {}
        for mgr_status in all_statuses:
            # Synthesize manager_id only as a defensive fallback for legacy
            # publishers that didn't populate it. Both sibling managers now
            # publish their canonical manager_id (ADR-0105 §R3).
            manager_id = mgr_status.manager_id or (
                f"{mgr_status.manager_type.value}-{mgr_status.worker_id}"
            )
            manager_key = f"{manager_id}-{mgr_status.manager_type.value}"
            is_stale = registry.is_stale(mgr_status)

            # Prefer the inverted worker→manager_id binding (computed from the
            # worker registry) for the sibling managers; fall back to the
            # manager's self-report (and finally to its bootstrap worker_id).
            served_index = served_by_manager_for_type.get(mgr_status.manager_type.value)
            if served_index is not None:
                computed_served = sorted(served_index.get(manager_id, []))
                served_workers = computed_served or (
                    mgr_status.served_workers or [mgr_status.worker_id]
                )
            else:
                served_workers = (
                    mgr_status.served_workers
                    if mgr_status.served_workers
                    else [mgr_status.worker_id]
                )

            managers[manager_key] = {
                "manager_id": manager_id,
                "served_workers": served_workers,
                "worker_id": mgr_status.worker_id,
                "type": mgr_status.manager_type.value,
                "status": "stale" if is_stale else mgr_status.status,
                "pid": mgr_status.pid,
                "last_update": mgr_status.last_update,
                "instances": {
                    "total": mgr_status.instances_total,
                    "healthy": mgr_status.instances_healthy,
                    "unhealthy": mgr_status.instances_unhealthy,
                },
                "stats": {
                    "total_requests": mgr_status.total_requests,
                    "active_requests": mgr_status.active_requests,
                    "errors": mgr_status.errors,
                    "uptime_seconds": mgr_status.uptime_seconds,
                },
                "resources": {
                    "memory_mb": mgr_status.memory_mb,
                    "cpu_percent": mgr_status.cpu_percent,
                },
                "metadata": mgr_status.metadata,
            }

        return JSONResponse({
            "status": "success",
            "managers": managers,
            "timestamp": time.time(),
        })

    except Exception as e:
        logger.error("Failed to get manager status", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get manager status: {str(e)}")

