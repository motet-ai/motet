"""
Motet - Manage Schedule Tool

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Manage schedule tool for the Motet distributed framework.
    Provides the ability to suspend, resume, cancel, or delete scheduled commands.
    Enables LLMs to manage scheduled commands they create during reasoning.
    
    IMPORTANT: This tool manages Motet scheduled commands (time-based command execution),
    NOT Google Workspace tasks or other task management systems. Use MCP tools like
    'mcp.google_workspace.update_task' for Google Workspace task management.

Dependencies:
    - pydantic: Data validation and model definitions
    - typing: Type hints and annotations
    - Tool registry and runtime stack
    - Scheduled command manager
    - Schedule models

Usage:
    from motet.core.tools.builtin.manage_schedule import run

    # Suspend a scheduled command
    result = run({
        "schedule_id": "schedule_123",
        "action": "suspend"
    })
    
    # Resume a paused scheduled command
    result = run({
        "schedule_id": "schedule_123",
        "action": "resume"
    })
    
    # Cancel a scheduled command
    result = run({
        "schedule_id": "schedule_123",
        "action": "cancel"
    })
    
    # Delete a scheduled command
    result = run({
        "schedule_id": "schedule_123",
        "action": "delete"
    })

Notes:
    - Manages scheduled commands (suspend, resume, cancel, delete)
    - Automatically verifies schedule ownership via principal_id
    - Only allows management of schedules created by the current user
    - Includes comprehensive error handling and validation
    - Integrates with ScheduledCommandManager
"""

from __future__ import annotations

from typing import Any, Dict, Literal

from pydantic import BaseModel, Field

from ..registry import ToolRegistry, get_runtime_stack


class ManageScheduleParams(BaseModel):
    """Parameters for managing a scheduled command."""
    schedule_id: str = Field(..., description="Schedule ID of the command to manage")
    action: Literal["suspend", "resume", "cancel", "delete"] = Field(
        ...,
        description="Action to perform. ONLY these 4 operations are supported: 'suspend' (pause execution), 'resume' (resume paused schedule), 'cancel' (cancel schedule), 'delete' (permanently delete schedule). "
        "NOT supported: 'update', 'modify', 'edit' - Schedule attributes cannot be changed after creation. To change attributes, delete the schedule and create a new one."
    )


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Manage a scheduled command (suspend, resume, cancel, or delete).
    
    Verifies schedule ownership via principal_id before allowing management operations.
    Only allows management of schedules created by the current user.
    """
    from ..protocol import ok, err
    
    try:
        # Parse parameters
        parsed = ManageScheduleParams(**(params or {}))
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
    
    try:
        # Import here to avoid circular dependencies at module level
        from ...orchestration.scheduling.manager import ScheduledCommandManager
        from ...orchestration.scheduling.models import ScheduleStatus
        
        # Initialize schedule manager
        manager = ScheduledCommandManager()
        
        # Retrieve schedule to verify ownership
        schedule = manager.get_schedule(parsed.schedule_id)
        if not schedule:
            return err(f"Schedule not found: {parsed.schedule_id}")
        
        # Security checks: verify tenant, motet, and principal ownership
        if schedule.tenant_id and schedule.tenant_id != tenant_id:
            return err(f"Permission denied: Schedule {parsed.schedule_id} belongs to a different tenant.")
        
        if schedule.motet_id and schedule.motet_id != motet_id:
            return err(f"Permission denied: Schedule {parsed.schedule_id} belongs to a different motet/environment.")
        
        # Verify ownership - only allow management of schedules created by current principal
        if principal_id and schedule.created_by and schedule.created_by != principal_id:
            return err(f"Permission denied: Schedule {parsed.schedule_id} was created by a different user. You can only manage schedules you created.")
        
        # Perform action based on type
        success = False
        action_message = ""
        
        if parsed.action == "suspend":
            # Check if it's already paused or in wrong state before attempting
            if schedule.status == ScheduleStatus.PAUSED:
                return err(f"Schedule is already paused")
            elif schedule.status != ScheduleStatus.ACTIVE:
                return err(f"Cannot suspend schedule - current status is {schedule.status.value} (must be active)")
            
            success = manager.suspend_schedule(parsed.schedule_id)
            action_message = "suspended (paused)"
        
        elif parsed.action == "resume":
            # Check if it's already active or in wrong state before attempting
            if schedule.status == ScheduleStatus.ACTIVE:
                return err(f"Schedule is already active")
            elif schedule.status != ScheduleStatus.PAUSED:
                return err(f"Cannot resume schedule - current status is {schedule.status.value} (must be paused)")
            
            success = manager.resume_schedule(parsed.schedule_id)
            action_message = "resumed"
        
        elif parsed.action == "cancel":
            success = manager.cancel_schedule(parsed.schedule_id)
            action_message = "cancelled"
        
        elif parsed.action == "delete":
            success = manager.delete_schedule(parsed.schedule_id)
            action_message = "deleted"
        else:
            # Handle unsupported actions (update, modify, edit, etc.)
            return err(f"Unsupported action: {parsed.action}. Available actions: suspend, resume, cancel, delete. "
                      f"Schedule attributes cannot be modified after creation. To change schedule attributes, "
                      f"delete the existing schedule and create a new one with schedule_command.")
        
        if not success:
            return err(f"Failed to {parsed.action} schedule: {parsed.schedule_id}")
        
        # Get updated schedule info (if not deleted)
        updated_schedule = None
        if parsed.action != "delete":
            updated_schedule = manager.get_schedule(parsed.schedule_id)
        
        result: Dict[str, Any] = {
            "schedule_id": parsed.schedule_id,
            "action": parsed.action,
            "status": "success",
            "message": f"Schedule {parsed.schedule_id} {action_message} successfully"
        }
        
        if updated_schedule:
            result["current_status"] = updated_schedule.status.value if hasattr(updated_schedule.status, 'value') else str(updated_schedule.status)
            result["name"] = updated_schedule.name
        
        return ok(result)
        
    except Exception as exc:
        return err(f"failed to {parsed.action} schedule: {str(exc)}")


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
                if k == "schedule_id" or k == "id":
                    params["schedule_id"] = v
                elif k == "action":
                    v_lower = v.lower()
                    # Handle update/modify/edit attempts - these are not supported
                    if v_lower in ["update", "modify", "edit", "change"]:
                        # These actions are not supported - schedules are immutable
                        # We'll let the validation error handle this, but could also return a helpful error here
                        pass
                    elif v_lower in ["suspend", "resume", "cancel", "delete", "pause", "stop", "remove"]:
                        # Map common aliases
                        if v_lower in ["pause", "stop"]:
                            params["action"] = "suspend"
                        elif v_lower == "remove":
                            params["action"] = "delete"
                        else:
                            params["action"] = v_lower
    elif text:
        # If just text provided, try to infer action from keywords
        text_lower = text.lower()
        if any(word in text_lower for word in ["pause", "suspend", "stop"]):
            params["action"] = "suspend"
        elif any(word in text_lower for word in ["resume", "start", "continue"]):
            params["action"] = "resume"
        elif any(word in text_lower for word in ["cancel", "abort"]):
            params["action"] = "cancel"
        elif any(word in text_lower for word in ["delete", "remove"]):
            params["action"] = "delete"
    
    return params


def _fmt(res: Dict[str, Any]) -> str:
    """Format result for observation."""
    if "error" in res:
        return f"manage_schedule(error={res['error']})"
    
    action = res.get("action", "unknown")
    schedule_id = res.get("schedule_id", "unknown")
    return f"manage_schedule(action={action}, schedule_id={schedule_id})"


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.manage_schedule",
        description="Manage a Motet scheduled command: suspend (pause), resume (unpause), cancel (stop future executions), or delete (permanently remove). "
        "IMPORTANT: This tool manages Motet scheduled commands (time-based command execution), NOT Google Workspace tasks or other task management systems. "
        "For Google Workspace task management, use MCP tools like 'mcp__google_workspace__update_task' or 'mcp__google_workspace__delete_task'. "
        "Use this to control scheduled commands you created. You can only manage schedules you created (filtered by principal_id). "
        "Available operations (ONLY these 4): "
        "'suspend' - Pause execution (status → paused), 'resume' - Resume paused schedule (status → active), "
        "'cancel' - Stop future executions (status → cancelled), 'delete' - Permanently remove schedule. "
        "NOT available: 'update', 'modify', 'edit' - Schedule attributes CANNOT be changed after creation. "
        "Schedule attributes (cron_expression, interval_seconds, name, scheduled_at, command_data, etc.) are IMMUTABLE. "
        "To change schedule attributes: 1) Delete old schedule (manage_schedule with action='delete'), 2) Create new schedule (schedule_command) with desired attributes. "
        "Example: manage_schedule with schedule_id='abc123', action='suspend' to pause a schedule. "
        "Example: manage_schedule with schedule_id='abc123', action='resume' to resume a paused schedule. "
        "Example: manage_schedule with schedule_id='abc123', action='cancel' to cancel a schedule. "
        "Example: manage_schedule with schedule_id='abc123', action='delete' to permanently delete a schedule. "
        "Example of what NOT to do: You cannot 'update' or 'modify' a schedule's cron_expression. Instead, delete the old schedule and create a new one.",
        func=run,
        tool_schema=ManageScheduleParams,
        triggers=["manage_schedule:", "manage_task:", "schedule_manage:", "pause:", "resume:", "cancel:", "delete:", "suspend:", "stop_schedule:"],  # Keep manage_task: for backward compatibility
        priority=5,
        parse_params=_parse,
        observation_formatter=_fmt,
        category="system",
        default_timeout_seconds=5.0,
        suggested_max_calls=10,
        cost_class="low",
    )


__all__ = ["register"]
