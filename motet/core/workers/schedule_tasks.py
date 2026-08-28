"""
Motet - Schedule Tasks

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-27

Description:
    Worker schedule tasks for the Motet distributed framework.
    Celery Beat checks (delayed/recurring/conditional) enqueue
    ``imf.commands.schedule`` onto the routed ``command_processing`` queue
    (same path for delayed and recurring — do not hardcode ``queue='celery'``).

Dependencies:
    - typing: Type hints and annotations
    - Celery app task routing (``imf.commands.*`` → ``command_processing``)
    - ScheduledCommandManager for schedule metadata and locking

Usage:
    from motet.core.workers.schedule_tasks import check_delayed_schedules

Notes:
    - Delayed and recurring both use ``schedule_distributed_command.delay(...)``
      so task_routes apply; an explicit ``queue='celery'`` would land on a queue
      no worker consumes.
"""


import json
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, cast

import structlog

from .celery_app import celery_app
from motet.core.commands.distributed import DistributedCommand
from ..orchestration.scheduling import ScheduledCommandManager, ScheduleExecutionResult, ScheduleStatus

logger = structlog.get_logger(__name__)


def _resolve_scheduled_identity(
    schedule_created_by: Optional[str],
    distributed_context: Optional[Dict[str, Any]],
) -> Optional[str]:
    """
    Resolve principal identity for scheduled execution.

    Scheduled runs execute as the principal that created the schedule.
    We treat schedule metadata (`created_by`) as authoritative and require it.
    """
    created_by = (schedule_created_by or "").strip()
    if created_by:
        return created_by
    raise ValueError("Schedule is missing created_by principal_id")


@celery_app.task(name="imf.commands.schedule", bind=True)
def schedule_distributed_command(self, schedule_data: str) -> Dict[str, Any]:
    """
    Execute a scheduled command with schedule tracking.
    
    This task is called by Celery Beat for scheduled executions.
    """
    start_time = time.time()
    celery_task_id = self.request.id  # Celery task ID for this schedule execution
    worker_id = self.request.hostname
    
    # Generate a NEW task_id for each scheduled command execution
    # This ensures each execution has a unique task_id for proper tracking and isolation
    execution_task_id = str(uuid.uuid4())
    
    try:
        logger.info("Executing scheduled command",
                   celery_task_id=celery_task_id,
                   execution_task_id=execution_task_id,
                   worker_id=worker_id)
        
        # Parse schedule data
        import json
        if isinstance(schedule_data, str):
            schedule_info = json.loads(schedule_data)
        else:
            schedule_info = schedule_data
        schedule_id = schedule_info.get("schedule_id")
        original_command_data = schedule_info.get("command_data")  # This is actually original_command_data
        distributed_context = schedule_info.get("distributed_context", {})
        
        if not schedule_id or not original_command_data:
            return {
                "status": "error",
                "error": "Invalid schedule data - missing schedule_id or command_data",
                "execution_time_ms": int((time.time() - start_time) * 1000),
                "worker_id": worker_id,
                "task_id": execution_task_id
            }
        
        # Get schedule manager
        schedule_manager = ScheduledCommandManager()
        
        # Retrieve the schedule
        schedule = schedule_manager.get_schedule(schedule_id)
        if not schedule:
            return {
                "status": "error",
                "error": f"Schedule not found: {schedule_id}",
                "execution_time_ms": int((time.time() - start_time) * 1000),
                "worker_id": worker_id,
                "task_id": execution_task_id
            }
        
        # Check if schedule should still execute
        if not schedule.should_execute():
            logger.info("Schedule should not execute, skipping",
                       schedule_id=schedule_id,
                       status=schedule.status)
            return {
                "status": "skipped",
                "reason": f"Schedule status: {schedule.status}",
                "execution_time_ms": int((time.time() - start_time) * 1000),
                "worker_id": worker_id,
                "task_id": execution_task_id
            }
        
        # Deserialize the command
        try:
            # Reconstruct the complete command transport object
            if isinstance(original_command_data, dict):
                # Option A transport format: envelope + payload (no legacy fallback).
                # - envelope: execution context (routing/identity/tracing)
                # - payload: command's Pydantic data model fields
                
                # Start with distributed_context to preserve original context
                envelope: Dict[str, Any] = {}
                if isinstance(distributed_context, dict):
                    envelope.update(distributed_context)
                
                # CRITICAL: Override with fresh values for this execution
                # Generate new task_id for each execution to ensure proper tracking
                envelope["command_type"] = schedule.command_type  # From schedule metadata
                envelope["task_id"] = execution_task_id  # NEW task_id for each execution

                tenant_id_overwrite = (
                    (distributed_context.get("tenant_id") if isinstance(distributed_context, dict) else None)
                    or schedule.tenant_id
                )
                principal_id_overwrite = _resolve_scheduled_identity(
                    schedule.created_by,
                    distributed_context if isinstance(distributed_context, dict) else None,
                )
                if tenant_id_overwrite:
                    envelope["tenant_id"] = tenant_id_overwrite
                if principal_id_overwrite:
                    envelope["principal_id"] = principal_id_overwrite
                # Ensure a non-empty motet_id for ADR-0056 isolation
                envelope["motet_id"] = (envelope.get("motet_id") or "default")
                
                # CRITICAL: Ensure conversation_id is preserved from distributed_context
                # This is essential for workflows and tools that require conversation_id (e.g., MCP Playwright)
                # If conversation_id is missing from distributed_context, generate one for scheduled commands
                if not envelope.get("conversation_id"):
                    envelope["conversation_id"] = str(uuid.uuid4())
                    logger.info("Generated conversation_id for scheduled command",
                               schedule_id=schedule_id,
                               conversation_id=envelope["conversation_id"])

                command_data_json = json.dumps(
                    {
                        "envelope": envelope,
                        "payload": original_command_data,
                    }
                )
            else:
                command_data_json = original_command_data
            command = DistributedCommand.deserialize_from_transport(command_data_json)
            logger.info("Deserialized scheduled command",
                       schedule_id=schedule_id,
                       command_type=command.get_command_type(),
                       command_id=command.command_id)
        except Exception as e:
            error_msg = f"Failed to deserialize command: {str(e)}"
            logger.error(error_msg, schedule_id=schedule_id, exc_info=True)
            
            # Record the failure
            execution_result = ScheduleExecutionResult(
                schedule_id=schedule_id,
                execution_id=str(uuid.uuid4()),
                success=False,
                error=error_msg,
                execution_time_ms=int((time.time() - start_time) * 1000),
                worker_id=worker_id,
                schedule_status=schedule.status,
                execution_count=schedule.execution_count,
                consecutive_failures=schedule.consecutive_failures + 1
            )
            schedule_manager.record_execution_result(schedule_id, execution_result)
            
            return {
                "status": "error",
                "error": error_msg,
                "execution_time_ms": int((time.time() - start_time) * 1000),
                "worker_id": worker_id,
                "task_id": execution_task_id
            }
        
        # Apply worker targeting from schedule to command context
        if schedule.target_worker_id:
            command.distributed_context.target_worker_id = schedule.target_worker_id
        if schedule.preferred_worker_ids:
            command.distributed_context.preferred_worker_ids = schedule.preferred_worker_ids.copy()
        if schedule.worker_affinity:
            command.distributed_context.worker_affinity = schedule.worker_affinity
        if schedule.avoid_worker_ids:
            command.distributed_context.avoid_worker_ids = schedule.avoid_worker_ids.copy()
        
        # Execute the command using the distributed invoker to respect worker targeting
        try:
            from .tasks import _create_worker_context
            from .invoker_context import set_worker_context, get_distributed_invoker, set_current_command_id, clear_current_command_id
            
            # Get worker context for command execution
            worker_context = _create_worker_context()
            
            # Set the worker context for this execution thread
            set_worker_context(worker_context)
            
            # Set the current command ID for parent tracking
            set_current_command_id(command.command_id)
            
            try:
                # Get the distributed invoker from worker context
                invoker = get_distributed_invoker()
                
                # Execute the command with proper worker targeting
                execution_start = time.time()
                result = invoker.execute_command(command=command)
                execution_time_ms = int((time.time() - execution_start) * 1000)
            finally:
                # Always clear the command context when done
                clear_current_command_id()
            
            # Convert result to expected format
            if isinstance(result, dict):
                execution_result_dict = result
            else:
                execution_result_dict = {
                    "status": "success",
                    "result": result,
                    "worker_id": schedule.target_worker_id or "unknown"
                }
            
            # Record execution result
            # Check for successful execution - DistributedInvokerNode returns "completed" for success
            is_successful = execution_result_dict.get("status") == "completed"
            
            execution_result = ScheduleExecutionResult(
                schedule_id=schedule_id,
                execution_id=str(uuid.uuid4()),
                success=is_successful,
                result=execution_result_dict,
                error=execution_result_dict.get("error"),
                execution_time_ms=execution_time_ms,
                worker_id=execution_result_dict.get("worker_id", worker_id),
                schedule_status=schedule.status,
                execution_count=schedule.execution_count + 1,
                consecutive_failures=0 if is_successful else schedule.consecutive_failures + 1
            )
            
            # Record execution result - this handles all updates (count, status, next_execution, etc.)
            # NOTE: Do NOT manually update schedule here - record_execution_result does everything
            schedule_manager.record_execution_result(schedule_id, execution_result)
            
            logger.info("Scheduled command executed successfully",
                       schedule_id=schedule_id,
                       command_id=command.command_id,
                       success=is_successful,
                       execution_count=schedule.execution_count)
            
            return {
                "status": "success",
                "schedule_id": schedule_id,
                "command_id": command.command_id,
                "execution_result": execution_result_dict,
                "execution_time_ms": execution_time_ms,
                "total_time_ms": int((time.time() - start_time) * 1000),
                "worker_id": execution_result_dict.get("worker_id", worker_id),
                "task_id": execution_task_id,
                "next_execution_at": schedule.next_execution_at.isoformat() if schedule.next_execution_at else None
            }
            
        except Exception as e:
            error_msg = f"Failed to execute scheduled command: {str(e)}"
            logger.error(error_msg, schedule_id=schedule_id, exc_info=True)
            
            # Record the failure
            schedule.record_failure(error_msg)
            if schedule.consecutive_failures >= schedule.max_consecutive_failures:
                schedule.status = ScheduleStatus.FAILED
            
            # Create execution result for failure
            failure_execution_result = ScheduleExecutionResult(
                schedule_id=schedule_id,
                execution_id=str(uuid.uuid4()),
                success=False,
                error=error_msg,
                execution_time_ms=int((time.time() - start_time) * 1000),
                worker_id=worker_id,
                schedule_status=schedule.status,
                execution_count=schedule.execution_count,
                consecutive_failures=schedule.consecutive_failures
            )
            schedule_manager.record_execution_result(schedule_id, failure_execution_result)
            
            return {
                "status": "error",
                "error": error_msg,
                "execution_time_ms": int((time.time() - start_time) * 1000),
                "worker_id": worker_id,
                "task_id": execution_task_id
            }
    
    except Exception as e:
        error_msg = f"Unexpected error in scheduled command execution: {str(e)}"
        logger.error(error_msg, execution_task_id=execution_task_id, celery_task_id=celery_task_id, exc_info=True)
        
        return {
            "status": "error",
            "error": error_msg,
            "execution_time_ms": int((time.time() - start_time) * 1000),
            "worker_id": worker_id,
            "task_id": execution_task_id
        }


@celery_app.task(name="imf.schedules.cleanup")
def cleanup_expired_schedules() -> Dict[str, Any]:
    """Periodic cleanup of expired schedules"""
    start_time = time.time()
    
    try:
        logger.info("Starting schedule cleanup")
        
        schedule_manager = ScheduledCommandManager()
        
        # Get all active schedules
        from ..orchestration.scheduling.models import ScheduleFilter, ScheduleStatus
        filters = ScheduleFilter(status=ScheduleStatus.ACTIVE)
        schedules = schedule_manager.storage.list_schedules(filters)
        
        cleaned_count = 0
        for schedule in schedules:
            if schedule.is_expired():
                schedule.status = ScheduleStatus.EXPIRED
                schedule_manager.storage.update_schedule(schedule)
                cleaned_count += 1
                
                logger.info("Marked expired schedule",
                           schedule_id=schedule.schedule_id,
                           schedule_type=schedule.schedule_type)
        
        execution_time_ms = int((time.time() - start_time) * 1000)
        
        logger.info("Schedule cleanup completed",
                   cleaned_count=cleaned_count,
                   execution_time_ms=execution_time_ms)
        
        return {
            "status": "success",
            "cleaned_count": cleaned_count,
            "execution_time_ms": execution_time_ms
        }
        
    except Exception as e:
        error_msg = f"Failed to cleanup expired schedules: {str(e)}"
        logger.error(error_msg, exc_info=True)
        
        return {
            "status": "error",
            "error": error_msg,
            "execution_time_ms": int((time.time() - start_time) * 1000)
        }


@celery_app.task(name="imf.schedules.recurring_check")
def check_recurring_schedules() -> Dict[str, Any]:
    """Check and execute recurring schedules with global lock to prevent concurrent execution"""
    start_time = time.time()
    
    # Import lock utilities
    from ..distributed.redis_manager import acquire_distributed_lock_sync
    
    # Removed global lock - per-schedule locks provide sufficient protection
    # Multiple workers can now check schedules concurrently for better performance
    logger.info("Checking recurring schedules")
    
    try:
        schedule_manager = ScheduledCommandManager()
        
        # Get all recurring schedules that are ready for execution
        ready_schedules = schedule_manager.get_schedules_ready_for_execution()
        
        executed_count = 0
        skipped_count = 0
        
        for schedule in ready_schedules:
            if schedule.schedule_type.value == "recurring":
                try:
                    # Acquire per-schedule lock to prevent duplicate execution of the same schedule
                    # This is critical because the global lock only prevents concurrent checks,
                    # not multiple executions of the same schedule during rapid beat intervals
                    schedule_lock = acquire_distributed_lock_sync(
                        client_id="schedule_executor",
                        lock_key=f"lock:schedule:{schedule.schedule_id}",
                        ttl_seconds=30  # Lock expires after 30 seconds
                    )
                    
                    if not schedule_lock:
                        # Another worker is executing this schedule right now, skip
                        logger.debug("Skipping schedule - already executing",
                                   schedule_id=schedule.schedule_id)
                        skipped_count += 1
                        continue
                    
                    try:
                        # CRITICAL FIX: Update last_execution_at and next_execution_at BEFORE executing
                        # This prevents race conditions where the beat check runs again before execution completes
                        
                        old_next_execution = schedule.next_execution_at
                        current_time = datetime.utcnow()
                        
                        # Update last_execution_at to current time FIRST
                        schedule.last_execution_at = current_time
                        
                        # Calculate the next execution time based on the UPDATED last_execution_at
                        new_next_execution = schedule_manager._calculate_next_execution(schedule)
                        
                        # Only execute if we successfully calculated a new next_execution time
                        if new_next_execution:
                            # Update next_execution_at and persist to Redis
                            schedule.next_execution_at = new_next_execution
                            schedule_manager.storage.update_schedule(schedule)
                            
                            # Release the per-schedule lock IMMEDIATELY after updating Redis
                            # This allows other checks to see the updated next_execution_at
                            # and prevents blocking during the actual command execution
                            if schedule_lock:
                                schedule_lock.release_sync()
                                schedule_lock = None  # Prevent double-release in finally
                            
                            logger.info("Executing recurring schedule",
                                       schedule_id=schedule.schedule_id,
                                       interval_seconds=schedule.interval_seconds,
                                       old_next_execution=old_next_execution.isoformat() if old_next_execution else None,
                                       new_next_execution=new_next_execution.isoformat())
                            
                            # Execute the schedule (lock is already released)
                            schedule_data = {
                                "schedule_id": schedule.schedule_id,
                                "command_data": schedule.metadata.get("original_command_data", {}),
                                "distributed_context": schedule.metadata.get("distributed_context", {})
                            }
                            
                            # Execute the scheduled command - must pass as JSON string
                            # Celery serialization can fail with complex nested objects
                            schedule_data_json = json.dumps(schedule_data)
                            schedule_task = cast(Any, schedule_distributed_command)
                            result = schedule_task.delay(schedule_data_json)
                            
                            logger.debug("Scheduled command task sent",
                                        schedule_id=schedule.schedule_id,
                                        task_id=result.id if result else None)
                            
                            executed_count += 1
                        else:
                            logger.warning("Skipped recurring schedule - could not calculate next execution",
                                         schedule_id=schedule.schedule_id)
                            skipped_count += 1
                    finally:
                        # Release the per-schedule lock if it wasn't already released
                        if schedule_lock:
                            schedule_lock.release_sync()
                    
                except Exception as e:
                    logger.error("Failed to execute recurring schedule",
                                schedule_id=schedule.schedule_id,
                                error=str(e), exc_info=True)
        
        execution_time_ms = int((time.time() - start_time) * 1000)
        
        logger.info("Recurring schedule check completed",
                   checked_count=len(ready_schedules),
                   executed_count=executed_count,
                   skipped_count=skipped_count,
                   execution_time_ms=execution_time_ms)
        
        return {
            "status": "success",
            "checked_count": len(ready_schedules),
            "executed_count": executed_count,
            "skipped_count": skipped_count,
            "execution_time_ms": execution_time_ms
        }
        
    except Exception as e:
        logger.error("Failed to check recurring schedules",
                   error=str(e), exc_info=True)
        return {
            "status": "error",
            "error": str(e),
            "execution_time_ms": int((time.time() - start_time) * 1000)
        }


@celery_app.task(name="imf.schedules.delayed_check")
def check_delayed_schedules() -> Dict[str, Any]:
    """Check and execute delayed and immediate schedules (one-time scheduled commands)"""
    start_time = time.time()
    
    # Import lock utilities
    from ..distributed.redis_manager import acquire_distributed_lock_sync
    from datetime import timezone
    
    logger.info("Checking delayed and immediate schedules")
    
    try:
        schedule_manager = ScheduledCommandManager()
        
        # Get all delayed/immediate schedules that are ready for execution
        ready_schedules = schedule_manager.get_schedules_ready_for_execution()
        
        executed_count = 0
        skipped_count = 0
        
        for schedule in ready_schedules:
            # Handle both delayed and immediate schedules (one-time executions)
            if schedule.schedule_type.value in ["delayed", "immediate"]:
                try:
                    # Acquire per-schedule lock to prevent duplicate execution
                    schedule_lock = acquire_distributed_lock_sync(
                        client_id="schedule_executor",
                        lock_key=f"lock:schedule:{schedule.schedule_id}",
                        ttl_seconds=30  # Lock expires after 30 seconds
                    )
                    
                    if not schedule_lock:
                        # Another worker is executing this schedule right now, skip
                        logger.debug("Skipping schedule - already executing",
                                   schedule_id=schedule.schedule_id)
                        skipped_count += 1
                        continue
                    
                    try:
                        # Update execution tracking BEFORE executing
                        current_time = datetime.now(timezone.utc)
                        schedule.last_execution_at = current_time
                        schedule.execution_count += 1
                        
                        # Delayed/immediate schedules execute once, so mark for completion after execution
                        # We'll update the schedule after sending the task
                        
                        logger.info("Executing one-time schedule",
                                   schedule_id=schedule.schedule_id,
                                   schedule_type=schedule.schedule_type.value,
                                   scheduled_at=schedule.scheduled_at.isoformat() if schedule.scheduled_at else None,
                                   execution_count=schedule.execution_count)
                        
                        # Execute the schedule
                        schedule_data = {
                            "schedule_id": schedule.schedule_id,
                            "command_data": schedule.metadata.get("original_command_data", {}),
                            "distributed_context": schedule.metadata.get("distributed_context", {})
                        }
                        
                        # Same enqueue path as recurring: .delay() honors task_routes
                        # (imf.commands.* → command_processing). Do not hardcode
                        # queue='celery' — workers do not consume that queue.
                        schedule_data_json = json.dumps(schedule_data)
                        schedule_task = cast(Any, schedule_distributed_command)
                        result = schedule_task.delay(schedule_data_json)
                        
                        logger.debug("Scheduled command task sent",
                                   schedule_id=schedule.schedule_id,
                                   task_id=result.id if result else None)
                        
                        # Mark schedule as completed since delayed schedules execute only once
                        schedule.status = ScheduleStatus.COMPLETED
                        schedule.next_execution_at = None
                        schedule_manager.storage.update_schedule(schedule)
                        
                        executed_count += 1
                        
                    finally:
                        # Release the per-schedule lock
                        if schedule_lock:
                            schedule_lock.release_sync()
                            
                except Exception as e:
                    logger.error("Failed to execute one-time schedule",
                               schedule_id=schedule.schedule_id,
                               schedule_type=schedule.schedule_type.value,
                               error=str(e), exc_info=True)
                    skipped_count += 1
                    continue
        
        execution_time_ms = int((time.time() - start_time) * 1000)
        
        logger.info("Delayed/immediate schedule check completed",
                   checked_count=len([s for s in ready_schedules if s.schedule_type.value in ["delayed", "immediate"]]),
                   executed_count=executed_count,
                   skipped_count=skipped_count,
                   execution_time_ms=execution_time_ms)
        
        return {
            "status": "success",
            "checked_count": len([s for s in ready_schedules if s.schedule_type.value in ["delayed", "immediate"]]),
            "executed_count": executed_count,
            "skipped_count": skipped_count,
            "execution_time_ms": execution_time_ms
        }
        
    except Exception as e:
        logger.error("Failed to check delayed/immediate schedules",
                   error=str(e), exc_info=True)
        return {
            "status": "error",
            "error": str(e),
            "execution_time_ms": int((time.time() - start_time) * 1000)
        }


@celery_app.task(name="imf.schedules.condition_check")
def check_conditional_schedules() -> Dict[str, Any]:
    """Check and execute conditional schedules"""
    start_time = time.time()
    
    try:
        logger.info("Checking conditional schedules")
        
        schedule_manager = ScheduledCommandManager()
        
        # Get all conditional schedules
        from ..orchestration.scheduling.models import ScheduleFilter, ScheduleStatus
        from motet.core.commands.distributed import ScheduleType
        
        filters = ScheduleFilter(
            status=ScheduleStatus.ACTIVE,
            schedule_type=ScheduleType.CONDITIONAL
        )
        schedules = schedule_manager.storage.list_schedules(filters)
        
        executed_count = 0
        for schedule in schedules:
            if schedule.should_execute() and schedule.condition_expression:
                try:
                    # TODO: Implement safe condition evaluation in Phase 4
                    # For now, just log that we would check the condition
                    logger.info("Would check condition for schedule",
                               schedule_id=schedule.schedule_id,
                               condition=schedule.condition_expression)
                    
                    # Placeholder for actual condition evaluation
                    condition_met = False  # TODO: Implement condition evaluation
                    
                    if condition_met:
                        # Execute the schedule
                        # TODO: Implement conditional execution
                        executed_count += 1
                        
                except Exception as e:
                    logger.error("Failed to check condition for schedule",
                                schedule_id=schedule.schedule_id,
                                error=str(e), exc_info=True)
        
        execution_time_ms = int((time.time() - start_time) * 1000)
        
        logger.info("Conditional schedule check completed",
                   checked_count=len(schedules),
                   executed_count=executed_count,
                   execution_time_ms=execution_time_ms)
        
        return {
            "status": "success",
            "checked_count": len(schedules),
            "executed_count": executed_count,
            "execution_time_ms": execution_time_ms
        }
        
    except Exception as e:
        error_msg = f"Failed to check conditional schedules: {str(e)}"
        logger.error(error_msg, exc_info=True)
        
        return {
            "status": "error",
            "error": error_msg,
            "execution_time_ms": int((time.time() - start_time) * 1000)
        }
