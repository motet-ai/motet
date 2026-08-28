"""
Motet - Scheduled Commands List Tool

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Scheduled commands list tool for the Motet distributed framework.
    Provides the ability to list and query scheduled commands with filtering
    by status, schedule type, and other criteria. Enables LLMs to inspect
    and manage scheduled commands during reasoning.
    
    IMPORTANT: This tool lists Motet scheduled commands (time-based command execution),
    NOT Google Workspace tasks or other task management systems. Use MCP tools like
    'mcp.google_workspace.list_tasks' for Google Workspace task management.

Dependencies:
    - pydantic: Data validation and model definitions
    - typing: Type hints and annotations
    - Tool registry and runtime stack
    - Scheduled command manager
    - Schedule models and filters

Usage:
    from motet.core.tools.builtin.scheduled_commands_list import run

    # List all scheduled commands
    result = run({})
    
    # List active scheduled commands
    result = run({
        "status": "active",
        "limit": 20
    })
    
    # List recurring scheduled commands
    result = run({
        "schedule_type": "recurring",
        "limit": 10
    })

Notes:
    - Lists scheduled commands with optional filtering
    - Supports filtering by status, schedule_type, tenant_id, created_by
    - Includes pagination with limit and offset
    - Automatically filters by tenant/principal context from runtime stack
    - Returns schedule metadata including next execution time
    - Integrates with ScheduledCommandManager
    - Includes comprehensive error handling
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from datetime import datetime

from pydantic import BaseModel, Field

from ..registry import ToolRegistry, get_runtime_stack


class ScheduledCommandsListParams(BaseModel):
    """Parameters for listing scheduled commands."""
    status: Optional[str] = Field(default=None, description="Filter by status: active, paused, completed, cancelled, failed, expired")
    schedule_type: Optional[str] = Field(default=None, description="Filter by schedule type: immediate, delayed, recurring, conditional")
    limit: int = Field(default=50, ge=1, le=200, description="Maximum number of scheduled commands to return")
    offset: int = Field(default=0, ge=0, description="Number of scheduled commands to skip")
    include_metadata: bool = Field(default=True, description="Include full schedule metadata in results")


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    List scheduled commands with optional filtering (synchronous for Celery workers - ADR-0033).
    
    Queries ScheduledCommandManager to retrieve schedules, automatically filtering
    by tenant/principal context from runtime stack.
    """
    from ..protocol import ok, err
    
    try:
        # Parse parameters
        parsed = ScheduledCommandsListParams(**(params or {}))
    except Exception as exc:
        return err(f"validation error: {exc}")
    
    # Get runtime stack for context
    stack = get_runtime_stack()
    if not stack:
        return err("runtime stack not available")
    
    from ...workers.invoker_context import resolve_current_identity
    identity = resolve_current_identity()
    tenant_id = identity.tenant_id
    motet_id = identity.motet_id
    principal_id = identity.principal_id
    
    # Log extracted context for debugging
    import structlog
    logger = structlog.get_logger(__name__)
    logger.debug("scheduled_commands_list_context_extracted",
                tenant_id=tenant_id,
                motet_id=motet_id,
                principal_id=principal_id,
                principal_id_empty=not principal_id,
                status_filter=parsed.status,
                schedule_type_filter=parsed.schedule_type)
    
    try:
        # Import here to avoid circular dependencies at module level
        from ...orchestration.scheduling.manager import ScheduledCommandManager
        from ...orchestration.scheduling.models import ScheduleFilter, ScheduleStatus, ScheduleType
        
        # Initialize schedule manager
        manager = ScheduledCommandManager()
        
        # Build filter
        filters = ScheduleFilter(
            tenant_id=tenant_id,  # Automatically filter by tenant
            motet_id=motet_id,  # Automatically filter by motet (ADR-0056)
            limit=parsed.limit,
            offset=parsed.offset
        )
        
        # Parse status filter
        if parsed.status:
            try:
                filters.status = ScheduleStatus(parsed.status.lower())
            except ValueError:
                return err(f"invalid status: {parsed.status}. Must be one of: active, paused, completed, cancelled, failed, expired")
        
        # Parse schedule_type filter
        if parsed.schedule_type:
            try:
                filters.schedule_type = ScheduleType(parsed.schedule_type.lower())
            except ValueError:
                return err(f"invalid schedule_type: {parsed.schedule_type}. Must be one of: immediate, delayed, recurring, conditional")
        
        # Optionally filter by principal (if provided)
        if principal_id:
            filters.created_by = principal_id
        
        # Log filter details for debugging
        logger.debug("scheduled_commands_list_query_filters",
                    tenant_id=filters.tenant_id,
                    motet_id=filters.motet_id,
                    principal_id=filters.created_by,
                    status=filters.status,
                    schedule_type=filters.schedule_type,
                    limit=filters.limit,
                    offset=filters.offset)
        
        # List schedules
        schedules = manager.list_schedules(filters)
        
        # Log results for debugging
        logger.debug("scheduled_commands_list_query_results",
                    schedules_found=len(schedules),
                    tenant_id=filters.tenant_id,
                    motet_id=filters.motet_id,
                    principal_id=filters.created_by)
        
        # Format results
        results = []
        for schedule in schedules:
            schedule_dict = {
                "schedule_id": schedule.schedule_id,
                "command_id": schedule.command_id,
                "command_type": schedule.command_type,
                "name": schedule.name,
                "schedule_type": schedule.schedule_type.value if isinstance(schedule.schedule_type, ScheduleType) else str(schedule.schedule_type),
                "status": schedule.status.value if isinstance(schedule.status, ScheduleStatus) else str(schedule.status),
                "created_at": schedule.created_at.isoformat() if schedule.created_at else None,
                "next_execution_at": schedule.next_execution_at.isoformat() if schedule.next_execution_at else None,
                "last_execution_at": schedule.last_execution_at.isoformat() if schedule.last_execution_at else None,
                "execution_count": schedule.execution_count,
                "max_executions": schedule.max_executions,
            }
            
            # Include additional metadata if requested
            if parsed.include_metadata:
                schedule_dict.update({
                    "cron_expression": schedule.cron_expression,
                    "interval_seconds": schedule.interval_seconds,
                    "scheduled_at": schedule.scheduled_at.isoformat() if schedule.scheduled_at else None,
                    "recurring_until": schedule.recurring_until.isoformat() if schedule.recurring_until else None,
                    "condition_expression": schedule.condition_expression,
                    "timeout_seconds": schedule.timeout_seconds,
                    "priority": schedule.priority,
                    "max_retries": schedule.max_retries,
                    "target_worker_id": schedule.target_worker_id,
                    "preferred_worker_ids": schedule.preferred_worker_ids,
                    "worker_affinity": schedule.worker_affinity,
                    "avoid_worker_ids": schedule.avoid_worker_ids,
                    "consecutive_failures": schedule.consecutive_failures,
                    "last_error": schedule.last_error,
                })
            
            results.append(schedule_dict)
        
        return ok({
            "total": len(results),
            "schedules": results,
            "limit": parsed.limit,
            "offset": parsed.offset,
            "filters": {
                "status": parsed.status,
                "schedule_type": parsed.schedule_type,
                "tenant_id": tenant_id,
                "motet_id": motet_id,
            }
        })
        
    except Exception as exc:
        return err(f"failed to list scheduled commands: {str(exc)}")


def _parse(ln: str, trig: str) -> Dict[str, Any]:
    """Parse natural language input into structured parameters."""
    text = ln[len(trig):].strip()
    params: Dict[str, Any] = {}
    
    # Simple parsing: extract key=value pairs
    if "=" in text:
        parts = text.split()
        for part in parts:
            if "=" in part:
                k, v = part.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k == "status":
                    params["status"] = v
                elif k == "type" or k == "schedule_type":
                    params["schedule_type"] = v
                elif k == "limit":
                    try:
                        params["limit"] = int(v)
                    except ValueError:
                        pass
                elif k == "offset":
                    try:
                        params["offset"] = int(v)
                    except ValueError:
                        pass
    elif text:
        # If just text provided, treat as status filter
        params["status"] = text.lower()
    
    return params


def _fmt(res: Dict[str, Any]) -> str:
    """Format result for observation."""
    if "error" in res:
        return f"scheduled_commands_list(error={res['error']})"
    
    total = res.get("total", 0)
    return f"scheduled_commands_list(count={total})"


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.scheduled_commands_list",
        description="List Motet scheduled commands with optional filtering by status, schedule type, and other criteria. "
        "IMPORTANT: This tool lists Motet scheduled commands (time-based command execution), NOT Google Workspace tasks or other task management systems. "
        "For Google Workspace task management, use MCP tools like 'mcp__google_workspace__list_tasks'. "
        "Note: Schedules are immutable after creation. To change a schedule's attributes (cron_expression, interval_seconds, name, etc.), "
        "you must delete the old schedule (using manage_schedule with action='delete') and create a new one (using schedule_command). "
        "Use this to inspect and manage scheduled commands. Returns commands scheduled by the current user (principal_id).",
        func=run,
        tool_schema=ScheduledCommandsListParams,
        triggers=["scheduled_commands_list:", "scheduled_commands:", "list_scheduled_commands:", "scheduled_tasks_list:", "scheduled_tasks:", "list_scheduled_tasks:", "schedules_list:", "schedules:", "list_schedules:", "scheduled:"],  # Keep old triggers for backward compatibility
        priority=5,
        parse_params=_parse,
        observation_formatter=_fmt,
        category="system",
        default_timeout_seconds=5.0,
        suggested_max_calls=5,
        cost_class="low",
    )


__all__ = ["register"]
