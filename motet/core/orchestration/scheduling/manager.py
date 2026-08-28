"""
Motet - Scheduled Command Manager

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Comprehensive scheduled command manager for the Motet distributed framework.
    Manages command scheduling lifecycle, persistence, and execution coordination.
    Includes cron-based scheduling, condition-based execution, worker targeting,
    and comprehensive schedule management with Redis storage.

Dependencies:
    - uuid: Unique identifier generation
    - datetime: Time and date handling for scheduling
    - structlog: Structured logging
    - typing: Type hints and annotations
    - Distributed command system
    - Schedule storage and models

Usage:
    from motet.core.orchestration.scheduling.manager import ScheduledCommandManager
    
    # Create manager
    manager = ScheduledCommandManager(redis_url="redis://localhost:6379/0")
    
    # Schedule command
    schedule_id = manager.schedule_command(command)
    
    # Get scheduled commands
    schedules = manager.get_scheduled_commands()

Notes:
    - Supports comprehensive command scheduling lifecycle management
    - Includes cron-based scheduling with expression validation
    - Provides condition-based execution and recurring schedules
    - Includes worker targeting and affinity management
    - Supports schedule persistence and Redis storage
    - Integrates with distributed command system and execution
    - Includes comprehensive schedule monitoring and management
"""


import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import structlog

from motet.core.commands.distributed import DistributedCommand, ScheduleType
from .models import ScheduleMetadata, ScheduleFilter, ScheduleStatus, ScheduleExecutionResult
from .storage import ScheduleStorage
from .cron_utils import (
    validate_cron_expression, 
    get_next_execution_from_cron, 
    CronExpressionError,
    describe_cron_expression
)

logger = structlog.get_logger(__name__)


class ScheduledCommandManager:
    """Manages command scheduling lifecycle and persistence"""
    
    def __init__(self, redis_url: Optional[str] = None):
        self.storage = ScheduleStorage(redis_url)
    
    def schedule_command(self, command: DistributedCommand) -> str:
        """Schedule a command for future execution"""
        try:
            # Generate unique schedule ID
            schedule_id = str(uuid.uuid4())
            
            # Create schedule metadata
            # Convert schedule_type string to enum if needed
            schedule_type = command.distributed_context.schedule_type
            if isinstance(schedule_type, str):
                from motet.core.commands.distributed import ScheduleType
                schedule_type = ScheduleType(schedule_type)
            
            # Validate cron expression if provided
            cron_expression = command.distributed_context.cron_expression
            if cron_expression and not validate_cron_expression(cron_expression):
                raise ValueError(f"Invalid cron expression: {cron_expression}")
            
            # Serialize command data to dict to ensure proper JSON serialization
            # command.data is a Pydantic model that may contain datetime objects
            # Use mode='json' to automatically convert datetime objects to ISO strings
            command_data_dict = command.data.model_dump(mode='json') if hasattr(command.data, 'model_dump') else command.data
            
            schedule = ScheduleMetadata(
                schedule_id=schedule_id,
                command_id=command.command_id,
                command_type=command.get_command_type(),
                name=command.distributed_context.schedule_name,
                schedule_type=schedule_type,
                scheduled_at=command.distributed_context.scheduled_at,
                cron_expression=cron_expression,
                recurring_until=command.distributed_context.recurring_until,
                condition_check_interval=command.distributed_context.condition_check_interval,
                condition_expression=command.distributed_context.condition_expression,
                max_executions=command.distributed_context.max_executions,
                tenant_id=command.distributed_context.tenant_id,
                motet_id=command.distributed_context.motet_id,
                created_by=command.distributed_context.principal_id,
                # Worker targeting (ADR-0025 enhancement)
                target_worker_id=command.distributed_context.target_worker_id,
                preferred_worker_ids=command.distributed_context.preferred_worker_ids.copy(),
                worker_affinity=command.distributed_context.worker_affinity,
                avoid_worker_ids=command.distributed_context.avoid_worker_ids.copy() if hasattr(command.distributed_context, 'avoid_worker_ids') else [],
                # Additional scheduling parameters
                interval_seconds=command.distributed_context.interval_seconds,
                max_retries=getattr(command.data, 'max_retries', 3) if hasattr(command.data, 'max_retries') else 3,
                timeout_seconds=getattr(command.data, 'timeout_seconds', 300) if hasattr(command.data, 'timeout_seconds') else 300,
                priority=getattr(command.data, 'priority', 5) if hasattr(command.data, 'priority') else 5,
                metadata={
                    "original_command_data": command_data_dict,  # Now properly serialized as dict
                    "distributed_context": command.distributed_context.model_dump(mode='json')  # Serialize datetime objects to ISO strings
                }
            )
            
            # Calculate next execution time
            schedule.next_execution_at = self._calculate_next_execution(schedule)
            
            # Store the schedule
            success = self.storage.store_schedule(schedule)
            if not success:
                raise RuntimeError("Failed to store schedule in Redis")
            
            # Update command context with schedule ID
            command.distributed_context.schedule_id = schedule_id
            
            logger.info("Command scheduled successfully",
                       schedule_id=schedule_id,
                       command_id=command.command_id,
                       schedule_type=schedule.schedule_type,
                       next_execution_at=schedule.next_execution_at,
                       principal_id=schedule.created_by,
                       tenant_id=schedule.tenant_id,
                       motet_id=schedule.motet_id)
            
            return schedule_id
            
        except Exception as e:
            logger.error("Failed to schedule command",
                        command_id=command.command_id,
                        error=str(e), exc_info=True)
            raise RuntimeError(f"Failed to schedule command: {e}") from e
    
    
    def cancel_schedule(self, schedule_id: str) -> bool:
        """Cancel a scheduled command"""
        try:
            schedule = self.storage.retrieve_schedule(schedule_id)
            if not schedule:
                logger.warning("Schedule not found for cancellation",
                              schedule_id=schedule_id)
                return False
            
            # Update status to cancelled
            schedule.status = ScheduleStatus.CANCELLED
            
            # Update in storage
            success = self.storage.update_schedule(schedule)
            if success:
                logger.info("Schedule cancelled successfully",
                           schedule_id=schedule_id)
            else:
                logger.error("Failed to update schedule status",
                            schedule_id=schedule_id)
            
            return success
            
        except Exception as e:
            logger.error("Failed to cancel schedule",
                        schedule_id=schedule_id,
                        error=str(e), exc_info=True)
            return False
    
    def delete_schedule(self, schedule_id: str) -> bool:
        """Permanently delete a scheduled command"""
        try:
            # Check if schedule exists
            schedule = self.storage.retrieve_schedule(schedule_id)
            if not schedule:
                logger.warning("Schedule not found for deletion",
                              schedule_id=schedule_id)
                return False
            
            # Delete from storage
            success = self.storage.delete_schedule(schedule_id)
            if success:
                logger.info("Schedule deleted successfully",
                           schedule_id=schedule_id)
            else:
                logger.error("Failed to delete schedule from storage",
                            schedule_id=schedule_id)
            
            return success
            
        except Exception as e:
            logger.error("Failed to delete schedule",
                        schedule_id=schedule_id,
                        error=str(e), exc_info=True)
            return False
    
    def suspend_schedule(self, schedule_id: str) -> bool:
        """Suspend a scheduled command (pause execution)"""
        try:
            schedule = self.storage.retrieve_schedule(schedule_id)
            if not schedule:
                logger.warning("Schedule not found for suspension",
                              schedule_id=schedule_id)
                return False
            
            # Only suspend if currently active
            if schedule.status != ScheduleStatus.ACTIVE:
                logger.warning("Cannot suspend schedule - not active",
                              schedule_id=schedule_id,
                              current_status=schedule.status)
                return False
            
            # Update status to paused
            schedule.status = ScheduleStatus.PAUSED
            
            # Update in storage
            success = self.storage.update_schedule(schedule)
            if success:
                logger.info("Schedule suspended successfully",
                           schedule_id=schedule_id)
            else:
                logger.error("Failed to update schedule status to paused",
                            schedule_id=schedule_id)
            
            return success
            
        except Exception as e:
            logger.error("Failed to suspend schedule",
                        schedule_id=schedule_id,
                        error=str(e), exc_info=True)
            return False
    
    def resume_schedule(self, schedule_id: str) -> bool:
        """Resume a suspended scheduled command"""
        try:
            schedule = self.storage.retrieve_schedule(schedule_id)
            if not schedule:
                logger.warning("Schedule not found for resumption",
                              schedule_id=schedule_id)
                return False
            
            # Only resume if currently paused
            if schedule.status != ScheduleStatus.PAUSED:
                logger.warning("Cannot resume schedule - not paused",
                              schedule_id=schedule_id,
                              current_status=schedule.status)
                return False
            
            # Update status to active
            schedule.status = ScheduleStatus.ACTIVE
            
            # Recalculate next execution time if needed
            if not schedule.next_execution_at:
                schedule.next_execution_at = self._calculate_next_execution(schedule)
            
            # Update in storage
            success = self.storage.update_schedule(schedule)
            if success:
                logger.info("Schedule resumed successfully",
                           schedule_id=schedule_id)
            else:
                logger.error("Failed to update schedule status to active",
                            schedule_id=schedule_id)
            
            return success
            
        except Exception as e:
            logger.error("Failed to resume schedule",
                        schedule_id=schedule_id,
                        error=str(e), exc_info=True)
            return False
    
    def modify_schedule(self, schedule_id: str, updates: Dict[str, Any]) -> bool:
        """Modify an existing schedule"""
        try:
            schedule = self.storage.retrieve_schedule(schedule_id)
            if not schedule:
                logger.warning("Schedule not found for modification",
                              schedule_id=schedule_id)
                return False
            
            # Apply updates
            for key, value in updates.items():
                if hasattr(schedule, key):
                    setattr(schedule, key, value)
                else:
                    logger.warning("Invalid schedule field for update",
                                  schedule_id=schedule_id,
                                  field=key)
            
            # Recalculate next execution time if schedule parameters changed
            if any(key in updates for key in ['scheduled_at', 'cron_expression', 'recurring_until']):
                schedule.next_execution_at = self._calculate_next_execution(schedule)
            
            # Update in storage
            success = self.storage.update_schedule(schedule)
            if success:
                logger.info("Schedule modified successfully",
                           schedule_id=schedule_id,
                           updates=list(updates.keys()))
            else:
                logger.error("Failed to update schedule",
                            schedule_id=schedule_id)
            
            return success
            
        except Exception as e:
            logger.error("Failed to modify schedule",
                        schedule_id=schedule_id,
                        error=str(e), exc_info=True)
            return False
    
    def list_schedules(self, filters: Optional[ScheduleFilter] = None) -> List[ScheduleMetadata]:
        """List active schedules with optional filtering"""
        try:
            return self.storage.list_schedules(filters)
        except Exception as e:
            logger.error("Failed to list schedules",
                        error=str(e), exc_info=True)
            return []
    
    def get_schedule(self, schedule_id: str) -> Optional[ScheduleMetadata]:
        """Get a specific schedule by ID"""
        try:
            return self.storage.retrieve_schedule(schedule_id)
        except Exception as e:
            logger.error("Failed to get schedule",
                        schedule_id=schedule_id,
                        error=str(e), exc_info=True)
            return None
    
    def get_schedules_ready_for_execution(self) -> List[ScheduleMetadata]:
        """Get schedules that are ready for execution"""
        try:
            # Get all active schedules
            filters = ScheduleFilter(status=ScheduleStatus.ACTIVE)
            schedules = self.storage.list_schedules(filters)
            
            # Filter for schedules ready to execute
            ready_schedules = []
            # Use timezone-aware datetime for comparison to avoid naive/aware datetime errors
            from datetime import timezone
            now = datetime.now(timezone.utc)
            
            for schedule in schedules:
                # Convert next_execution_at to timezone-aware if it's naive
                next_exec = schedule.next_execution_at
                if next_exec and next_exec.tzinfo is None:
                    next_exec = next_exec.replace(tzinfo=timezone.utc)
                
                if schedule.should_execute() and next_exec and next_exec <= now:
                    ready_schedules.append(schedule)
            
            return ready_schedules
            
        except Exception as e:
            logger.error("Failed to get schedules ready for execution",
                        error=str(e), exc_info=True)
            return []
    
    def _calculate_next_execution(self, schedule: ScheduleMetadata) -> Optional[datetime]:
        """Calculate the next execution time for a schedule"""
        try:
            from datetime import timezone
            if schedule.schedule_type == ScheduleType.IMMEDIATE:
                return datetime.now(timezone.utc)
            
            elif schedule.schedule_type == ScheduleType.DELAYED:
                return schedule.scheduled_at
            
            elif schedule.schedule_type == ScheduleType.RECURRING:
                if schedule.cron_expression:
                    try:
                        # Use proper cron parsing to calculate next execution
                        base_time = schedule.last_execution_at or datetime.now(timezone.utc)
                        next_execution = get_next_execution_from_cron(schedule.cron_expression, base_time)
                        
                        logger.debug("Calculated next execution from cron",
                                   schedule_id=schedule.schedule_id,
                                   cron_expression=schedule.cron_expression,
                                   base_time=base_time.isoformat(),
                                   next_execution=next_execution.isoformat() if next_execution else None)
                        
                        return next_execution
                    except CronExpressionError as e:
                        logger.error("Invalid cron expression in schedule",
                                   schedule_id=schedule.schedule_id,
                                   cron_expression=schedule.cron_expression,
                                   error=str(e))
                        return None
                elif schedule.interval_seconds:
                    # For interval-based recurring schedules
                    if schedule.last_execution_at:
                        # Calculate next execution based on last execution + interval
                        return schedule.last_execution_at + timedelta(seconds=schedule.interval_seconds)
                    else:
                        # First execution - start immediately
                        return datetime.now(timezone.utc)
                return None
            
            elif schedule.schedule_type == ScheduleType.CONDITIONAL:
                # For conditional, next execution is based on check interval
                if schedule.condition_check_interval:
                    return datetime.now(timezone.utc) + timedelta(seconds=schedule.condition_check_interval)
                return None
            
            return None
            
        except Exception as e:
            logger.error("Failed to calculate next execution time",
                        schedule_id=schedule.schedule_id,
                        error=str(e), exc_info=True)
            return None
    
    def get_schedule_description(self, schedule: ScheduleMetadata) -> str:
        """Get a human-readable description of a schedule"""
        try:
            if schedule.schedule_type == ScheduleType.IMMEDIATE:
                return "Execute immediately"
            elif schedule.schedule_type == ScheduleType.DELAYED:
                if schedule.scheduled_at:
                    return f"Execute once at {schedule.scheduled_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
                return "Execute once (delayed)"
            elif schedule.schedule_type == ScheduleType.RECURRING:
                if schedule.cron_expression:
                    cron_desc = describe_cron_expression(schedule.cron_expression)
                    return f"Recurring: {cron_desc}"
                elif schedule.interval_seconds:
                    if schedule.interval_seconds < 60:
                        return f"Every {schedule.interval_seconds} seconds"
                    elif schedule.interval_seconds < 3600:
                        minutes = schedule.interval_seconds // 60
                        return f"Every {minutes} minute{'s' if minutes != 1 else ''}"
                    elif schedule.interval_seconds < 86400:
                        hours = schedule.interval_seconds // 3600
                        return f"Every {hours} hour{'s' if hours != 1 else ''}"
                    else:
                        days = schedule.interval_seconds // 86400
                        return f"Every {days} day{'s' if days != 1 else ''}"
                return "Recurring (no schedule specified)"
            elif schedule.schedule_type == ScheduleType.CONDITIONAL:
                if schedule.condition_expression:
                    return f"When condition met: {schedule.condition_expression}"
                return "Conditional execution"
            else:
                return f"Unknown schedule type: {schedule.schedule_type}"
        except Exception as e:
            logger.debug("Failed to generate schedule description",
                        schedule_id=schedule.schedule_id,
                        error=str(e))
            return f"Schedule type: {schedule.schedule_type}"
    
    def record_execution_result(self, schedule_id: str, result: ScheduleExecutionResult) -> bool:
        """Record the result of a scheduled command execution"""
        try:
            schedule = self.storage.retrieve_schedule(schedule_id)
            if not schedule:
                logger.warning("Schedule not found for execution result recording",
                              schedule_id=schedule_id)
                return False
            
            # Update execution tracking
            if result.success:
                # For RECURRING schedules, don't update last_execution_at here because it's
                # already set correctly in check_recurring_schedules BEFORE execution.
                # Updating it here would overwrite the precise timing with the post-execution time.
                is_recurring = schedule.schedule_type == ScheduleType.RECURRING
                schedule.increment_execution_count(update_last_execution=not is_recurring)
                
                # CRITICAL FIX: Don't change status on successful execution
                # This preserves user-initiated status changes (PAUSED, CANCELLED, etc.)
                # The status should only be changed by explicit user actions or failure conditions
            else:
                schedule.record_failure(result.error or "Unknown error")
                
                # Check if we should mark as failed
                if schedule.consecutive_failures >= schedule.max_consecutive_failures:
                    schedule.status = ScheduleStatus.FAILED
            
            # NOTE: For RECURRING schedules, next_execution_at is already calculated and set
            # in check_recurring_schedules BEFORE execution to prevent race conditions.
            # We do NOT recalculate it here to preserve the lock-based timing guarantees.
            # Only calculate for non-recurring schedules (DELAYED, CONDITIONAL, etc.)
            if (schedule.schedule_type != ScheduleType.RECURRING and 
                schedule.schedule_type == ScheduleType.DELAYED and 
                schedule.status == ScheduleStatus.ACTIVE):
                schedule.next_execution_at = self._calculate_next_execution(schedule)
            
            # Check if schedule is completed
            if schedule.is_expired():
                schedule.status = ScheduleStatus.COMPLETED
            
            # Update in storage
            success = self.storage.update_schedule(schedule)
            if success:
                logger.info("Execution result recorded successfully",
                           schedule_id=schedule_id,
                           success=result.success,
                           execution_count=schedule.execution_count)
            else:
                logger.error("Failed to update schedule after execution",
                            schedule_id=schedule_id)
            
            return success
            
        except Exception as e:
            logger.error("Failed to record execution result",
                        schedule_id=schedule_id,
                        error=str(e), exc_info=True)
            return False
