"""
Motet - Distributed Scheduling Commands

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Distributed scheduling command system for the Motet distributed framework.
    Provides unified distributed commands for scheduling other distributed commands
    for future execution. Includes cron-based scheduling, worker targeting,
    and comprehensive schedule management with proper command registration.

Dependencies:
    - datetime: Time and date handling for scheduling
    - time: Timestamp management
    - typing: Type hints and annotations
    - Distributed command system
    - Scheduled command manager

Usage:
    from motet.core.commands.builtin.schedule import ScheduleCommand
    
    # Schedule a command
    command = ScheduleCommand(
        task_id="schedule_123",
        data=ScheduleData(
            target_command_type="reasoning",
            target_command_data={"strategy": "auto"},
            cron_expression="0 9 * * *"
        )
    )
    result = await command.execute()

Notes:
    - Supports scheduling of any distributed command type for future execution
    - Immediate schedules dispatch the target fire-and-forget (issue #129):
      the create call returns the schedule_id promptly instead of blocking on
      target completion (long targets such as core.workflow_execution)
    - Includes cron-based scheduling with flexible time expressions
    - Provides worker targeting and affinity management
    - Supports command registration and type validation
    - Includes comprehensive schedule management and monitoring
    - Integrates with distributed worker routing and capability management
    - Supports schedule persistence and execution coordination
"""


from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Type
from datetime import datetime, timedelta, timezone

import structlog

from motet.core.commands.distributed import DistributedCommand, DistributedCommandContext
from motet.core.constants import CELERY_PROCESS_COMMAND_TASK

from motet.core.commands.capabilities import WorkerCapability
from motet.core.commands.command_data_classes import ScheduleData
from motet.core.workers.observers import EventPriority

logger = structlog.get_logger(__name__)


def resolve_delayed_scheduled_at(
    scheduled_at: Optional[datetime],
    delay_seconds: Optional[int],
    *,
    now: Optional[datetime] = None,
) -> Optional[datetime]:
    """Resolve absolute ``scheduled_at`` for delayed schedules.

    ``scheduled_at`` wins when already provided. Otherwise ``delay_seconds`` is
    applied relative to ``now`` (UTC). Raises ValueError for non-positive delays.
    """
    if scheduled_at is not None:
        return scheduled_at
    if delay_seconds is None:
        return None
    if delay_seconds <= 0:
        raise ValueError("delay_seconds must be a positive integer for delayed schedules")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current + timedelta(seconds=int(delay_seconds))


class ScheduleCommand(DistributedCommand):
    """Schedule a Motet command for later or recurring execution: delay, cron-like recurrence, or conditional runs; list and manage schedules."""
    
    def __init__(self, task_id: str, data: Any, **distributed_kwargs):
        super().__init__(task_id, data, **distributed_kwargs)
    
    def _get_default_timeout(self) -> int:
        return 30  # Schedule operations should be fast
    
    def _get_default_priority(self) -> int:
        return EventPriority.NORMAL
    
    def _setup_command_specifics(self):
        """Set up command-specific configuration"""
        # Schedule commands need MODEL_INFERENCE capability for the target command
        self.distributed_context.required_capabilities = {WorkerCapability.MODEL_INFERENCE}
        
        # Set schedule type from data (needed for proper scheduling)
        if hasattr(self.data, 'schedule_type') and self.data.schedule_type:
            from motet.core.commands.distributed import ScheduleType
            if isinstance(self.data.schedule_type, str):
                self.distributed_context.schedule_type = ScheduleType(self.data.schedule_type)
            else:
                self.distributed_context.schedule_type = self.data.schedule_type
        
        # Worker targeting on ScheduleData applies to the *scheduled target*
        # command (applied in _do_execute), not to this ScheduleCommand itself.
        # Copying target_worker_id onto our distributed_context would route
        # schedule *creation* onto an edge worker (multi-app builders), where
        # it often hits TimeLimitExceeded(30). Creation stays on cloud;
        # preferred/avoid/affinity may still guide create when useful.
        if hasattr(self.data, 'preferred_worker_ids') and self.data.preferred_worker_ids:
            self.distributed_context.preferred_worker_ids = self.data.preferred_worker_ids
        if hasattr(self.data, 'worker_affinity') and self.data.worker_affinity:
            self.distributed_context.worker_affinity = self.data.worker_affinity
        if hasattr(self.data, 'avoid_worker_ids') and self.data.avoid_worker_ids:
            self.distributed_context.avoid_worker_ids = self.data.avoid_worker_ids
    
    def _do_execute(self, worker_context: Dict[str, Any]) -> Any:
        """Execute the schedule command"""
        try:
            # Import the schedule manager
            from motet.core.orchestration.scheduling.manager import ScheduledCommandManager
            from motet.core.commands.distributed import DistributedCommand
            from motet.core.commands.command_data_classes import create_command_data
            from uuid import uuid4
            
            # Resolve command type: accept bare names (e.g. "agent_turn") by trying "core." prefix
            # so that schedule_command tool and API can use either form.
            from motet.core.commands.command_type_registry import command_type_registry
            target_command_type = self.data.target_command_type
            if not target_command_type.startswith("core.") and not command_type_registry.get(target_command_type):
                target_command_type = "core." + target_command_type

            schedule_context = self.data.schedule_context or {}

            target_command_data = create_command_data(
                target_command_type,
                **dict(self.data.target_command_data or {})
            )
            
            # Create the specific command type to be scheduled
            from motet.core.commands.distributed import DistributedCommand
            
            # Ensure all command types are registered
            DistributedCommand._ensure_commands_registered()
            
            # Get the command class for the target command type from new CommandTypeRegistry
            registration = command_type_registry.get(target_command_type)
            if not registration:
                available_types = command_type_registry.get_command_types()
                raise ValueError(f"Unknown command type: {target_command_type}. Available types: {', '.join(available_types)}")
            command_class = registration.implementation
            
            # Create the target command using the specific command class
            target_command = command_class(
                task_id=str(uuid4()),
                data=target_command_data,
                conversation_id=self.distributed_context.conversation_id,
                tenant_id=self.distributed_context.tenant_id,
                principal_id=self.distributed_context.principal_id,
                trace_id=self.distributed_context.trace_id,
                timeout_seconds=self.data.timeout_seconds,
                priority=self.data.priority,
                max_retries=self.data.max_retries,
                # Worker targeting
                target_worker_id=self.data.target_worker_id,
                preferred_worker_ids=self.data.preferred_worker_ids,
                worker_affinity=self.data.worker_affinity,
                avoid_worker_ids=self.data.avoid_worker_ids
            )

            # Write schedule_context into envelope metadata so scheduled runs retain
            # agent_id, surface_id, principal_roles, model_*, enable_thinking, etc.
            if schedule_context:
                target_meta = dict(target_command.distributed_context.metadata or {})
                target_meta.update(schedule_context)
                target_command.distributed_context.metadata = target_meta
            
            # Set scheduling parameters on the target command context
            target_command.distributed_context.schedule_type = self.data.schedule_type
            # Use name from ScheduleData if available, otherwise fall back to context
            schedule_name = self.data.name if self.data.name else self.distributed_context.schedule_name
            target_command.distributed_context.schedule_name = schedule_name
            delay_seconds = getattr(self.data, "delay_seconds", None)
            scheduled_at = resolve_delayed_scheduled_at(
                self.data.scheduled_at,
                delay_seconds,
            )
            if scheduled_at is not None and self.data.scheduled_at is None and delay_seconds is not None:
                logger.info(
                    "schedule_command_resolved_delay_seconds",
                    delay_seconds=delay_seconds,
                    scheduled_at=scheduled_at.isoformat(),
                )
            if scheduled_at:
                target_command.distributed_context.scheduled_at = scheduled_at
            if self.data.cron_expression:
                target_command.distributed_context.cron_expression = self.data.cron_expression
            if self.data.interval_seconds:
                target_command.distributed_context.interval_seconds = self.data.interval_seconds
            if self.data.condition_expression:
                target_command.distributed_context.condition_expression = self.data.condition_expression
            
            # Schedule the command
            schedule_manager = ScheduledCommandManager()
            schedule_id = schedule_manager.schedule_command(target_command)
            
            # If immediate execution, execute the command right away
            # Check if schedule_type is "immediate" (either enum or string)
            is_immediate = False
            if isinstance(self.data.schedule_type, str):
                is_immediate = self.data.schedule_type.lower() == "immediate"
            else:
                from motet.core.commands.distributed import ScheduleType
                is_immediate = self.data.schedule_type == ScheduleType.IMMEDIATE
            
            execution_info: Optional[Dict[str, Any]] = None
            if is_immediate:
                # Fire-and-forget dispatch (issue #129). The previous behavior
                # executed the target synchronously via
                # global_invoker.execute_command(), blocking this 30s-limited
                # core.schedule task until the target finished — long targets
                # (e.g. core.workflow_execution build cycles) hit
                # TimeLimitExceeded(30,) even though the target succeeded.
                execution_info = self._dispatch_immediate(target_command, schedule_id)

            result: Dict[str, Any] = {
                "status": "success",
                "schedule_id": schedule_id,
                "target_command_type": self.data.target_command_type,
                "schedule_type": self.data.schedule_type,
                "message": f"Command scheduled successfully with ID: {schedule_id}"
            }
            if execution_info is not None:
                result["execution"] = execution_info
            return result
            
        except Exception as e:
            return {
                "status": "error",
                "error": str(e),
                "message": f"Failed to schedule command: {str(e)}"
            }

    def _dispatch_immediate(
        self, target_command: DistributedCommand, schedule_id: str
    ) -> Dict[str, Any]:
        """Dispatch an immediate schedule's target fire-and-forget (issue #129).

        Routes the target through the worker router (honoring
        ``target_worker_id`` and identity-scoped edge affinity), enqueues it
        with its own per-command time limits, and returns dispatch info
        without waiting for completion. The schedule record is then marked
        COMPLETED — matching ``check_delayed_schedules`` semantics — so the
        beat's delayed/immediate check does not dispatch it a second time.

        Raises RuntimeError (failing the schedule call loudly) when no
        eligible worker exists: an immediate schedule whose target cannot be
        enqueued has not done its job.
        """
        from motet.core.workers import global_invoker

        global_invoker.initialize()
        # The router lives on the invoker's primary node (facade delegates).
        node = getattr(global_invoker, "primary_node", None) or global_invoker
        router = getattr(node, "worker_router", None)
        if router is None:
            raise RuntimeError(
                f"Immediate schedule {schedule_id}: worker router unavailable for dispatch"
            )

        routing_decision = router.route_command(
            target_command, target_worker_id=self.data.target_worker_id
        )
        selected_worker = routing_decision.selected_worker
        if not selected_worker or not selected_worker.get("worker_id"):
            raise RuntimeError(
                f"Immediate schedule {schedule_id}: no eligible worker for "
                f"{target_command.get_command_type()} "
                f"({routing_decision.error or 'no suitable workers available'})"
            )
        worker_id = selected_worker["worker_id"]

        # Same per-command time-limit handling as WorkerCommunicator.send_command:
        # override Celery's global 300s limit so long targets (e.g. builder
        # workflows) are not hard-killed early.
        timeout = getattr(target_command.distributed_context, "timeout_seconds", None)
        try:
            timeout = int(timeout) if timeout is not None else 300
        except (TypeError, ValueError):
            timeout = 300
        if timeout < 1:
            timeout = 300
        soft_time_limit = max(timeout - 60, 1) if timeout > 60 else max(timeout - 1, 1)

        # Hold the same per-schedule lock check_delayed_schedules uses: the
        # record was just stored ACTIVE with next_execution_at=now, so a beat
        # tick in this window would dispatch the target a second time.
        from motet.core.distributed.redis_manager import acquire_distributed_lock_sync

        schedule_lock = acquire_distributed_lock_sync(
            client_id="schedule_executor",
            lock_key=f"lock:schedule:{schedule_id}",
            ttl_seconds=30,
        )
        if not schedule_lock:
            # The beat's delayed/immediate check claimed the schedule first;
            # it will dispatch and mark it COMPLETED. Do not dispatch twice.
            logger.info(
                "immediate_schedule_claimed_by_scheduler",
                schedule_id=schedule_id,
                command_type=target_command.get_command_type(),
            )
            return {
                "dispatched": True,
                "target_command_id": target_command.command_id,
                "worker_id": None,
                "note": "Target claimed by the scheduler beat tick (dispatching via check_delayed_schedules)",
            }

        try:
            from motet.core.workers.celery_app import get_celery_app

            queue_name = f"worker.{worker_id}"
            celery_result = get_celery_app().send_task(
                CELERY_PROCESS_COMMAND_TASK,
                args=[target_command.serialize_for_transport()],
                queue=queue_name,
                time_limit=timeout,
                soft_time_limit=soft_time_limit,
            )

            self._mark_immediate_schedule_dispatched(schedule_id)
        finally:
            schedule_lock.release_sync()

        logger.info(
            "immediate_schedule_dispatched",
            schedule_id=schedule_id,
            command_type=target_command.get_command_type(),
            target_command_id=target_command.command_id,
            worker_id=worker_id,
            queue=queue_name,
            celery_task_id=celery_result.id,
            time_limit=timeout,
            soft_time_limit=soft_time_limit,
        )

        return {
            "dispatched": True,
            "target_command_id": target_command.command_id,
            "worker_id": worker_id,
            "queue": queue_name,
            "celery_task_id": celery_result.id,
            "time_limit": timeout,
            "note": "Target dispatched without waiting for completion (fire-and-forget)",
        }

    def _mark_immediate_schedule_dispatched(self, schedule_id: str) -> None:
        """Mark a dispatched immediate schedule COMPLETED (one-shot semantics).

        Without this, the schedule stays ACTIVE with ``next_execution_at``
        already due, and ``check_delayed_schedules`` (beat, every 5s) would
        execute the target a second time.
        """
        from datetime import timezone

        from motet.core.orchestration.scheduling.manager import ScheduledCommandManager
        from motet.core.orchestration.scheduling.models import ScheduleStatus

        manager = ScheduledCommandManager()
        schedule = manager.storage.retrieve_schedule(schedule_id)
        if not schedule:
            logger.warning(
                "immediate_schedule_missing_after_dispatch", schedule_id=schedule_id
            )
            return
        schedule.last_execution_at = datetime.now(timezone.utc)
        schedule.execution_count += 1
        schedule.status = ScheduleStatus.COMPLETED
        schedule.next_execution_at = None
        if not manager.storage.update_schedule(schedule):
            logger.error(
                "immediate_schedule_completion_update_failed", schedule_id=schedule_id
            )

    def get_command_type(self) -> str:
        """Return the command type identifier"""
        return "core.schedule"
    
    @classmethod
    def _get_data_class(cls) -> Type[ScheduleData]:
        """Return the data dataclass for this command"""
        return ScheduleData
    
    def can_undo(self) -> bool:
        """Schedule commands cannot be undone"""
        return False
    
    def undo(self, stack) -> Any:
        """Schedule commands cannot be undone"""
        return {"error": "Schedule commands cannot be undone"}
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert command to dictionary for serialization"""
        base_dict = super().to_dict()
        base_dict.update({
            "target_command_type": self.data.target_command_type,
            "target_command_data": self.data.target_command_data,
            "name": self.data.name,  # Include name in serialization
            "schedule_type": self.data.schedule_type,
            "scheduled_at": (
                self.data.scheduled_at.isoformat() if self.data.scheduled_at else None
            ),
            "delay_seconds": getattr(self.data, "delay_seconds", None),
            "cron_expression": self.data.cron_expression,
            "interval_seconds": self.data.interval_seconds,
            "condition_expression": self.data.condition_expression,
            "timeout_seconds": self.data.timeout_seconds,
            "priority": self.data.priority,
            "max_retries": self.data.max_retries,
            "target_worker_id": self.data.target_worker_id,
            "preferred_worker_ids": self.data.preferred_worker_ids,
            "worker_affinity": self.data.worker_affinity,
            "avoid_worker_ids": self.data.avoid_worker_ids
        })
        return base_dict


class ScheduleCommandService:
    """Service for creating schedule commands with the new simplified pattern"""
    
    @staticmethod
    def create_schedule(
        task_id: str,
        target_command_type: str,
        target_command_data: Dict[str, Any],
        schedule_type: str,
        name: Optional[str] = None,
        scheduled_at: Optional[datetime] = None,
        delay_seconds: Optional[int] = None,
        cron_expression: Optional[str] = None,
        interval_seconds: Optional[int] = None,
        condition_expression: Optional[str] = None,
        timeout_seconds: int = 300,
        priority: int = 5,
        max_retries: int = 3,
        target_worker_id: Optional[str] = None,
        preferred_worker_ids: Optional[List[str]] = None,
        worker_affinity: Optional[str] = None,
        avoid_worker_ids: Optional[List[str]] = None,
        schedule_context: Optional[Dict[str, Any]] = None,
        **distributed_params
    ) -> ScheduleCommand:
        """Create a schedule command with the new simplified pattern.
        
        schedule_context: Full execution context dict (agent_id, surface_id, principal_roles,
        model_provider, model_name, model_profile_name, enable_thinking, reasoning_effort, etc.)
        Written to the target command's distributed_context.metadata.
        """
        command_data = ScheduleData(
            target_command_type=target_command_type,
            target_command_data=target_command_data,
            name=name,
            schedule_type=schedule_type,
            scheduled_at=scheduled_at,
            delay_seconds=delay_seconds,
            cron_expression=cron_expression,
            interval_seconds=interval_seconds,
            condition_expression=condition_expression,
            timeout_seconds=timeout_seconds,
            priority=priority,
            max_retries=max_retries,
            target_worker_id=target_worker_id,
            preferred_worker_ids=preferred_worker_ids or [],
            worker_affinity=worker_affinity,
            avoid_worker_ids=avoid_worker_ids or [],
            schedule_context=schedule_context,
        )
        
        return ScheduleCommand(
            task_id=task_id,
            data=command_data,
            schedule_name=name,  # This will be passed to _create_context via **distributed_kwargs
            **distributed_params
        )


__all__ = [
    "ScheduleCommand",
    "ScheduleCommandService",
    "resolve_delayed_scheduled_at",
]

# Register command types with the base class
from motet.core.commands.distributed import DistributedCommand
DistributedCommand.register_command_type(ScheduleCommand)
