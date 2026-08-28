"""
Motet - Schedule API

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-17

Description:
    Schedule API for the Motet distributed framework.
    Provides REST API endpoints for schedule management and execution.

Dependencies:
    - fastapi: Web framework for REST API
    - pydantic: Data validation and serialization
    - datetime: Time and date handling
    - Schedule management system

Usage:
    from motet.interfaces.api.v1.schedules import router
    
    # Include in FastAPI app
    app.include_router(router)

Notes:
    - Provides comprehensive schedule management
    - Includes CRUD operations for schedules
    - Supports cron expressions and one-time schedules
    - Integrates with distributed command system
    - Delayed schedules accept scheduled_at (absolute ISO 8601) or delay_seconds (relative)
    - command_data is validated against the target command's data class on create; a
      schedule is immutable and may be recurring, so a payload that only fails at
      execution time would keep failing on every firing
    - tenant_id and created_by on create are names, not permission (issue #214).
      Foreign values 403 unless can_access_all_tenants; lists default to the
      caller's tenant. Get/cancel/delete/suspend/resume check the schedule tenant.
    - List and stats accept optional tenant_id / motet_id for the manage-app
      scope selector; those query params still go through require_tenant_access
      / require_motet_access.
"""

import os
from typing import Dict, List, Optional, Any
from datetime import datetime
from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel, Field

from ..shared.auth import (
    can_access_all_tenants,
    get_current_principal,
    require_motet_access,
    require_tenant_access,
)
from ..shared.scope import ManageAppScope, get_manage_app_scope
from ....core.types import Principal
from ....core.orchestration.scheduling.manager import ScheduledCommandManager
from ....core.orchestration.scheduling.models import ScheduleMetadata, ScheduleFilter, ScheduleStatus, ScheduleType

router = APIRouter(prefix="/api/v1/schedules", tags=["schedules"])

# Initialize schedule manager
schedule_manager = ScheduledCommandManager()


def _require_schedule_tenant_access(principal: Principal, schedule: ScheduleMetadata) -> None:
    """Raise HTTP 403 unless the caller may access this schedule's tenant."""
    if not schedule.tenant_id:
        if not can_access_all_tenants(principal):
            raise HTTPException(
                status_code=403,
                detail="Not authorized to access another tenant",
            )
        return
    require_tenant_access(principal, schedule.tenant_id)

class ScheduleListResponse(BaseModel):
    """Response model for schedule listing."""
    total_schedules: int = Field(..., description="Total number of schedules matching the filter")
    schedules: List[Dict[str, Any]] = Field(..., description="List of schedule summary objects")
    last_updated: str = Field(..., description="ISO 8601 timestamp of the most recent schedule update")

class ScheduleDetailResponse(BaseModel):
    """Response model for individual schedule details."""
    schedule: Dict[str, Any] = Field(..., description="Full schedule configuration and status")
    execution_history: List[Dict[str, Any]] = Field(..., description="Recent execution records for this schedule")

class CreateScheduleRequest(BaseModel):
    """Request model for creating a new scheduled command."""
    command_type: str = Field(..., description="Registered command type to execute")
    command_data: Dict[str, Any] = Field(..., description="Command-specific payload")
    schedule_type: ScheduleType = Field(..., description="Schedule trigger type (IMMEDIATE, DELAYED, RECURRING, CONDITIONAL)")
    name: Optional[str] = Field(default=None, description="Human-readable name for the schedule")
    schedule_context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Execution context for scheduled runs (agent_id, surface_id, principal_roles, model_provider, model_name, model_profile_name, enable_thinking, reasoning_effort). Written to the target command envelope metadata.",
        json_schema_extra={"example": {"model_provider": "openai", "model_name": "gpt-4.1-mini", "model_profile_name": "default"}},
    )
    scheduled_at: Optional[datetime] = Field(
        default=None,
        description="Absolute execution time for DELAYED schedules (ISO 8601). Provide scheduled_at or delay_seconds, not both.",
        json_schema_extra={"example": "2026-07-27T14:30:00Z"},
    )
    delay_seconds: Optional[int] = Field(
        default=None,
        description="Relative delay in seconds from now for DELAYED schedules. Alternative to scheduled_at.",
        json_schema_extra={"example": 30},
    )
    cron_expression: Optional[str] = Field(default=None, description="Cron expression (for RECURRING schedules)")
    interval_seconds: Optional[int] = Field(default=None, description="Repeat interval in seconds (for RECURRING schedules)")
    condition_expression: Optional[str] = Field(default=None, description="Condition to evaluate (for CONDITIONAL schedules)")
    timeout_seconds: int = Field(default=300, description="Maximum execution time in seconds")
    priority: int = Field(default=5, description="Execution priority (1=highest, 10=lowest)")
    max_retries: int = Field(default=3, description="Maximum retry attempts on failure")
    target_worker_id: Optional[str] = Field(default=None, description="Pin execution to a specific worker")
    preferred_worker_ids: List[str] = Field(default_factory=list, description="Preferred workers for execution")
    worker_affinity: Optional[str] = Field(default=None, description="Worker affinity key for sticky routing")
    avoid_worker_ids: List[str] = Field(default_factory=list, description="Workers to exclude from execution")
    tenant_id: Optional[str] = Field(
        default=None,
        description=(
            "Tenant scope. Omitted uses the authenticated principal's tenant. "
            "A different tenant requires global tenant access; otherwise 403."
        ),
    )
    created_by: Optional[str] = Field(
        default=None,
        description=(
            "Principal ID of the schedule creator. Omitted uses the authenticated "
            "principal. A different id requires global tenant access; otherwise 403."
        ),
    )

class CreateScheduleResponse(BaseModel):
    """Response model for schedule creation."""
    status: str = Field(..., description="Creation status (e.g. 'created')")
    schedule_id: str = Field(..., description="Unique identifier of the new schedule")
    message: str = Field(..., description="Human-readable result message")
    created_at: str = Field(..., description="ISO 8601 timestamp of creation")


class ScheduleActionResponse(BaseModel):
    """Response model for schedule lifecycle actions (cancel, delete, suspend, resume)."""
    status: str = Field(..., description="Action result status")
    message: str = Field(..., description="Human-readable result message")

@router.get("", response_model=ScheduleListResponse)
@router.get("/", response_model=ScheduleListResponse)
async def list_schedules(
    status: Optional[ScheduleStatus] = Query(None, description="Filter by schedule status"),
    schedule_type: Optional[ScheduleType] = Query(None, description="Filter by schedule type"),
    limit: int = Query(50, description="Maximum number of schedules to return"),
    offset: int = Query(0, description="Number of schedules to skip"),
    scope: ManageAppScope = Depends(get_manage_app_scope),
    principal: Principal = Depends(get_current_principal)
) -> ScheduleListResponse:
    """List schedules visible to the caller, optionally filtered."""
    try:
        authorized_tenant = require_tenant_access(principal, scope.tenant_id)
        authorized_motet = (
            require_motet_access(principal, scope.motet_id) if scope.motet_id else None
        )
        filters = ScheduleFilter(
            status=status,
            schedule_type=schedule_type,
            tenant_id=authorized_tenant,
            motet_id=authorized_motet,
            limit=limit,
            offset=offset
        )
        
        # Get schedules
        schedules = schedule_manager.list_schedules(filters)
        
        # Convert to display format
        schedule_data = []
        for schedule in schedules:
            context_md = schedule.metadata.get("distributed_context", {}) if schedule.metadata else {}
            context_meta = context_md.get("metadata", {}) if isinstance(context_md, dict) else {}
            schedule_info = {
                "schedule_id": schedule.schedule_id,
                "name": schedule.name if schedule.name else None,
                "command_type": schedule.command_type,
                "schedule_type": schedule.schedule_type.value,
                "status": schedule.status.value,
                "created_at": schedule.created_at.isoformat() if schedule.created_at else None,
                "next_execution_at": schedule.next_execution_at.isoformat() if schedule.next_execution_at else None,
                "last_execution_at": schedule.last_execution_at.isoformat() if schedule.last_execution_at else None,
                "execution_count": schedule.execution_count,
                "consecutive_failures": schedule.consecutive_failures,
                "max_consecutive_failures": schedule.max_consecutive_failures,
                "tenant_id": schedule.tenant_id,
                "created_by": schedule.created_by,
                # Worker targeting info
                "target_worker_id": schedule.target_worker_id,
                "preferred_worker_ids": schedule.preferred_worker_ids,
                "worker_affinity": schedule.worker_affinity,
                "avoid_worker_ids": schedule.avoid_worker_ids,
                # Schedule-specific info
                "cron_expression": schedule.cron_expression,
                "interval_seconds": schedule.interval_seconds,
                "condition_expression": schedule.condition_expression,
                "timeout_seconds": schedule.timeout_seconds,
                "priority": schedule.priority,
                "schedule_context": context_meta if context_meta else None,
            }
            schedule_data.append(schedule_info)
        
        return ScheduleListResponse(
            total_schedules=len(schedule_data),
            schedules=schedule_data,
            last_updated=datetime.utcnow().isoformat()
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list schedules: {str(e)}")

@router.get("/command-types")
async def get_available_command_types(
    principal: Principal = Depends(get_current_principal),
) -> Dict[str, Any]:
    """Get list of available command types for scheduling."""
    try:
        # Import command data classes to get available command types
        from motet.core.commands.command_data_classes import get_command_types, get_all_command_data_classes
        
        # Get available command types
        command_type_list = get_command_types()
        command_data_classes = get_all_command_data_classes()
        
        # Format command types with descriptions
        command_types = []
        for command_type in command_type_list:
            data_class = command_data_classes.get(command_type)
            command_types.append({
                "type": command_type,
                "description": getattr(data_class, '__doc__', 'No description available') if data_class else 'No description available',
                "class_name": data_class.__name__ if data_class else 'Unknown'
            })
        
        return {
            "command_types": command_types,
            "total_count": len(command_types)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get command types: {str(e)}")

@router.get("/stats/summary")
async def get_schedule_stats(
    scope: ManageAppScope = Depends(get_manage_app_scope),
    principal: Principal = Depends(get_current_principal),
) -> Dict[str, Any]:
    """Get summary statistics about schedules in the caller's tenant."""
    try:
        authorized_tenant = require_tenant_access(principal, scope.tenant_id)
        authorized_motet = (
            require_motet_access(principal, scope.motet_id) if scope.motet_id else None
        )
        all_schedules = schedule_manager.list_schedules(
            ScheduleFilter(
                tenant_id=authorized_tenant,
                motet_id=authorized_motet,
                limit=10_000,
            )
        )
        
        # Calculate stats
        total_schedules = len(all_schedules)
        active_schedules = len([s for s in all_schedules if s.status == ScheduleStatus.ACTIVE])
        paused_schedules = len([s for s in all_schedules if s.status == ScheduleStatus.PAUSED])
        completed_schedules = len([s for s in all_schedules if s.status == ScheduleStatus.COMPLETED])
        failed_schedules = len([s for s in all_schedules if s.status == ScheduleStatus.FAILED])
        
        # Count by type
        immediate_count = len([s for s in all_schedules if s.schedule_type == ScheduleType.IMMEDIATE])
        delayed_count = len([s for s in all_schedules if s.schedule_type == ScheduleType.DELAYED])
        recurring_count = len([s for s in all_schedules if s.schedule_type == ScheduleType.RECURRING])
        conditional_count = len([s for s in all_schedules if s.schedule_type == ScheduleType.CONDITIONAL])
        
        # Count by command type
        command_types = {}
        for schedule in all_schedules:
            cmd_type = schedule.command_type
            command_types[cmd_type] = command_types.get(cmd_type, 0) + 1
        
        return {
            "total_schedules": total_schedules,
            "status_breakdown": {
                "active": active_schedules,
                "paused": paused_schedules,
                "completed": completed_schedules,
                "failed": failed_schedules
            },
            "type_breakdown": {
                "immediate": immediate_count,
                "delayed": delayed_count,
                "recurring": recurring_count,
                "conditional": conditional_count
            },
            "command_type_breakdown": command_types,
            "last_updated": datetime.utcnow().isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get schedule stats: {str(e)}")

@router.get("/{schedule_id}", response_model=ScheduleDetailResponse)
async def get_schedule_details(
    schedule_id: str,
    principal: Principal = Depends(get_current_principal),
) -> ScheduleDetailResponse:
    """Get detailed information about a specific schedule."""
    try:
        # Get schedule
        schedule = schedule_manager.get_schedule(schedule_id)
        if not schedule:
            raise HTTPException(status_code=404, detail=f"Schedule {schedule_id} not found")
        _require_schedule_tenant_access(principal, schedule)

        # Convert to display format
        context_md = schedule.metadata.get("distributed_context", {}) if schedule.metadata else {}
        context_meta = context_md.get("metadata", {}) if isinstance(context_md, dict) else {}
        schedule_info = {
            "schedule_id": schedule.schedule_id,
            "command_type": schedule.command_type,
            "command_data": schedule.metadata.get("original_command_data", {}),
            "schedule_type": schedule.schedule_type.value,
            "status": schedule.status.value,
            "created_at": schedule.created_at.isoformat() if schedule.created_at else None,
            "next_execution_at": schedule.next_execution_at.isoformat() if schedule.next_execution_at else None,
            "last_execution_at": schedule.last_execution_at.isoformat() if schedule.last_execution_at else None,
            "execution_count": schedule.execution_count,
            "consecutive_failures": schedule.consecutive_failures,
            "max_consecutive_failures": schedule.max_consecutive_failures,
            "tenant_id": schedule.tenant_id,
            "created_by": schedule.created_by,
            # Worker targeting info
            "target_worker_id": schedule.target_worker_id,
            "preferred_worker_ids": schedule.preferred_worker_ids,
            "worker_affinity": schedule.worker_affinity,
            "avoid_worker_ids": schedule.avoid_worker_ids,
            # Schedule-specific info
            "cron_expression": schedule.cron_expression,
            "interval_seconds": schedule.interval_seconds,
            "condition_expression": schedule.condition_expression,
            "timeout_seconds": schedule.timeout_seconds,
            "priority": schedule.priority,
            "schedule_context": context_meta if context_meta else None,
        }
        
        # TODO: Get execution history from Redis
        # For now, return empty history
        execution_history = []
        
        return ScheduleDetailResponse(
            schedule=schedule_info,
            execution_history=execution_history
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get schedule details: {str(e)}")

@router.delete("/{schedule_id}", responses={404: {"description": "Schedule not found"}})
async def cancel_schedule(
    schedule_id: str,
    principal: Principal = Depends(get_current_principal),
) -> Dict[str, Any]:
    """Cancel a scheduled command."""
    try:
        schedule = schedule_manager.get_schedule(schedule_id)
        if not schedule:
            raise HTTPException(status_code=404, detail=f"Schedule {schedule_id} not found or already cancelled")
        _require_schedule_tenant_access(principal, schedule)
        success = schedule_manager.cancel_schedule(schedule_id)
        if not success:
            raise HTTPException(status_code=404, detail=f"Schedule {schedule_id} not found or already cancelled")
        
        return {
            "status": "success",
            "message": f"Schedule {schedule_id} cancelled successfully",
            "cancelled_at": datetime.utcnow().isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to cancel schedule: {str(e)}")

@router.delete("/{schedule_id}/delete", responses={404: {"description": "Schedule not found"}})
async def delete_schedule(
    schedule_id: str,
    principal: Principal = Depends(get_current_principal)
) -> Dict[str, Any]:
    """Permanently delete a scheduled command."""
    try:
        schedule = schedule_manager.get_schedule(schedule_id)
        if not schedule:
            raise HTTPException(status_code=404, detail=f"Schedule {schedule_id} not found")
        _require_schedule_tenant_access(principal, schedule)
        success = schedule_manager.delete_schedule(schedule_id)
        if not success:
            raise HTTPException(status_code=404, detail=f"Schedule {schedule_id} not found")
        
        return {
            "status": "success",
            "message": f"Schedule {schedule_id} deleted successfully",
            "deleted_at": datetime.utcnow().isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete schedule: {str(e)}")

@router.post("/{schedule_id}/suspend", responses={404: {"description": "Schedule not found or cannot be suspended"}})
async def suspend_schedule(
    schedule_id: str,
    principal: Principal = Depends(get_current_principal),
) -> Dict[str, Any]:
    """Suspend a scheduled command (pause execution)."""
    try:
        schedule = schedule_manager.get_schedule(schedule_id)
        if not schedule:
            raise HTTPException(status_code=404, detail=f"Schedule {schedule_id} not found or cannot be suspended")
        _require_schedule_tenant_access(principal, schedule)
        success = schedule_manager.suspend_schedule(schedule_id)
        if not success:
            raise HTTPException(status_code=404, detail=f"Schedule {schedule_id} not found or cannot be suspended")
        
        return {
            "status": "success",
            "message": f"Schedule {schedule_id} suspended successfully",
            "suspended_at": datetime.utcnow().isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to suspend schedule: {str(e)}")

@router.post("/{schedule_id}/resume", responses={404: {"description": "Schedule not found or cannot be resumed"}})
async def resume_schedule(
    schedule_id: str,
    principal: Principal = Depends(get_current_principal)
) -> Dict[str, Any]:
    """Resume a suspended scheduled command."""
    try:
        schedule = schedule_manager.get_schedule(schedule_id)
        if not schedule:
            raise HTTPException(status_code=404, detail=f"Schedule {schedule_id} not found or cannot be resumed")
        _require_schedule_tenant_access(principal, schedule)
        success = schedule_manager.resume_schedule(schedule_id)
        if not success:
            raise HTTPException(status_code=404, detail=f"Schedule {schedule_id} not found or cannot be resumed")
        
        return {
            "status": "success",
            "message": f"Schedule {schedule_id} resumed successfully",
            "resumed_at": datetime.utcnow().isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to resume schedule: {str(e)}")

@router.post("/", response_model=CreateScheduleResponse)
async def create_schedule(
    request: CreateScheduleRequest,
    principal: Principal = Depends(get_current_principal)
) -> CreateScheduleResponse:
    """Create a new scheduled command."""
    try:
        # Import the schedule command service
        from motet.core.commands.builtin.schedule import ScheduleCommandService
        from uuid import uuid4
        
        tenant_id = require_tenant_access(principal, request.tenant_id)
        if (
            request.created_by
            and request.created_by != principal.id
            and not can_access_all_tenants(principal)
        ):
            raise HTTPException(
                status_code=403,
                detail="Not authorized to impersonate another principal",
            )
        created_by = request.created_by or principal.id

        if request.scheduled_at is not None and request.delay_seconds is not None:
            raise HTTPException(
                status_code=400,
                detail="Provide either scheduled_at or delay_seconds for DELAYED schedules, not both",
            )
        if (
            request.schedule_type == ScheduleType.DELAYED
            and request.scheduled_at is None
            and request.delay_seconds is None
        ):
            raise HTTPException(
                status_code=400,
                detail="DELAYED schedules require scheduled_at or delay_seconds",
            )
        if request.delay_seconds is not None and request.delay_seconds <= 0:
            raise HTTPException(
                status_code=400,
                detail="delay_seconds must be a positive integer",
            )

        # Reject payloads the target command cannot consume, so a recurring schedule
        # is never created against a command_data shape that fails on every firing.
        from motet.core.commands.command_data_classes import validate_command_data
        command_data_error = validate_command_data(request.command_type, request.command_data)
        if command_data_error:
            raise HTTPException(status_code=400, detail=command_data_error)

        schedule_command = ScheduleCommandService.create_schedule(
            task_id=str(uuid4()),
            target_command_type=request.command_type,
            target_command_data=request.command_data,
            schedule_type=request.schedule_type,
            name=request.name,
            schedule_context=request.schedule_context,
            scheduled_at=request.scheduled_at,
            delay_seconds=request.delay_seconds,
            cron_expression=request.cron_expression,
            interval_seconds=request.interval_seconds,
            condition_expression=request.condition_expression,
            timeout_seconds=request.timeout_seconds,
            priority=request.priority,
            max_retries=request.max_retries,
            target_worker_id=request.target_worker_id,
            preferred_worker_ids=request.preferred_worker_ids,
            worker_affinity=request.worker_affinity,
            avoid_worker_ids=request.avoid_worker_ids,
            conversation_id="",
            tenant_id=tenant_id,
            principal_id=created_by,
            trace_id=None
        )
        
        # Execute the schedule command using the distributed invoker
        from ....core.workers import global_invoker
        
        # Initialize the invoker if needed
        global_invoker.initialize()
        
        # Execute the schedule command
        result = global_invoker.execute_command(schedule_command)
        
        # The result is nested in the distributed command response
        if result and result.get("status") == "completed":
            inner_result = result.get("result", {})
            if inner_result.get("status") == "success":
                schedule_id = inner_result.get("schedule_id")
                return CreateScheduleResponse(
                    status="success",
                    schedule_id=schedule_id,
                    message=f"Schedule created successfully",
                    created_at=datetime.utcnow().isoformat()
                )
            else:
                error_msg = inner_result.get("error", "Unknown error occurred")
                raise Exception(error_msg)
        else:
            error_msg = result.get("error", "Unknown error occurred") if result else "No result returned"
            raise Exception(error_msg)
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create schedule: {str(e)}")
