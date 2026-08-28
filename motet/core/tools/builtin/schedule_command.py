"""
Motet - Schedule Command Tool

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Schedule command tool for the Motet distributed framework.
    Provides the ability to schedule distributed commands for future execution
    with support for delayed, recurring (cron), and conditional schedules.
    Enables LLMs to autonomously schedule commands during reasoning.
    
    IMPORTANT: This tool manages Motet scheduled commands (time-based command execution),
    NOT Google Workspace tasks or other task management systems. Use MCP tools like
    'mcp.google_workspace.create_task' for Google Workspace task management.

Dependencies:
    - pydantic: Data validation and model definitions
    - typing: Type hints and annotations
    - datetime: Time and date handling for scheduling
    - uuid: Unique identifier generation
    - Tool registry and runtime stack
    - Schedule command system
    - Distributed command invoker

Usage:
    from motet.core.tools.builtin.schedule_command import run

    # Schedule a delayed command with absolute time
    result = run({
        "command_type": "core.agent_turn",
        "command_data": {
            "messages": [{"role": "user", "content": "Daily summary"}],
        },
        "schedule_type": "delayed",
        "scheduled_at": "2026-01-09T09:00:00Z",
        "name": "Daily summary"
    })

    # Schedule a delayed command with relative offset (preferred for "in N seconds")
    result = run({
        "command_type": "core.agent_turn",
        "command_data": {
            "messages": [{"role": "user", "content": "Follow up"}],
        },
        "schedule_type": "delayed",
        "delay_seconds": 30,
        "name": "Follow up in 30s"
    })

    # Schedule an agent turn (messages is always a list of {role, content} items)
    result = run({
        "command_type": "core.agent_turn",
        "command_data": {"messages": [{"role": "user", "content": "hi"}]},
        "schedule_type": "recurring",
        "interval_seconds": 30,
        "name": "Agent turn every 30s"
    })
    
    # Schedule a recurring command
    result = run({
        "command_type": "core.tool_execution",
        "command_data": {"tool_name": "core.web_search", "parameters": {"query": "news"}},
        "schedule_type": "recurring",
        "cron_expression": "0 9 * * *",
        "name": "Daily news check"
    })

Notes:
    - Creates scheduled commands for future execution
    - Supports delayed (one-time), recurring (cron), and conditional schedules
    - Delayed schedules accept scheduled_at (absolute ISO 8601) or delay_seconds (relative)
    - command_data is validated against the target command's data class before the
      schedule is created; unknown fields are rejected rather than silently dropped,
      so a recurring schedule can never be created against a payload that fails on
      every firing
    - Automatically extracts tenant/principal context from runtime stack
    - Executes ScheduleCommand via distributed invoker
    - Returns schedule_id for later management
    - Integrates with distributed command system
    - Includes comprehensive error handling and validation
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, Field

from ..registry import ToolRegistry, get_runtime_stack


class ScheduleCommandParams(BaseModel):
    """Parameters for scheduling a command."""
    command_type: str = Field(..., description="Type of command to schedule (e.g., 'core.agent_turn', 'core.tool_execution')")
    command_data: Dict[str, Any] = Field(..., description="Command data/parameters as a dictionary")
    schedule_type: str = Field(..., description="Schedule type: 'immediate', 'delayed', 'recurring', or 'conditional'")
    name: Optional[str] = Field(default=None, description="Optional human-readable name for the schedule")
    model_profile_name: Optional[str] = Field(
        default=None,
        description="Optional model profile name for routing/policy overrides.",
    )
    model_provider: Optional[str] = Field(
        default=None,
        description="Optional model provider override for scheduled command inference.",
    )
    model_name: Optional[str] = Field(
        default=None,
        description="Optional model name override for scheduled command inference.",
    )
    context: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional execution context override for scheduled runs "
            "(e.g., agent_id, surface_id, principal_roles, enable_thinking, reasoning_effort). "
            "When provided, values override inherited runtime context."
        ),
    )
    # Scheduling parameters
    scheduled_at: Optional[str] = Field(
        default=None,
        description=(
            "ISO 8601 datetime for delayed execution (e.g. '2026-07-27T14:30:00Z'). "
            "For delayed schedules provide scheduled_at OR delay_seconds (not both)."
        ),
    )
    delay_seconds: Optional[int] = Field(
        default=None,
        description=(
            "Relative delay in seconds from now for delayed execution (e.g. 30 for 'in 30 seconds'). "
            "Preferred when you do not have an absolute timestamp. "
            "For delayed schedules provide scheduled_at OR delay_seconds (not both)."
        ),
    )
    cron_expression: Optional[str] = Field(default=None, description="Cron expression for recurring schedules (required for 'recurring')")
    interval_seconds: Optional[int] = Field(default=None, description="Interval in seconds for recurring schedules (alternative to cron)")
    condition_expression: Optional[str] = Field(default=None, description="Condition expression for conditional schedules")
    # Command parameters
    timeout_seconds: int = Field(default=300, description="Command timeout in seconds")
    priority: int = Field(default=5, ge=1, le=10, description="Command priority (1-10, higher is more important)")
    max_retries: int = Field(default=3, ge=0, description="Maximum retry attempts")
    # Worker targeting
    target_worker_id: Optional[str] = Field(default=None, description="Force execution on specific worker ID")
    preferred_worker_ids: Optional[List[str]] = Field(default=None, description="Preferred worker IDs in order")
    worker_affinity: Optional[str] = Field(default=None, description="Affinity key for consistent worker selection")
    avoid_worker_ids: Optional[List[str]] = Field(default=None, description="Worker IDs to avoid")


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Schedule a command for future execution (synchronous for Celery workers - ADR-0033).
    
    Creates a ScheduleCommand and executes it via the distributed invoker to register
    the schedule with ScheduledCommandManager.
    """
    from ..protocol import ok, err
    
    # Import here to avoid circular dependencies at module level
    from motet.core.commands.distributed import ScheduleType
    
    try:
        # Parse parameters
        parsed = ScheduleCommandParams(**(params or {}))
    except Exception as exc:
        return err(f"validation error: {exc}")
    
    # Get runtime stack for context
    stack = get_runtime_stack()
    if not stack:
        return err("runtime stack not available")
    
    # Extract context from runtime stack
    task_id = getattr(stack, "_task_id", None) or str(uuid4())
    conversation_id = getattr(stack, "_conversation_id", None) or ""
    from ...workers.invoker_context import resolve_current_identity
    identity = resolve_current_identity()
    tenant_id = identity.tenant_id
    principal_id = identity.principal_id
    
    # Log extracted context for debugging
    import structlog
    logger = structlog.get_logger(__name__)
    logger.debug("schedule_command_context_extracted",
                task_id=task_id,
                conversation_id=conversation_id,
                tenant_id=tenant_id,
                principal_id=principal_id,
                principal_id_empty=not principal_id)

    # Build schedule_context: stack-inherited values → explicit parsed.context → LLM model params.
    # Each layer overrides the previous, so LLM-explicit model params always win.
    from motet.core.commands.command_data_classes import SCHEDULE_CONTEXT_KEYS
    inherited_schedule_context: Dict[str, Any] = {}
    for key in SCHEDULE_CONTEXT_KEYS:
        default = [] if key == "principal_roles" else (None if key == "enable_thinking" else "")
        val = getattr(stack, f"_{key}", default)
        if val is not None and val != "" and val != []:
            inherited_schedule_context[key] = val

    explicit_context = {
        key: value for key, value in (parsed.context or {}).items()
        if value is not None and value != "" and value != []
    }
    schedule_context: Optional[Dict[str, Any]] = dict(inherited_schedule_context)
    schedule_context.update(explicit_context)

    for key, param_val in (
        ("model_profile_name", parsed.model_profile_name),
        ("model_provider", parsed.model_provider),
        ("model_name", parsed.model_name),
    ):
        val = str(param_val or "").strip()
        if val:
            schedule_context[key] = val

    if not schedule_context:
        schedule_context = None
    
    # CRITICAL: Generate conversation_id if empty for scheduled commands
    # Scheduled commands need conversation_id for proper context propagation to child commands
    # (e.g., workflows executing tool_execution commands that require conversation_id)
    if not conversation_id:
        # Generate a new conversation_id for scheduled commands to ensure proper isolation
        conversation_id = str(uuid4())
    
    # Validate command_data against the target command's schema before the schedule
    # exists. A schedule is immutable and may be recurring, so a payload that only
    # fails at execution time would keep failing on every firing.
    from motet.core.commands.command_data_classes import validate_command_data
    command_data_error = validate_command_data(parsed.command_type, parsed.command_data)
    if command_data_error:
        return err(command_data_error)

    # Validate schedule_type
    try:
        schedule_type_enum = ScheduleType(parsed.schedule_type.lower())
    except ValueError:
        return err(f"invalid schedule_type: {parsed.schedule_type}. Must be one of: immediate, delayed, recurring, conditional")
    
    # Validate schedule parameters based on type
    if schedule_type_enum == ScheduleType.DELAYED:
        absolute_at = parsed.scheduled_at or None
        relative_delay = parsed.delay_seconds
        if absolute_at and relative_delay is not None:
            return err(
                "Provide either scheduled_at (absolute ISO 8601) or delay_seconds (relative), not both"
            )
        if not absolute_at and relative_delay is None:
            return err(
                "delayed schedules require scheduled_at (ISO 8601) or delay_seconds (relative seconds from now)"
            )
        if relative_delay is not None and relative_delay <= 0:
            return err("delay_seconds must be a positive integer")
        if absolute_at:
            try:
                datetime.fromisoformat(absolute_at.replace("Z", "+00:00"))
            except Exception as e:
                return err(
                    f"invalid scheduled_at format: {e}. Use ISO 8601 format (e.g., '2026-01-09T09:00:00Z')"
                )
    elif schedule_type_enum == ScheduleType.RECURRING:
        if not parsed.cron_expression and not parsed.interval_seconds:
            return err("cron_expression or interval_seconds is required for recurring schedules")
    elif schedule_type_enum == ScheduleType.CONDITIONAL:
        if not parsed.condition_expression:
            return err("condition_expression is required for conditional schedules")
    
    # Parse scheduled_at if provided
    scheduled_at_dt = None
    if parsed.scheduled_at:
        try:
            scheduled_at_dt = datetime.fromisoformat(parsed.scheduled_at.replace('Z', '+00:00'))
        except Exception:
            pass  # Will be handled by ScheduleCommand validation
    
    try:
        # Import here to avoid circular dependencies at module level
        from motet.core.commands.builtin.schedule import ScheduleCommandService
        from ...workers import global_invoker
        
        # Initialize global invoker if needed
        global_invoker.initialize()
        
        schedule_command = ScheduleCommandService.create_schedule(
            task_id=task_id,
            target_command_type=parsed.command_type,
            target_command_data=parsed.command_data,
            schedule_type=parsed.schedule_type,
            name=parsed.name,
            scheduled_at=scheduled_at_dt,
            delay_seconds=parsed.delay_seconds,
            cron_expression=parsed.cron_expression,
            interval_seconds=parsed.interval_seconds,
            condition_expression=parsed.condition_expression,
            timeout_seconds=parsed.timeout_seconds,
            priority=parsed.priority,
            max_retries=parsed.max_retries,
            target_worker_id=parsed.target_worker_id,
            preferred_worker_ids=parsed.preferred_worker_ids or [],
            worker_affinity=parsed.worker_affinity,
            avoid_worker_ids=parsed.avoid_worker_ids or [],
            schedule_context=schedule_context,
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            principal_id=principal_id
        )
        
        # Execute the schedule command (synchronous)
        # execute_command returns the result from _do_execute directly
        result = global_invoker.execute_command(schedule_command)

        from motet.core.commands.response_models import (
            parse_command_envelope,
            strip_transport_envelope,
        )
        from pydantic import ValidationError

        payload = strip_transport_envelope(result)
        schedule_id = None
        error_msg = None
        try:
            envelope = parse_command_envelope(payload)
        except (ValidationError, TypeError, ValueError):
            if isinstance(payload, dict):
                schedule_id = payload.get("schedule_id")
                if payload.get("status") == "error" and not schedule_id:
                    err_info = payload.get("error") or {}
                    error_msg = (
                        err_info.get("message")
                        if isinstance(err_info, dict)
                        else str(err_info or "Failed to schedule command")
                    )
            else:
                error_msg = f"Unexpected result type: {type(payload)}"
        else:
            if envelope.status == "error":
                error_msg = (
                    envelope.error.message
                    if envelope.error
                    else "Failed to schedule command"
                )
            else:
                data = envelope.data
                if isinstance(data, dict):
                    schedule_id = data.get("schedule_id")
                elif isinstance(payload, dict):
                    schedule_id = payload.get("schedule_id")
        
        # If we have an error, return it
        if error_msg:
            return err(error_msg)
        
        # If no schedule_id found, try to get it from the command context as fallback
        # (The schedule_id is stored in target_command.distributed_context.schedule_id, not schedule_command)
        if not schedule_id:
            # Try to get from schedule_command context (unlikely but worth checking)
            schedule_id = getattr(schedule_command.distributed_context, "schedule_id", None)
        
        if not schedule_id:
            # Log the actual result for debugging
            import structlog
            logger = structlog.get_logger(__name__)
            logger.error("Schedule command executed but no schedule_id returned",
                        result_type=type(result).__name__,
                        result_keys=list(result.keys()) if isinstance(result, dict) else None,
                        result_preview=str(result)[:500] if result else None,
                        result_str=str(result))
            return err(f"Schedule command executed but no schedule_id returned. Result type: {type(result).__name__}, keys: {list(result.keys()) if isinstance(result, dict) else 'N/A'}, preview: {str(result)[:500]}")
        
        return ok({
            "schedule_id": schedule_id,
            "command_type": parsed.command_type,
            "schedule_type": parsed.schedule_type,
            "name": parsed.name,
            "message": f"Command scheduled successfully with ID: {schedule_id}"
        })
        
    except Exception as exc:
        return err(f"failed to schedule command: {str(exc)}")


def _parse(ln: str, trig: str) -> Dict[str, Any]:
    """Parse natural language input into structured parameters."""
    text = ln[len(trig):].strip()
    params: Dict[str, Any] = {}
    
    # Simple parsing: extract key=value pairs
    # For complex scheduling, users should use structured parameters
    if "command_type=" in text:
        parts = text.split()
        for part in parts:
            if "=" in part:
                k, v = part.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k == "command_type":
                    params["command_type"] = v
                elif k == "schedule_type":
                    params["schedule_type"] = v
                elif k == "cron":
                    params["cron_expression"] = v
                elif k == "at":
                    params["scheduled_at"] = v
                elif k == "name":
                    params["name"] = v
    
    return params


def _fmt(res: Dict[str, Any]) -> str:
    """Format result for observation."""
    if "error" in res:
        return f"schedule_command(error={res['error']})"
    
    schedule_id = res.get("schedule_id", "unknown")
    schedule_type = res.get("schedule_type", "unknown")
    return f"schedule_command(id={schedule_id}, type={schedule_type})"


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.schedule_command",
        description=(
            "Schedule a Motet command to run later (delayed one-time, recurring, or conditional). "
            "This is Motet time-based command scheduling, NOT Google Workspace tasks. "
            "Required: command_type (string), command_data (object — always include it), schedule_type (string). "
            "command_data must match the target command's schema; unknown fields are rejected. "
            "To schedule a chat-style agent turn, set command_type='core.agent_turn' and "
            "command_data={'messages': [{'role': 'user', 'content': '<text>'}]} (a list — not a bare 'message' string). "
            "For delayed schedules: provide delay_seconds (relative, preferred for 'in N seconds/minutes') "
            "OR scheduled_at (absolute ISO 8601), not both. "
            "Optional: context (object) and, for recurring schedules, interval_seconds. "
            "To schedule a tool, set command_type='core.tool_execution' and command_data with a canonical dotted tool_name plus its parameters. "
            "To schedule a workflow_* tool, set command_type='core.workflow_execution' and command_data with workflow_id (the name without the 'workflow_' prefix) plus a context object. "
            "Schedules are immutable once created: to change one, delete it via manage_schedule and create a new one. "
            "If unsure of the command_data shape for a command_type, call command_describe first to get its schema."
        ),
        func=run,
        tool_schema=ScheduleCommandParams,
        triggers=["schedule:", "schedule_command:", "schedule_task:"],  # Keep schedule_task: for backward compatibility
        priority=5,
        parse_params=_parse,
        observation_formatter=_fmt,
        category="system",
        default_timeout_seconds=10.0,
        suggested_max_calls=10,
        cost_class="low",
        keywords=[
            "schedule",
            "scheduling",
            "cron",
            "recurring",
            "delayed",
            "delay_seconds",
            "periodic",
            "automation",
            "timer",
            "future execution",
            "time-based",
            "in N seconds",
        ],
    )


__all__ = ["register"]
