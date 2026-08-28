"""
Motet - Worker Communicator

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Routing worker communicator for the Motet distributed framework.

    All workers (cloud and local) communicate via the shared Valkey broker.
    Local workers reach Valkey through a WireGuard tunnel; from
    the perspective of command routing, they are indistinguishable from
    cloud workers.

    honors sticky cancel via inherited ``cancel_scopes`` (one
    variadic EXISTS) before ``send_task`` and waits for child completion via
    sticky control + per-waiter ``BLPOP`` wakes (cancel and result) — no
    pub/sub and no Celery ``ready()`` poll. After the result wake the parent
    loads SUCCESS/FAILURE from Motet ``cmd:outcome:{command_id}`` (issue
    #229). ``retrieve_command_wait_outcome`` hydrates ``cmd:result``
    pointers. Wait timeouts auto-cancel the waiting parent's own cancel scope
    when it pushed one (root command_id or workflow_run_id). Nested leaves
    that did not push a scope do not cancel the tree.

Dependencies:
    - typing: Type hints and annotations
    - motet.core.distributed.task_control: Scope cancel sticky + BLPOP wake
    - motet.core.distributed.redis_command_data_manager: ``cmd:outcome`` wait envelope

Usage:
    from motet.core.workers.routing.worker_communicator import WorkerCommunicator

Notes:
    - All command dispatch uses Celery send_task to per-worker queues
    - Per-command ``timeout_seconds`` is passed as Celery ``time_limit`` /
      ``soft_time_limit`` so long workflows (e.g. builder implement_cycle)
      are not killed by the global 600s Celery default
"""


import os
import time
from typing import Dict, Any, Optional

import structlog

from motet.core.commands.distributed import DistributedCommand
from motet.core.constants import CELERY_PROCESS_COMMAND_TASK

logger = structlog.get_logger(__name__)


class WorkerCommunicator:
    """
    Clean interface for worker communication.
    
    This component handles:
    - Celery task dispatch
    - Result polling and retrieval
    - Error handling and retries
    - Communication metrics
    """
    
    def __init__(self, 
                 default_timeout: int = 60,
                 enable_retries: bool = True,
                 max_retries: int = 3):
        self.default_timeout = default_timeout
        self.enable_retries = enable_retries
        self.max_retries = max_retries
        
        self.comm_stats = {
            'total_commands_sent': 0,
            'successful_commands': 0,
            'failed_commands': 0,
            'timeout_commands': 0,
            'cancelled_commands': 0,
            'retry_attempts': 0,
            'avg_response_time_ms': 0.0
        }

    @staticmethod
    def _motet_task_id(command: DistributedCommand) -> str:
        ctx = getattr(command, "distributed_context", None)
        if not ctx:
            return ""
        return (getattr(ctx, "task_id", None) or "").strip()

    @staticmethod
    def _cancel_scopes(command: DistributedCommand) -> list:
        ctx = getattr(command, "distributed_context", None)
        if not ctx:
            return []
        from motet.core.distributed.task_control import append_cancel_scope

        scopes = list(getattr(ctx, "cancel_scopes", None) or [])
        task_id = (getattr(ctx, "task_id", None) or "").strip()
        return append_cancel_scope(scopes, task_id)

    @staticmethod
    def _cancel_waiter_own_scope(*, reason: str, source: str) -> None:
        """Cancel the waiting parent's own scope, if it pushed one."""
        try:
            from motet.core.commands.motet_context import get_motet_context
            from motet.core.distributed.task_control import cancel_own_scope_for_command

            motet = get_motet_context()
            command = getattr(motet, "_command", None)
            if command is None:
                return
            cancel_own_scope_for_command(
                command, reason=reason, source=source
            )
        except Exception:
            return

    def _cancelled_result(
        self,
        *,
        command: DistributedCommand,
        worker_id: Optional[str],
        start_time: float,
        celery_task_id: Optional[str] = None,
        reason: str = "Task cancelled",
        error_code: str = "task_cancelled",
    ) -> Dict[str, Any]:
        self.comm_stats['cancelled_commands'] += 1
        response_time = int((time.time() - start_time) * 1000)
        motet_task_id = self._motet_task_id(command)
        logger.info(
            "worker_communicator_command_cancelled",
            command_id=command.command_id,
            command_type=command.get_command_type(),
            worker_id=worker_id,
            task_id=motet_task_id,
            reason=reason,
            error_code=error_code,
        )
        out: Dict[str, Any] = {
            'status': 'error',
            'error': reason,
            'error_code': error_code,
            'worker_id': worker_id,
            'response_time_ms': response_time,
            'motet_task_id': motet_task_id,
        }
        if celery_task_id:
            out['task_id'] = celery_task_id
        return out

    def send_command(self, 
                         worker: Dict[str, Any], 
                         command: DistributedCommand) -> Dict[str, Any]:
        """
        Send command to specific worker via Celery.
        
        Args:
            worker: Worker information dict
            command: Command to execute
            
        Returns:
            Execution result from worker
        """
        start_time = time.time()
        worker_id = worker.get('worker_id')
        motet_task_id = self._motet_task_id(command)
        cancel_scopes = self._cancel_scopes(command)
        
        try:
            self.comm_stats['total_commands_sent'] += 1

            from motet.core.distributed.task_control import is_cancelled

            # ADR-0131: refuse enqueue if any inherited cancel scope is sticky.
            if cancel_scopes and is_cancelled(cancel_scopes):
                hit = None
                try:
                    from motet.core.distributed.task_control import first_cancelled_scope

                    hit = first_cancelled_scope(cancel_scopes)
                except Exception:
                    hit = None
                workflow_hit = bool(hit and str(hit).startswith("wfrun-"))
                return self._cancelled_result(
                    command=command,
                    worker_id=worker_id,
                    start_time=start_time,
                    reason=(
                        "Workflow cancelled (pre-send)"
                        if workflow_hit
                        else "Task cancelled (pre-send)"
                    ),
                    error_code=(
                        "workflow_cancelled" if workflow_hit else "task_cancelled"
                    ),
                )
            
            from ..celery_app import get_celery_app
            
            command_data = command.serialize_for_transport()
            
            if not worker_id:
                raise ValueError(
                    f"WorkerCommunicator.send_command requires worker_id but got None. "
                    f"Router must select a specific worker before sending command {command.command_id}"
                )

            celery_app = get_celery_app()
            queue_name = f"worker.{worker_id}"

            timeout = getattr(
                command.distributed_context, "timeout_seconds", self.default_timeout
            )
            try:
                timeout = int(timeout) if timeout is not None else self.default_timeout
            except (TypeError, ValueError):
                timeout = self.default_timeout
            if timeout < 1:
                timeout = self.default_timeout
            # Override Celery's global task_time_limit (default 600s) so long
            # commands like core.workflow_execution are not hard-killed early.
            soft_time_limit = max(timeout - 60, 1) if timeout > 60 else max(timeout - 1, 1)

            celery_result = celery_app.send_task(
                CELERY_PROCESS_COMMAND_TASK,
                args=[command_data],
                queue=queue_name,
                time_limit=timeout,
                soft_time_limit=soft_time_limit,
                ignore_result=True,
            )
            
            logger.debug(
                "worker_communicator_command_sent",
                command_id=command.command_id,
                command_type=command.get_command_type(),
                worker_id=worker_id,
                queue_name=queue_name,
                celery_task_id=celery_result.id,
                time_limit=timeout,
                soft_time_limit=soft_time_limit,
            )
            
            result = None

            try:
                from motet.core.distributed.task_control import wait_for_command_outcome

                # ADR-0131: BLPOP cancel + result wakes. Payload is Motet
                # ``cmd:outcome`` (#229), not Celery ``AsyncResult``.
                outcome = wait_for_command_outcome(
                    motet_task_id or None,
                    celery_result.id,
                    timeout_seconds=float(timeout),
                    cancel_scopes=cancel_scopes,
                )
                outcome_name = getattr(outcome, "outcome", outcome)
                if outcome_name == "cancelled":
                    hit = getattr(outcome, "cancelled_scope", None)
                    workflow_hit = bool(hit and str(hit).startswith("wfrun-"))
                    if workflow_hit:
                        reason = "Workflow cancelled (wait)"
                        error_code = "workflow_cancelled"
                    else:
                        reason = "Task cancelled (wait)"
                        error_code = "task_cancelled"
                    return self._cancelled_result(
                        command=command,
                        worker_id=worker_id,
                        start_time=start_time,
                        celery_task_id=celery_result.id,
                        reason=reason,
                        error_code=error_code,
                    )
                if outcome_name == "timeout":
                    raise Exception(f"Task timed out after {timeout}s")

                from motet.core.distributed.redis_command_data_manager import (
                    get_redis_command_data_manager,
                )

                ctx = command.distributed_context
                try:
                    result = get_redis_command_data_manager().retrieve_command_wait_outcome(
                        command.command_id,
                        tenant_id=getattr(ctx, "tenant_id", None) if ctx else None,
                        motet_id=getattr(ctx, "motet_id", None) if ctx else None,
                    )
                except Exception as retrieve_error:
                    raise Exception(
                        f"Task result wake received but Motet wait outcome "
                        f"not found for command {command.command_id}"
                    ) from retrieve_error
                if not isinstance(result, dict):
                    raise Exception(
                        f"Task result wake received but Motet wait outcome "
                        f"was not an envelope for command {command.command_id}"
                    )
                if result.get("status") == "error":
                    raise Exception(f"Task failed: {result.get('error')}")

                response_time = int((time.time() - start_time) * 1000)
                self.comm_stats['successful_commands'] += 1
                self._update_response_time(response_time)

                logger.debug(
                    "worker_communicator_command_completed",
                    command_id=command.command_id,
                    command_type=command.get_command_type(),
                    worker_id=worker_id,
                    response_time_ms=response_time,
                )

                # retrieve_command_wait_outcome already resolves
                # ``_redis_result_key``; this is idempotent if hydrated.
                rehydrated_result = DistributedCommand.rehydrate_command_result(result)

                if result != rehydrated_result:
                    logger.debug(
                        "worker_communicator_result_rehydrated",
                        command_id=command.command_id,
                        command_type=command.get_command_type(),
                        worker_id=worker_id,
                        rehydrated=True,
                    )
                else:
                    debug_mode = os.getenv("MOTET_DEBUG_MODE", "false").lower() == "true"
                    if debug_mode:
                        logger.debug(
                            "worker_communicator_result_rehydrated",
                            command_id=command.command_id,
                            command_type=command.get_command_type(),
                            worker_id=worker_id,
                            rehydrated=False,
                        )

                return {
                    'status': 'completed',
                    'result': rehydrated_result,
                    'timing': rehydrated_result.get('result', {}).get('timing', {}) if isinstance(rehydrated_result, dict) and 'result' in rehydrated_result else {},
                    'worker_id': worker_id,
                    'response_time_ms': response_time,
                    'task_id': celery_result.id
                }

            except Exception as e:
                response_time = int((time.time() - start_time) * 1000)

                err_l = str(e).lower()
                if (
                    "timeout" in err_l
                    or "timed out" in err_l
                    or "time limit" in err_l
                    or "wait outcome was stored" in err_l
                    or "wait outcome not found" in err_l
                ):
                    self.comm_stats['timeout_commands'] += 1
                    error_msg = f"Command timed out after {timeout}s"
                    # Cancel the waiting parent's own scope if it pushed one
                    # (root command_id / workflow_run_id). Nested leaves are a no-op.
                    try:
                        self._cancel_waiter_own_scope(
                            reason=error_msg,
                            source="communicator_timeout",
                        )
                    except Exception as cancel_err:
                        logger.warning(
                            "worker_communicator_timeout_cancel_failed",
                            task_id=motet_task_id,
                            error=str(cancel_err),
                            exc_info=True,
                        )
                else:
                    self.comm_stats['failed_commands'] += 1
                    error_msg = f"Command execution failed: {str(e)}"

                logger.warning(
                    "worker_communicator_command_failed",
                    command_id=command.command_id,
                    command_type=command.get_command_type(),
                    worker_id=worker_id,
                    error=error_msg,
                )

                return {
                    'status': 'error',
                    'error': error_msg,
                    'worker_id': worker_id,
                    'response_time_ms': response_time,
                    'task_id': celery_result.id
                }
                
        except Exception as e:
            response_time = int((time.time() - start_time) * 1000)
            self.comm_stats['failed_commands'] += 1
            
            error_msg = f"Communication error: {str(e)}"
            logger.error(
                "worker_communicator_send_failed",
                command_id=command.command_id,
                command_type=command.get_command_type(),
                worker_id=worker_id,
                error=error_msg,
                exc_info=True,
            )
            
            return {
                'status': 'error',
                'error': error_msg,
                'worker_id': worker_id,
                'response_time_ms': response_time
            }

    def get_communication_stats(self) -> Dict[str, Any]:
        """Get communication statistics"""
        return self.comm_stats.copy()
    
    def reset_stats(self):
        """Reset communication statistics"""
        self.comm_stats = {
            'total_commands_sent': 0,
            'successful_commands': 0,
            'failed_commands': 0,
            'timeout_commands': 0,
            'cancelled_commands': 0,
            'retry_attempts': 0,
            'avg_response_time_ms': 0.0
        }
    
    def _update_response_time(self, response_time_ms: int):
        """Update average response time"""
        current_avg = self.comm_stats['avg_response_time_ms']
        total_successful = self.comm_stats['successful_commands']
        
        if total_successful == 1:
            self.comm_stats['avg_response_time_ms'] = response_time_ms
        else:
            self.comm_stats['avg_response_time_ms'] = (
                (current_avg * (total_successful - 1) + response_time_ms) / total_successful
            )
