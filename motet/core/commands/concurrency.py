"""
Motet - Distributed Concurrency Commands

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-23

Description:
    Distributed concurrency command system for the Motet distributed framework.
    Provides unified distributed commands for concurrent execution including gather,
    dispatch, and map operations, plus Motet ``cmd:outcome`` fan-in.
    Gather/join results are returned in submission order (not completion order).
    ``celery.group().apply_async()`` is fan-out only. Fan-in waits Motet
    result wakes and loads hydrated ``cmd:outcome`` (issue #242).
    ``retrieve_command_wait_outcome`` resolves ``_redis_result_key``.
    Fan-in hydrates ``cmd:outcome`` first and only BLPOP-waits leftovers, so a
    missed result wake (Redis socket timeout on BLPOP) cannot hang a join whose
    children already stored envelopes. Leftover waits and outcome hydrates run
    concurrently. Composition does not subscribe to completion events. Command
    members use ``ignore_result=True``.

Dependencies:
    - celery: Distributed task execution and coordination
    - time: Timestamp and performance tracking
    - typing: Type hints and annotations
    - Distributed command system
    - Concurrency primitives and event system

Usage:
    from motet.core.commands.concurrency import GatherCommand, DispatchCommand
    
    # Gather results from multiple commands
    gather_command = GatherCommand(
        child_commands=[command1, command2, command3]
    )
    results = await gather_command.execute()
    
    # Dispatch commands to multiple workers
    dispatch_command = DispatchCommand(
        commands=[command1, command2],
        target_workers=["worker1", "worker2"]
    )
    results = await dispatch_command.execute()

Notes:
    - Supports gather operations for collecting results from multiple commands
    - Includes dispatch operations for distributing commands to workers
    - Provides map operations for parallel execution of similar commands
    - Gather/map fan-in waits Motet result wakes and loads hydrated ``cmd:outcome``
    - Leftover child waits and outcome hydrates run concurrently (``WorkerExecutor``)
    - EventBus completion events are observability, not the composition join
    - Integrates with distributed worker routing and coordination
    - Supports comprehensive distributed concurrency management
"""


from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Sequence, Type

import structlog

from celery import group as celery_group

from motet.core.commands.distributed import DistributedCommand
from motet.core.commands.response_models import child_command_envelope

from motet.core.commands.capabilities import WorkerCapability
from motet.core.commands.command_data_classes import GatherCommandData, DispatchCommandData, MapCommandData
from motet.core.workers.observers import EventPriority


logger = structlog.get_logger(__name__)
DEBUG_MODE = os.getenv("MOTET_DEBUG_MODE", "false").lower() == "true"

DEFAULT_JOIN_TIMEOUT_SECONDS = 600
GATHER_CHILD_OVERHEAD_SECONDS = 30
GATHER_PERSIST_SLACK_SECONDS = 15


def _safe_timeout_seconds(raw: Any, default: int = 0) -> int:
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def join_celery_timeout_seconds(
    *,
    requested: Optional[int],
    child_timeouts: Sequence[int],
    default: int = DEFAULT_JOIN_TIMEOUT_SECONDS,
) -> int:
    """Celery/communicator limit for a gather/map parent.

    Must cover the longest child plus overhead, or the caller's requested
    budget, whichever is larger. Falls back to ``default`` when neither is set.
    """
    child_max = max((_safe_timeout_seconds(item) for item in child_timeouts), default=0)
    derived = child_max + GATHER_CHILD_OVERHEAD_SECONDS if child_max else 0
    requested_i = _safe_timeout_seconds(requested)
    return max(requested_i, derived) or default


def join_wait_timeout_seconds(
    *,
    own_timeout: Optional[int],
    child_max: int,
    default: int = DEFAULT_JOIN_TIMEOUT_SECONDS,
) -> float:
    """In-process fan-in budget: child wait, but leave slack to persist."""
    derived = (
        _safe_timeout_seconds(child_max) + GATHER_CHILD_OVERHEAD_SECONDS
        if _safe_timeout_seconds(child_max)
        else default
    )
    own = _safe_timeout_seconds(own_timeout, default)
    capped = max(1, own - GATHER_PERSIST_SLACK_SECONDS)
    return float(min(derived, capped))


def _capability_token(capability: Any) -> str:
    """Normalize capability enum/string values for routing checks."""
    if isinstance(capability, WorkerCapability):
        return capability.value
    value = getattr(capability, "value", capability)
    return str(value).strip().lower()


def _get_strict_routing_reason(cmd: DistributedCommand) -> Optional[str]:
    """
    Determine whether a command requires strict worker routing.

    Strict routing means we must not fall back to generic Celery routing because
    doing so could violate hard placement constraints.
    """
    context = getattr(cmd, "distributed_context", None)
    if not context:
        return None

    target_worker_id = getattr(context, "target_worker_id", None)
    if target_worker_id:
        return f"target_worker_id={target_worker_id}"

    required_capabilities = getattr(context, "required_capabilities", set()) or set()
    for capability in required_capabilities:
        if _capability_token(capability).startswith("edge_"):
            return "required_edge_capability"

    return None


def _celery_waiter_ids_from_group(group_result: Any) -> List[str]:
    """Celery task ids from ``group().apply_async()`` — ids only, never ``.get()``."""
    results = getattr(group_result, "results", None) or []
    waiter_ids: List[str] = []
    for item in results:
        wid = getattr(item, "id", None)
        if wid:
            waiter_ids.append(str(wid))
    return waiter_ids


def _dispatch_signatures(signatures: Sequence[Any]) -> List[str]:
    """Fan-out a Celery group and return waiter ids aligned with ``signatures``."""
    if not signatures:
        return []
    group_result = celery_group(list(signatures)).apply_async()
    waiter_ids = _celery_waiter_ids_from_group(group_result)
    if len(waiter_ids) != len(signatures):
        logger.warning(
            "gather_map_waiter_id_count_mismatch",
            signature_count=len(signatures),
            waiter_count=len(waiter_ids),
        )
    return waiter_ids


def _normalize_outcome_error(error: Any) -> Dict[str, Any]:
    if isinstance(error, dict):
        return error
    if error:
        return {
            "type": "CommandExecutionError",
            "message": str(error),
            "details": {},
        }
    return {
        "type": "UnknownError",
        "message": "Command failed",
        "details": {},
    }


def map_wait_outcome_to_observer_result(
    *,
    command_id: str,
    command_type: str,
    wait_outcome: str,
    envelope: Any,
    cancelled_scope: Optional[str] = None,
) -> Dict[str, Any]:
    """Map Motet ``cmd:outcome`` + wait result onto gather/map aggregation envelopes.

    Outcomes use ``completed`` / ``error``; aggregation still expects
    ``success`` / ``error`` / ``timeout`` (issue #242).
    """
    if wait_outcome == "cancelled":
        return {
            "command_id": command_id,
            "command_type": command_type,
            "status": "error",
            "result": None,
            "error": {
                "type": "TaskCancelled",
                "message": "Task cancelled",
                "details": {
                    "code": "task_cancelled",
                    "cancelled_scope": cancelled_scope,
                },
            },
        }
    if wait_outcome == "timeout":
        return {
            "command_id": command_id,
            "command_type": command_type,
            "status": "timeout",
            "result": None,
            "error": {"message": "Command did not complete within timeout"},
        }
    if not isinstance(envelope, dict):
        return {
            "command_id": command_id,
            "command_type": command_type,
            "status": "timeout",
            "result": None,
            "error": {
                "message": "No Motet wait outcome received for child command"
            },
        }

    envelope_status = envelope.get("status")
    command_type_out = str(envelope.get("command_type") or command_type)
    if envelope_status == "error":
        return {
            "command_id": command_id,
            "command_type": command_type_out,
            "status": "error",
            "result": envelope.get("result"),
            "error": _normalize_outcome_error(envelope.get("error")),
            "execution_time_ms": envelope.get("execution_time_ms"),
            "worker_id": envelope.get("worker_id"),
        }
    # ``completed`` (and any other non-error envelope) → observer ``success``
    return {
        "command_id": command_id,
        "command_type": command_type_out,
        "status": "success",
        "result": envelope.get("result"),
        "error": envelope.get("error"),
        "execution_time_ms": envelope.get("execution_time_ms"),
        "worker_id": envelope.get("worker_id"),
    }


def collect_child_wait_results(
    *,
    task_id: Optional[str],
    child_commands: Sequence[DistributedCommand],
    waiter_ids: Sequence[str],
    timeout_seconds: float,
    cancel_scopes: Optional[Sequence[str]] = None,
    tenant_id: Optional[str] = None,
    motet_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Load hydrated ``cmd:outcome`` in order; BLPOP-wait only leftovers.

    Children that already stored an envelope are complete even if the result
    wake was missed (Redis TLS BLPOP socket timeouts). Wait only the ones
    that have no envelope yet, then hydrate timed-out leftovers again.
    """
    from motet.core.distributed.redis_command_data_manager import (
        get_redis_command_data_manager,
    )
    from motet.core.distributed.task_control import (
        CommandWaitResult,
        wait_for_command_outcomes,
    )

    children = list(child_commands)
    ids = list(waiter_ids)
    manager = get_redis_command_data_manager()
    command_ids = [
        str(getattr(cmd, "command_id", "") or "") for cmd in children
    ]
    envelopes = _retrieve_wait_envelopes(
        manager,
        [cid for cid in command_ids if cid],
        tenant_id=tenant_id,
        motet_id=motet_id,
    )

    waited: Dict[str, Any] = {}
    leftover_ids: List[str] = []
    leftover_command_ids: List[str] = []
    for index, _cmd in enumerate(children):
        command_id = command_ids[index] if index < len(command_ids) else ""
        wid = ids[index] if index < len(ids) else ""
        if command_id and envelopes.get(command_id) is not None:
            if wid:
                waited[wid] = CommandWaitResult("completed")
            continue
        if wid:
            leftover_ids.append(wid)
            if command_id:
                leftover_command_ids.append(command_id)

    if leftover_ids:
        waited.update(
            wait_for_command_outcomes(
                task_id,
                leftover_ids,
                timeout_seconds=timeout_seconds,
                cancel_scopes=cancel_scopes,
                tenant_id=tenant_id,
                motet_id=motet_id,
            )
        )
        if leftover_command_ids:
            envelopes.update(
                _retrieve_wait_envelopes(
                    manager,
                    leftover_command_ids,
                    tenant_id=tenant_id,
                    motet_id=motet_id,
                )
            )

    collected: List[Dict[str, Any]] = []
    for index, cmd in enumerate(children):
        command_id = command_ids[index] if index < len(command_ids) else ""
        command_type = (
            cmd.get_command_type()
            if hasattr(cmd, "get_command_type")
            else "unknown"
        )
        wid = ids[index] if index < len(ids) else ""
        wait = waited.get(wid) if wid else None
        wait_outcome = wait.outcome if wait is not None else "timeout"
        cancelled_scope = wait.cancelled_scope if wait is not None else None
        envelope = envelopes.get(command_id) if command_id else None
        if envelope is not None and wait_outcome != "cancelled":
            wait_outcome = "completed"
        collected.append(
            map_wait_outcome_to_observer_result(
                command_id=command_id,
                command_type=command_type,
                wait_outcome=wait_outcome,
                envelope=envelope if wait_outcome == "completed" else None,
                cancelled_scope=cancelled_scope,
            )
        )
    return collected


def _retrieve_one_wait_envelope(
    manager: Any,
    command_id: str,
    tenant_id: Optional[str],
    motet_id: Optional[str],
) -> Any:
    try:
        return manager.retrieve_command_wait_outcome(
            command_id,
            tenant_id=tenant_id,
            motet_id=motet_id,
        )
    except Exception as retrieve_error:
        logger.warning(
            "gather_map_wait_outcome_retrieve_failed",
            command_id=command_id,
            error=str(retrieve_error),
            error_type=type(retrieve_error).__name__,
        )
        return None


def _retrieve_wait_envelopes(
    manager: Any,
    command_ids: Sequence[str],
    *,
    tenant_id: Optional[str],
    motet_id: Optional[str],
) -> Dict[str, Any]:
    """Load hydrated ``cmd:outcome`` bodies; concurrent when more than one."""
    unique_ids: List[str] = []
    seen: set[str] = set()
    for command_id in command_ids:
        if not command_id or command_id in seen:
            continue
        seen.add(command_id)
        unique_ids.append(command_id)
    if not unique_ids:
        return {}
    if len(unique_ids) == 1:
        command_id = unique_ids[0]
        return {
            command_id: _retrieve_one_wait_envelope(
                manager, command_id, tenant_id, motet_id
            )
        }

    from motet.core.workers.concurrency_primitives import WorkerExecutor

    envelopes: Dict[str, Any] = {}
    with WorkerExecutor(max_workers=len(unique_ids)) as executor:
        futures = {
            command_id: executor.submit(
                _retrieve_one_wait_envelope,
                manager,
                command_id,
                tenant_id,
                motet_id,
            )
            for command_id in unique_ids
        }
        for command_id, future in futures.items():
            try:
                envelopes[command_id] = future.result()
            except Exception as retrieve_error:
                logger.warning(
                    "gather_map_wait_outcome_retrieve_failed",
                    command_id=command_id,
                    error=str(retrieve_error),
                    error_type=type(retrieve_error).__name__,
                )
                envelopes[command_id] = None
    return envelopes


class GatherCommand(DistributedCommand):
    """Execute multiple commands in parallel and wait for all results (fan-out/fan-in join)."""
    
    def _get_default_timeout(self) -> int:
        """Default timeout for group execution (child max + overhead, else 600s)."""
        return DEFAULT_JOIN_TIMEOUT_SECONDS
    
    def _get_default_priority(self) -> int:
        """Default priority for group execution."""
        return EventPriority.NORMAL
    
    def _setup_command_specifics(self):
        """Setup command-specific configuration."""
        # GatherCommand needs capabilities of all child commands
        # This will be determined dynamically from child commands
        self.distributed_context.required_capabilities = set([
            WorkerCapability.TOOL_EXECUTION,  # Basic capability
        ])
    
    @classmethod
    def _get_data_class(cls) -> Type[GatherCommandData]:
        """Return the data class for this command."""
        return GatherCommandData
    
    def get_command_type(self) -> str:
        """Return the command type identifier."""
        return "core.gather"
    
    def can_undo(self) -> bool:
        """GatherCommand execution cannot be undone."""
        return False
    
    def undo(self, worker_context: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
        """Undo is not supported for GatherCommand."""
        raise NotImplementedError("GatherCommand execution cannot be undone")
    
    def _do_execute(self, worker_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute child commands in parallel using Celery group().
        
        Args:
            worker_context: Worker execution context
            
        Returns:
            ADR-0029 compliant response with aggregated results
        """
        start_time = time.time()
        
        # Store worker ID for metadata (ADR-0029)
        self._worker_id = worker_context.get("worker_id")
        
        try:
            # Deserialize child commands from command data
            child_commands = self._deserialize_child_commands(worker_context)
            
            if not child_commands:
                execution_time_ms = (time.time() - start_time) * 1000
                return self._create_success_response(
                    data={
                        "results": [],
                        "total_commands": 0,
                        "successful": 0,
                        "failed": 0,
                        "aggregation_strategy": self.data.aggregation_strategy
                    },
                    execution_time_ms=execution_time_ms,
                    warnings=["No child commands provided"]
                )
            
            logger.debug(
                "gather_command_start",
                command_id=self.command_id,
                child_count=len(child_commands),
                aggregation_strategy=self.data.aggregation_strategy,
                fail_fast=self.data.fail_fast,
            )
            
            # Get worker router from context for intelligent routing
            worker_router = worker_context.get('worker_router')
            if not worker_router:
                logger.warning(
                    "gather_command_no_worker_router_fallback_basic_routing",
                    command_id=self.command_id,
                )
                # Fallback to basic Celery routing
                worker_router = None
            
            # Route each child command through WorkerRouter for capability-based routing
            routed_tasks = self._create_routed_tasks(child_commands, worker_router)

            ctx = self.distributed_context
            child_max = max(
                (
                    _safe_timeout_seconds(cmd.distributed_context.timeout_seconds)
                    for cmd in child_commands
                ),
                default=0,
            )
            max_timeout = join_wait_timeout_seconds(
                own_timeout=getattr(ctx, "timeout_seconds", None),
                child_max=child_max,
                default=self._get_default_timeout(),
            )

            total_children = len(child_commands)
            max_parallel = self.data.max_parallel

            if max_parallel is not None:
                try:
                    max_parallel = int(max_parallel)
                except Exception:
                    max_parallel = None
            if max_parallel is not None and max_parallel < 1:
                max_parallel = None
            cancel_scopes = getattr(ctx, "cancel_scopes", None)
            tenant_id = getattr(ctx, "tenant_id", None)
            motet_id = getattr(ctx, "motet_id", None)
            task_id = getattr(ctx, "task_id", None)
            results_by_id: Dict[str, Dict[str, Any]] = {}
            deadline = time.time() + float(max_timeout)

            # group() is fan-out only; fan-in is Motet cmd:outcome (#242).
            if max_parallel is None or max_parallel >= total_children:
                waiter_ids = _dispatch_signatures(routed_tasks)
                chunk_results = collect_child_wait_results(
                    task_id=task_id,
                    child_commands=child_commands,
                    waiter_ids=waiter_ids,
                    timeout_seconds=max(0.0, deadline - time.time()),
                    cancel_scopes=cancel_scopes,
                    tenant_id=tenant_id,
                    motet_id=motet_id,
                )
                for item in chunk_results:
                    cmd_id = item.get("command_id")
                    if cmd_id:
                        results_by_id[str(cmd_id)] = item
            else:
                if DEBUG_MODE:
                    logger.debug(
                        "gather_command_chunked_dispatch_start",
                        command_id=self.command_id,
                        child_count=total_children,
                        max_parallel=max_parallel,
                        timeout_seconds=max_timeout,
                    )
                for start_idx in range(0, total_children, max_parallel):
                    end_idx = min(start_idx + max_parallel, total_children)
                    chunk_cmds = child_commands[start_idx:end_idx]
                    waiter_ids = _dispatch_signatures(routed_tasks[start_idx:end_idx])
                    remaining = max(0.0, deadline - time.time())
                    chunk_results = collect_child_wait_results(
                        task_id=task_id,
                        child_commands=chunk_cmds,
                        waiter_ids=waiter_ids,
                        timeout_seconds=remaining,
                        cancel_scopes=cancel_scopes,
                        tenant_id=tenant_id,
                        motet_id=motet_id,
                    )
                    timed_out = False
                    for item in chunk_results:
                        cmd_id = item.get("command_id")
                        if cmd_id:
                            results_by_id[str(cmd_id)] = item
                        if item.get("status") == "timeout":
                            timed_out = True
                    if timed_out:
                        logger.warning(
                            "gather_command_timeout_partial_results",
                            command_id=self.command_id,
                            timeout_seconds=max_timeout,
                            completed_count=len(results_by_id),
                            total_count=total_children,
                        )
                        break

            if DEBUG_MODE:
                logger.debug(
                    "gather_command_dispatched_waiting",
                    command_id=self.command_id,
                    child_count=total_children,
                    max_parallel=max_parallel,
                )

            results = []
            for cmd in child_commands:
                cmd_id = str(cmd.command_id)
                if cmd_id in results_by_id:
                    results.append(results_by_id[cmd_id])
                else:
                    results.append({
                        "command_id": cmd_id,
                        "status": "timeout",
                        "error": {"message": "Command did not complete within timeout"},
                    })

            if any(item.get("status") == "timeout" for item in results):
                logger.warning(
                    "gather_command_timeout_partial_results",
                    command_id=self.command_id,
                    timeout_seconds=max_timeout,
                )

            # Aggregate results based on strategy
            aggregated = self._aggregate_results(
                results=results,
                child_commands=child_commands,
                strategy=self.data.aggregation_strategy
            )
            
            # Calculate execution metrics
            execution_time_ms = (time.time() - start_time) * 1000
            
            # Determine if this was a success or partial success
            if aggregated["failed"] == 0:
                # All commands succeeded
                return self._create_success_response(
                    data=aggregated,
                    execution_time_ms=execution_time_ms
                )
            elif aggregated["successful"] > 0:
                # Some succeeded, some failed
                return self._create_partial_success_response(
                    data=aggregated,
                    error={
                        "type": "PartialGroupFailure",
                        "message": f"{aggregated['failed']} of {aggregated['total_commands']} commands failed",
                        "details": {
                            "failed_commands": aggregated.get("failed_commands", [])
                        },
                        "recoverable": True,
                        "retry_recommended": False
                    },
                    execution_time_ms=execution_time_ms
                )
            else:
                # All commands failed
                return self._create_error_response(
                    error=Exception(f"All {aggregated['total_commands']} commands failed"),
                    execution_time_ms=execution_time_ms,
                    details={
                        "failed_commands": aggregated.get("failed_commands", [])
                    }
                )
            
        except Exception as e:
            execution_time_ms = (time.time() - start_time) * 1000
            logger.error(
                "gather_command_failed",
                command_id=self.command_id,
                error=str(e),
                exc_info=True,
            )
            return self._create_error_response(
                error=e,
                execution_time_ms=execution_time_ms
            )
    
    def _create_routed_tasks(
        self,
        child_commands: List[DistributedCommand],
        worker_router: Optional[Any]
    ) -> List[Any]:
        """
        Create Celery task signatures with intelligent routing.
        
        Routes each child command through WorkerRouter to select the optimal
        worker based on capabilities, load, and routing strategy. Then creates
        Celery task signatures with explicit queue assignment for targeted execution.
        
        Args:
            child_commands: List of child commands to route
            worker_router: WorkerRouter instance for intelligent routing
            
        Returns:
            List of Celery task signatures with queue assignment
        """
        from motet.core.workers.command_tasks import process_distributed_command
        
        routed_tasks = []
        
        for cmd in child_commands:
            # Serialize the command
            command_data = cmd.serialize_for_transport()
            
            if worker_router:
                try:
                    # Route the command through WorkerRouter
                    routing_decision = worker_router.route_command(cmd)
                    
                    if routing_decision.selected_worker:
                        # Get the worker ID and queue name
                        worker_id = routing_decision.selected_worker.get('worker_id')
                        queue_name = f"worker.{worker_id}"
                        if DEBUG_MODE:
                            logger.debug(
                                "gather_command_child_routed",
                                child_command_id=cmd.command_id,
                                child_command_type=cmd.get_command_type(),
                                worker_id=worker_id,
                                queue_name=queue_name,
                                strategy_used=routing_decision.strategy_used,
                            )
                        
                        # Create task signature with explicit queue for per-worker routing
                        task_sig = process_distributed_command.s(command_data).set(  # type: ignore[attr-defined]
                            queue=queue_name
                        )
                        routed_tasks.append(task_sig)
                    else:
                        strict_reason = _get_strict_routing_reason(cmd)
                        if strict_reason:
                            error_message = (
                                "Strict routing required but no eligible worker found "
                                f"(reason={strict_reason}, error={routing_decision.error})"
                            )
                            logger.error(
                                "gather_command_child_routing_failed_strict",
                                child_command_id=cmd.command_id,
                                child_command_type=cmd.get_command_type(),
                                strict_reason=strict_reason,
                                error=routing_decision.error,
                            )
                            raise RuntimeError(error_message)
                        # Routing failed, use default routing
                        logger.warning(
                            "gather_command_child_routing_failed_fallback_default",
                            child_command_id=cmd.command_id,
                            child_command_type=cmd.get_command_type(),
                            error=routing_decision.error,
                        )
                        routed_tasks.append(process_distributed_command.s(command_data))  # type: ignore[attr-defined]
                
                except Exception as e:
                    strict_reason = _get_strict_routing_reason(cmd)
                    if strict_reason:
                        raise RuntimeError(
                            "Strict routing required but routing failed with exception "
                            f"(reason={strict_reason}): {e}"
                        ) from e
                    logger.warning(
                        "gather_command_child_routing_exception_fallback_default",
                        child_command_id=cmd.command_id,
                        child_command_type=cmd.get_command_type(),
                            error=str(e),
                        exc_info=True,
                    )
                    routed_tasks.append(process_distributed_command.s(command_data))  # type: ignore[attr-defined]
            else:
                strict_reason = _get_strict_routing_reason(cmd)
                if strict_reason:
                    raise RuntimeError(
                        "Strict routing required but WorkerRouter is unavailable "
                        f"(reason={strict_reason})"
                    )
                # No worker_router available, use default Celery routing
                routed_tasks.append(process_distributed_command.s(command_data))  # type: ignore[attr-defined]
        
        return routed_tasks
    
    def _deserialize_child_commands(self, worker_context: Dict[str, Any]) -> List[DistributedCommand]:
        """
        Deserialize child commands from command data.
        
        Args:
            worker_context: Worker execution context
            
        Returns:
            List of DistributedCommand instances
        """
        from motet.core.commands.distributed import DistributedCommand
        
        child_commands = []
        for cmd_json_string in self.data.commands:
            try:
                # self.data.commands contains JSON strings from serialize_for_transport()
                # Use deserialize_from_transport() which expects JSON strings
                cmd = DistributedCommand.deserialize_from_transport(cmd_json_string)
                
                # CRITICAL: Update parent_command_id to point to this GatherCommand
                # This ensures proper task hierarchy in Task Flow Visualization
                cmd.distributed_context.parent_command_id = self.command_id
                
                child_commands.append(cmd)
            except Exception as e:
                logger.warning(
                    "gather_command_child_deserialize_failed",
                    error=str(e),
                    exc_info=True,
                )
                # Continue with other commands
        
        return child_commands
    
    def _aggregate_results(
        self,
        results: List[Dict[str, Any]],
        child_commands: List[DistributedCommand],
        strategy: str
    ) -> Dict[str, Any]:
        """
        Aggregate results from parallel command execution.
        
        Args:
            results: List of command results (worker envelopes)
            child_commands: Original child commands
            strategy: Aggregation strategy ("all_results", "first_success", "majority_vote")
            
        Returns:
            Aggregated result data
        """
        if strategy == "all_results":
            return self._aggregate_all_results(results, child_commands)
        elif strategy == "first_success":
            return self._aggregate_first_success(results, child_commands)
        elif strategy == "majority_vote":
            return self._aggregate_majority_vote(results, child_commands)
        else:
            # Default to all_results
            return self._aggregate_all_results(results, child_commands)
    
    def _aggregate_all_results(
        self,
        results: List[Dict[str, Any]],
        child_commands: List[DistributedCommand]
    ) -> Dict[str, Any]:
        """
        Aggregate all results (default strategy).
        
        Emits one entry per child in **submission order** so ``motet.join()``
        positional unpacking matches the input list. Results are keyed by
        ``command_id`` first so scrambled completion order cannot reorder them.

        Wait-envelope format (mapped from ``cmd:outcome``):
        {
            "command_id": ...,
            "command_type": ...,
            "status": "success" | "error" | "timeout",
            "result": {...},  # ADR-0029 command response
            "error": {...},
            "worker_id": ...
        }
        """
        by_command_id: Dict[str, Dict[str, Any]] = {}
        for result in results:
            if isinstance(result, dict):
                cmd_id = result.get("command_id")
                if cmd_id:
                    by_command_id[cmd_id] = result

        ordered_results: List[Dict[str, Any]] = []
        failed_results: List[Dict[str, Any]] = []

        for cmd in child_commands:
            cmd_id = cmd.command_id
            cmd_type = cmd.get_command_type()
            result = by_command_id.get(cmd_id)

            if result is None:
                failure = child_command_envelope(
                    command_id=cmd_id,
                    command_type=cmd_type,
                    error={
                        "type": "MissingResult",
                        "message": "No Motet wait outcome received for child command",
                        "details": {},
                    },
                )
                ordered_results.append(failure)
                failed_results.append(failure)
                continue

            if not isinstance(result, dict):
                failure = child_command_envelope(
                    command_id=cmd_id,
                    command_type=cmd_type,
                    error={
                        "type": "InvalidResultType",
                        "message": f"Expected dict, got {type(result).__name__}",
                        "details": {},
                    },
                )
                ordered_results.append(failure)
                failed_results.append(failure)
                continue

            status = result.get("status")
            # Prefer observer command_type when present; fall back to deserialized child.
            result_cmd_type = result.get("command_type") or cmd_type

            if status == "success":
                command_response = result.get("result", {})
                if isinstance(command_response, dict) and command_response.get("status") == "success":
                    child_meta = command_response.get("metadata")
                    ordered_results.append(
                        child_command_envelope(
                            command_id=cmd_id,
                            command_type=result_cmd_type,
                            data=command_response.get("data"),
                            metadata=child_meta if isinstance(child_meta, dict) else None,
                            warnings=command_response.get("warnings")
                            if isinstance(command_response.get("warnings"), list)
                            else None,
                        )
                    )
                else:
                    failure = child_command_envelope(
                        command_id=cmd_id,
                        command_type=result_cmd_type,
                        error={
                            "type": "InvalidCommandResponse",
                            "message": "Success status but invalid command response format",
                            "details": {"response": command_response},
                        },
                    )
                    ordered_results.append(failure)
                    failed_results.append(failure)
            elif status in ["error", "timeout"]:
                err = result.get("error") or {
                    "type": "UnknownError",
                    "message": f"Command failed with status: {status}",
                    "details": {},
                }
                if not isinstance(err, dict):
                    err = {
                        "type": "UnknownError",
                        "message": str(err),
                        "details": {},
                    }
                failure = child_command_envelope(
                    command_id=cmd_id,
                    command_type=result_cmd_type,
                    error=err,
                )
                ordered_results.append(failure)
                failed_results.append(failure)
            else:
                failure = child_command_envelope(
                    command_id=cmd_id,
                    command_type=result_cmd_type,
                    error={
                        "type": "UnknownStatus",
                        "message": f"Unknown status: {status}",
                        "details": result,
                    },
                )
                ordered_results.append(failure)
                failed_results.append(failure)

        successful_count = len(ordered_results) - len(failed_results)
        return {
            # Authoritative in-order list: result[i] matches child_commands[i]
            "results": ordered_results,
            "failed_commands": failed_results,
            "total_commands": len(child_commands),
            "successful": successful_count,
            "failed": len(failed_results),
            "aggregation_strategy": "all_results",
        }
    
    def _aggregate_first_success(
        self,
        results: List[Dict[str, Any]],
        child_commands: List[DistributedCommand]
    ) -> Dict[str, Any]:
        """
        Return first successful result (future implementation).
        
        For MVP, this delegates to all_results.
        """
        # TODO: Implement first_success strategy
        # For now, return all results
        return self._aggregate_all_results(results, child_commands)
    
    def _aggregate_majority_vote(
        self,
        results: List[Dict[str, Any]],
        child_commands: List[DistributedCommand]
    ) -> Dict[str, Any]:
        """
        Return most common result (future implementation).
        
        For MVP, this delegates to all_results.
        """
        # TODO: Implement majority_vote strategy
        # For now, return all results
        return self._aggregate_all_results(results, child_commands)
    
    @classmethod
    def create(
        cls,
        commands: List[DistributedCommand],
        aggregation_strategy: str = "all_results",
        fail_fast: bool = False,
        max_parallel: Optional[int] = None,
        **kwargs
    ) -> 'GatherCommand':
        """
        Convenience factory method to create a GatherCommand.
        
        Args:
            commands: List of DistributedCommand instances to execute in parallel
            aggregation_strategy: How to aggregate results ("all_results", "first_success", "majority_vote")
            fail_fast: Stop on first failure
            max_parallel: Limit concurrent execution
            **kwargs: Additional command parameters (timeout, priority, etc.)
            
        Returns:
            GatherCommand instance
        """
        # Serialize commands to JSON strings for transport
        # Note: serialize_for_transport() already returns JSON strings
        serialized_commands = [
            cmd.serialize_for_transport() 
            for cmd in commands
        ]
        
        # Create command data
        data = GatherCommandData(
            commands=serialized_commands,
            aggregation_strategy=aggregation_strategy,
            fail_fast=fail_fast,
            max_parallel=max_parallel
        )
        
        # Ensure task_id is provided
        if 'task_id' not in kwargs:
            # Generate a UUID without prefix
            import uuid
            kwargs['task_id'] = str(uuid.uuid4())

        kwargs["timeout_seconds"] = join_celery_timeout_seconds(
            requested=kwargs.get("timeout_seconds"),
            child_timeouts=[
                _safe_timeout_seconds(
                    getattr(getattr(cmd, "distributed_context", None), "timeout_seconds", None)
                )
                for cmd in commands
            ],
            default=DEFAULT_JOIN_TIMEOUT_SECONDS,
        )
        
        # Create and return GatherCommand
        return cls(task_id=kwargs.pop('task_id'), data=data, **kwargs)


class DispatchCommand(DistributedCommand):
    """Fire-and-forget parallel command dispatch without waiting for child results."""
    
    def __init__(
        self,
        task_id: str,
        data: DispatchCommandData,
        conversation_id: str = "",
        tenant_id: str = "",
        principal_id: str = "",
        parent_command_id: Optional[str] = None,
        command_id: Optional[str] = None,  # Accept command_id for deserialization
        metadata: Optional[Dict[str, Any]] = None,
        timeout_seconds: Optional[int] = None,
        priority: Optional[int] = None,
        **kwargs  # Accept all other distributed kwargs (trace_id, parent_span_id, etc.)
    ):
        # Filter out None values before passing to parent __init__
        init_kwargs = {
            'task_id': task_id,
            'data': data,
            'conversation_id': conversation_id,
            'tenant_id': tenant_id,
            'principal_id': principal_id,
        }
        
        # Pass command_id to parent if provided (for deserialization)
        if command_id is not None:
            init_kwargs['command_id'] = command_id
        
        if parent_command_id is not None:
            init_kwargs['parent_command_id'] = parent_command_id
        if metadata is not None:
            init_kwargs['metadata'] = metadata
        if timeout_seconds is not None:
            init_kwargs['timeout_seconds'] = timeout_seconds
        if priority is not None:
            init_kwargs['priority'] = priority
        
        # Pass through any additional distributed kwargs (trace_id, parent_span_id, etc.)
        init_kwargs.update(kwargs)
        
        super().__init__(**init_kwargs)
        
        # Set required capabilities (same as GatherCommand)
        self.distributed_context.required_capabilities = set([
            WorkerCapability.TOOL_EXECUTION,
        ])
    
    @classmethod
    def _get_data_class(cls) -> Type[DispatchCommandData]:
        """Return the data class for this command."""
        return DispatchCommandData
    
    def get_command_type(self) -> str:
        """Return the command type identifier."""
        return "core.dispatch"
    
    def can_undo(self) -> bool:
        """DispatchCommand execution cannot be undone."""
        return False
    
    def undo(self, worker_context: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
        """Undo is not supported for DispatchCommand."""
        raise NotImplementedError("DispatchCommand execution cannot be undone")
    
    def _do_execute(self, worker_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Dispatch child commands without waiting (fire-and-forget).
        
        Args:
            worker_context: Worker execution context
            
        Returns:
            ADR-0029 compliant response with list of dispatched command IDs
        """
        if DEBUG_MODE:
            logger.debug("dispatch_command_start", command_id=self.command_id)
        start_time = time.time()
        
        # Store worker ID for metadata (ADR-0029)
        self._worker_id = worker_context.get("worker_id")
        
        try:
            # Deserialize child commands from command data
            if DEBUG_MODE:
                logger.debug(
                    "dispatch_command_deserialize_children_start",
                    command_id=self.command_id,
                    child_count=len(self.data.commands),
                )
            child_commands = self._deserialize_child_commands(worker_context)
            if DEBUG_MODE:
                logger.debug(
                    "dispatch_command_deserialize_children_complete",
                    command_id=self.command_id,
                    child_count=len(child_commands),
                )
            
            if not child_commands:
                execution_time_ms = (time.time() - start_time) * 1000
                return self._create_success_response(
                    data={
                        "dispatched": [],
                        "total_commands": 0
                    },
                    execution_time_ms=execution_time_ms,
                    warnings=["No child commands provided"]
                )
            
            logger.debug(
                "dispatch_command_dispatching",
                command_id=self.command_id,
                child_count=len(child_commands),
            )
            
            # Get worker router from context for intelligent routing
            worker_router = worker_context.get('worker_router')
            if not worker_router:
                logger.warning(
                    "dispatch_command_no_worker_router_fallback_basic_routing",
                    command_id=self.command_id,
                )
                worker_router = None
            
            # Route each child command through WorkerRouter for capability-based routing
            routed_tasks = self._create_routed_tasks(child_commands, worker_router)
            
            # Dispatch all tasks without waiting (fire-and-forget)
            from celery import group as celery_group
            celery_group(routed_tasks).apply_async()
            
            execution_time_ms = (time.time() - start_time) * 1000
            
            # Collect command IDs
            dispatched_ids = [cmd.command_id for cmd in child_commands]
            
            logger.debug(
                "dispatch_command_dispatched",
                command_id=self.command_id,
                dispatched_count=len(dispatched_ids),
                execution_time_ms=execution_time_ms,
            )
            
            return self._create_success_response(
                data={
                    "dispatched": dispatched_ids,
                    "total_commands": len(dispatched_ids),
                    "note": "Commands dispatched without waiting for results"
                },
                execution_time_ms=execution_time_ms
            )
            
        except Exception as e:
            execution_time_ms = (time.time() - start_time) * 1000
            logger.error(
                "dispatch_command_failed",
                command_id=self.command_id,
                error=str(e),
                exc_info=True,
            )
            
            return self._create_error_response(
                error=e,
                execution_time_ms=execution_time_ms
            )
    
    def _deserialize_child_commands(self, worker_context: Dict[str, Any]) -> List[DistributedCommand]:
        """
        Deserialize child commands from command data.
        
        Args:
            worker_context: Worker execution context
            
        Returns:
            List of DistributedCommand instances
        """
        from motet.core.commands.distributed import DistributedCommand
        
        child_commands = []
        for cmd_json_string in self.data.commands:
            try:
                # self.data.commands contains JSON strings from serialize_for_transport()
                # Use deserialize_from_transport() which expects JSON strings
                cmd = DistributedCommand.deserialize_from_transport(cmd_json_string)
                
                # CRITICAL: Set parent_command_id to point to this DispatchCommand
                # This ensures proper task hierarchy in Task Flow Visualization
                # Dispatched commands will show up as children of DispatchCommand
                cmd.distributed_context.parent_command_id = self.command_id
                
                child_commands.append(cmd)
            except Exception as e:
                logger.warning(
                    "dispatch_command_child_deserialize_failed",
                    error=str(e),
                    exc_info=True,
                )
                # Continue with other commands
        
        return child_commands
    
    def _create_routed_tasks(
        self,
        child_commands: List[DistributedCommand],
        worker_router: Optional[Any]
    ) -> List[Any]:
        """
        Create routed Celery tasks for child commands.
        
        Args:
            child_commands: List of commands to route
            worker_router: WorkerRouter instance for capability-based routing
            
        Returns:
            List of Celery signature objects
        """
        from motet.core.workers.command_tasks import process_distributed_command
        
        routed_tasks = []
        for cmd in child_commands:
            # Serialize command for transport
            command_data = cmd.serialize_for_transport()
            
            if worker_router:
                # Use WorkerRouter for capability-based routing
                target_worker = worker_router.select_worker_for_command(cmd)
                if target_worker:
                    worker_id = target_worker['worker_id']
                    queue_name = f"worker.{worker_id}"
                    routed_tasks.append(
                        process_distributed_command.s(command_data).set(queue=queue_name)  # type: ignore[attr-defined]
                    )
                else:
                    strict_reason = _get_strict_routing_reason(cmd)
                    if strict_reason:
                        raise RuntimeError(
                            "Strict routing required but no eligible worker found "
                            f"(reason={strict_reason})"
                        )
                    # No suitable worker, use default routing
                    routed_tasks.append(process_distributed_command.s(command_data))  # type: ignore[attr-defined]
            else:
                strict_reason = _get_strict_routing_reason(cmd)
                if strict_reason:
                    raise RuntimeError(
                        "Strict routing required but WorkerRouter is unavailable "
                        f"(reason={strict_reason})"
                    )
                # No worker_router available, use default Celery routing
                routed_tasks.append(process_distributed_command.s(command_data))  # type: ignore[attr-defined]
        
        return routed_tasks
    
    @classmethod
    def create(
        cls,
        commands: List[DistributedCommand],
        max_parallel: Optional[int] = None,
        **kwargs
    ) -> 'DispatchCommand':
        """
        Convenience factory method to create a DispatchCommand.
        
        Args:
            commands: List of DistributedCommand instances to dispatch
            max_parallel: Limit concurrent execution
            **kwargs: Additional command parameters (timeout, priority, etc.)
            
        Returns:
            DispatchCommand instance
        """
        # Serialize commands to JSON strings for transport
        # Note: serialize_for_transport() already returns JSON strings
        serialized_commands = [
            cmd.serialize_for_transport() 
            for cmd in commands
        ]
        
        # Create command data
        data = DispatchCommandData(
            commands=serialized_commands,
            max_parallel=max_parallel
        )
        
        # Ensure task_id is provided
        if 'task_id' not in kwargs:
            # Generate a UUID without prefix
            import uuid
            kwargs['task_id'] = str(uuid.uuid4())
        
        # Filter out None values to avoid overriding defaults in __init__
        filtered_kwargs = {k: v for k, v in kwargs.items() if v is not None}
        
        # Create and return DispatchCommand
        return cls(task_id=filtered_kwargs.pop('task_id'), data=data, **filtered_kwargs)


class MapCommand(DistributedCommand):
    """Batch-map the same command across many inputs in parallel (map-reduce style apply)."""
    
    def __init__(
        self,
        task_id: str,
        data: MapCommandData,
        conversation_id: str = "",
        tenant_id: str = "",
        principal_id: str = "",
        parent_command_id: Optional[str] = None,
        command_id: Optional[str] = None,  # Accept command_id for deserialization
        metadata: Optional[Dict[str, Any]] = None,
        timeout_seconds: Optional[int] = None,
        priority: Optional[int] = None,
        **kwargs  # Accept all other distributed kwargs (trace_id, parent_span_id, etc.)
    ):
        # Filter out None values before passing to parent __init__
        init_kwargs = {
            'task_id': task_id,
            'data': data,
            'conversation_id': conversation_id,
            'tenant_id': tenant_id,
            'principal_id': principal_id,
        }
        
        # Pass command_id to parent if provided (for deserialization)
        if command_id is not None:
            init_kwargs['command_id'] = command_id
        
        if parent_command_id is not None:
            init_kwargs['parent_command_id'] = parent_command_id
        if metadata is not None:
            init_kwargs['metadata'] = metadata
        if timeout_seconds is not None:
            init_kwargs['timeout_seconds'] = timeout_seconds
        if priority is not None:
            init_kwargs['priority'] = priority
        
        # Pass through any additional distributed kwargs (trace_id, parent_span_id, etc.)
        init_kwargs.update(kwargs)
        
        super().__init__(**init_kwargs)
        
        # Set required capabilities based on command type being mapped
        # Note: For now, default to tool_execution; could be made dynamic
        self.distributed_context.required_capabilities = set([
            WorkerCapability.TOOL_EXECUTION,
        ])
    
    @classmethod
    def _get_data_class(cls) -> Type[MapCommandData]:
        """Return the data class for this command."""
        return MapCommandData
    
    def get_command_type(self) -> str:
        """Return the command type identifier."""
        return "core.map"
    
    def can_undo(self) -> bool:
        """MapCommand execution cannot be undone."""
        return False
    
    def undo(self, worker_context: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
        """Undo is not supported for MapCommand."""
        raise NotImplementedError("MapCommand execution cannot be undone")
    
    def _do_execute(self, worker_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute same command type across multiple inputs (batch processing).
        
        Uses Motet ``cmd:outcome`` waits for fan-in (issue #242).
        
        Args:
            worker_context: Worker execution context
            
        Returns:
            ADR-0029 compliant response with aggregated batch results
        """
        start_time = time.time()
        
        # Store worker ID for metadata (ADR-0029)
        self._worker_id = worker_context.get("worker_id")
        
        try:
            # Create command instances from template + inputs
            command_instances = self._create_command_instances()
            
            if not command_instances:
                execution_time_ms = (time.time() - start_time) * 1000
                return self._create_success_response(
                    data={
                        "results": [],
                        "total_inputs": 0,
                        "successful": 0,
                        "failed": 0
                    },
                    execution_time_ms=execution_time_ms,
                    warnings=["No inputs provided for batch processing"]
                )
            
            logger.debug(
                "map_command_start",
                command_id=self.command_id,
                target_command_type=self.data.command_type,
                instance_count=len(command_instances),
            )
            
            # Preserve input order so callers (e.g. publish_bundle) can match results[i] to live_workers[i]
            ordered_command_ids = [cmd.command_id for cmd in command_instances]

            worker_router = worker_context.get('worker_router')
            if not worker_router:
                logger.warning(
                    "map_command_no_worker_router_fallback_basic_routing",
                    command_id=self.command_id,
                )
                worker_router = None

            routed_tasks = self._create_routed_tasks(command_instances, worker_router)

            total_instances = len(command_instances)
            batch_size = self.data.batch_size
            if batch_size is None:
                batch_size = getattr(self.data, "max_parallel", None)

            timeout = join_wait_timeout_seconds(
                own_timeout=self.distributed_context.timeout_seconds,
                child_max=_safe_timeout_seconds(self.distributed_context.timeout_seconds),
                default=DEFAULT_JOIN_TIMEOUT_SECONDS,
            )
            if batch_size is not None:
                try:
                    batch_size = int(batch_size)
                except Exception:
                    batch_size = None
            if batch_size is not None and batch_size < 1:
                batch_size = None

            ctx = self.distributed_context
            cancel_scopes = getattr(ctx, "cancel_scopes", None)
            tenant_id = getattr(ctx, "tenant_id", None)
            motet_id = getattr(ctx, "motet_id", None)
            task_id = getattr(ctx, "task_id", None)
            deadline = time.time() + float(timeout)
            completed_commands: Dict[str, Dict[str, Any]] = {}

            def _ingest(chunk_cmds: List[DistributedCommand], chunk_waiters: List[str]) -> bool:
                remaining = max(0.0, deadline - time.time())
                chunk_results = collect_child_wait_results(
                    task_id=task_id,
                    child_commands=chunk_cmds,
                    waiter_ids=chunk_waiters,
                    timeout_seconds=remaining,
                    cancel_scopes=cancel_scopes,
                    tenant_id=tenant_id,
                    motet_id=motet_id,
                )
                timed_out = False
                for item in chunk_results:
                    cmd_id = item.get("command_id")
                    if cmd_id:
                        completed_commands[str(cmd_id)] = item
                    if item.get("status") == "timeout":
                        timed_out = True
                return not timed_out

            if batch_size is None or batch_size >= total_instances:
                waiter_ids = _dispatch_signatures(routed_tasks)
                ok = _ingest(command_instances, waiter_ids)
            else:
                if DEBUG_MODE:
                    logger.debug(
                        "map_command_chunked_dispatch_start",
                        command_id=self.command_id,
                        instance_count=total_instances,
                        batch_size=batch_size,
                        timeout_seconds=timeout,
                    )
                ok = True
                for start_idx in range(0, total_instances, batch_size):
                    end_idx = min(start_idx + batch_size, total_instances)
                    waiter_ids = _dispatch_signatures(routed_tasks[start_idx:end_idx])
                    if not _ingest(command_instances[start_idx:end_idx], waiter_ids):
                        ok = False
                        break

            if DEBUG_MODE:
                logger.debug(
                    "map_command_waiting_for_completion",
                    command_id=self.command_id,
                    instance_count=len(command_instances),
                    timeout_seconds=timeout,
                )

            if not ok or len(completed_commands) < total_instances:
                execution_time_ms = (time.time() - start_time) * 1000
                return self._create_error_response(
                    error=TimeoutError(
                        f"MapCommand timed out after {timeout}s waiting for batch completion "
                        f"(completed={len(completed_commands)}/{total_instances})"
                    ),
                    execution_time_ms=execution_time_ms,
                )

            aggregated_result = self._aggregate_batch_results(
                completed_commands,
                ordered_command_ids=ordered_command_ids,
            )
            execution_time_ms = (time.time() - start_time) * 1000

            logger.debug(
                "map_command_completed",
                command_id=self.command_id,
                execution_time_ms=execution_time_ms,
                successful=aggregated_result.get("data", {}).get("successful"),
                total_inputs=aggregated_result.get("data", {}).get("total_inputs"),
            )

            aggregated_result['metadata']['execution_time_ms'] = execution_time_ms
            aggregated_result['metadata']['command_id'] = self.command_id
            aggregated_result['metadata']['command_type'] = 'map'
            aggregated_result['metadata']['worker_id'] = self._worker_id

            return aggregated_result

        except Exception as e:
            execution_time_ms = (time.time() - start_time) * 1000
            logger.error(
                "map_command_failed",
                command_id=self.command_id,
                error=str(e),
                exc_info=True,
            )
            
            return self._create_error_response(
                error=e,
                execution_time_ms=execution_time_ms
            )
    
    def _create_command_instances(self) -> List[DistributedCommand]:
        """
        Create command instances from template + inputs.
        
        Instantiates N commands of the same type with different data inputs.
        
        Returns:
            List of DistributedCommand instances
        """
        from motet.core.commands.distributed import DistributedCommand
        from motet.core.commands.command_type_registry import command_type_registry
        
        command_instances = []
        
        for idx, input_data in enumerate(self.data.inputs):
            try:
                # Merge template with input data
                merged_data = {**self.data.command_template, **input_data}
                
                # Get registration from registry
                registration = command_type_registry.get(self.data.command_type)
                if not registration:
                    logger.warning(
                        "map_command_unknown_command_type_skipping_input",
                        command_type=self.data.command_type,
                        input_index=idx,
                    )
                    continue
                
                # Get data class from registry
                from motet.core.commands.command_data_registry import command_data_registry
                data_class = command_data_registry.get(self.data.command_type)
                if not data_class:
                    logger.warning(
                        "map_command_missing_data_class_skipping_input",
                        command_type=self.data.command_type,
                        input_index=idx,
                    )
                    continue
                
                # Create command data instance
                command_data = data_class(**merged_data)
                
                # Generate unique command_id for each instance
                import uuid
                command_id = str(uuid.uuid4())
                
                # Propagate parent envelope metadata to map children so downstream
                # commands/tool execution preserve model/chat context
                # (e.g., model_provider/model_name/model_profile_name).
                parent_metadata = dict(self.distributed_context.metadata or {})

                # Use registry to instantiate command (handles both class-based and decorated)
                cmd = command_type_registry.instantiate_command(
                    command_type=self.data.command_type,
                    data=command_data,
                    task_id=self.distributed_context.task_id,
                    conversation_id=self.distributed_context.conversation_id,
                    tenant_id=self.distributed_context.tenant_id,
                    principal_id=self.distributed_context.principal_id,
                    motet_id=self.distributed_context.motet_id,
                    parent_command_id=self.command_id,  # This MapCommand is the parent
                    metadata=parent_metadata,
                    timeout_seconds=self.distributed_context.timeout_seconds,
                    priority=self.distributed_context.priority
                )
                
                # Override command_id to ensure uniqueness
                cmd.command_id = command_id

                # Propagate per-input target_worker_id so each map item is routed to the intended worker
                # (e.g. publish_bundle sends one reload_bundle per worker with target_worker_id set).
                target_worker_id = getattr(command_data, "target_worker_id", None)
                if target_worker_id and hasattr(cmd, "distributed_context") and cmd.distributed_context:
                    cmd.distributed_context.target_worker_id = target_worker_id

                command_instances.append(cmd)
                
            except Exception as e:
                logger.warning(
                    "map_command_create_instance_failed",
                    command_type=self.data.command_type,
                    input_index=idx,
                    error=str(e),
                    exc_info=True,
                )
                # Continue with other inputs
        
        return command_instances
    
    def _create_routed_tasks(
        self,
        command_instances: List[DistributedCommand],
        worker_router: Optional[Any]
    ) -> List[Any]:
        """
        Create routed Celery tasks for command instances.
        
        Args:
            command_instances: List of commands to route
            worker_router: WorkerRouter instance for capability-based routing
            
        Returns:
            List of Celery signature objects
        """
        from motet.core.workers.command_tasks import process_distributed_command
        
        routed_tasks = []
        for cmd in command_instances:
            # Serialize command for transport
            command_data = cmd.serialize_for_transport()
            
            if worker_router:
                # Use WorkerRouter for capability-based routing
                target_worker = worker_router.select_worker_for_command(cmd)
                if target_worker:
                    worker_id = target_worker['worker_id']
                    queue_name = f"worker.{worker_id}"
                    routed_tasks.append(
                        process_distributed_command.s(command_data).set(queue=queue_name)  # type: ignore[attr-defined]
                    )
                else:
                    # For explicit per-command targeting, never fall back to generic routing.
                    target_worker_id = getattr(getattr(cmd, "distributed_context", None), "target_worker_id", None)
                    if target_worker_id:
                        queue_name = f"worker.{target_worker_id}"
                        logger.warning(
                            "map_command_targeted_routing_miss_direct_queue_fallback",
                            command_id=getattr(cmd, "command_id", None),
                            command_type=getattr(cmd, "get_command_type", lambda: "unknown")(),
                            target_worker_id=target_worker_id,
                        )
                        routed_tasks.append(
                            process_distributed_command.s(command_data).set(queue=queue_name)  # type: ignore[attr-defined]
                        )
                    else:
                        strict_reason = _get_strict_routing_reason(cmd)
                        if strict_reason:
                            raise RuntimeError(
                                "Strict routing required but no eligible worker found "
                                f"(reason={strict_reason})"
                            )
                        # No suitable worker, use default routing
                        routed_tasks.append(process_distributed_command.s(command_data))  # type: ignore[attr-defined]
            else:
                strict_reason = _get_strict_routing_reason(cmd)
                if strict_reason:
                    raise RuntimeError(
                        "Strict routing required but WorkerRouter is unavailable "
                        f"(reason={strict_reason})"
                    )
                # No worker_router available, use default Celery routing
                routed_tasks.append(process_distributed_command.s(command_data))  # type: ignore[attr-defined]
        
        return routed_tasks
    
    def _aggregate_batch_results(
        self,
        completed_commands: Dict[str, Dict[str, Any]],
        ordered_command_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Aggregate results from batch processing with batch-specific metadata.

        Args:
            completed_commands: Dict of {command_id: result_data}
            ordered_command_ids: If provided, emit results in this order (so result[i] matches input i).

        Returns:
            ADR-0029 compliant response with batch statistics
        """
        ordered_results = []
        failed_results = []

        # Iterate in input order when provided so callers (e.g. publish_bundle) can use results[i] for input i
        iterate_over = ordered_command_ids if ordered_command_ids else completed_commands.keys()
        for cmd_id in iterate_over:
            result = completed_commands.get(cmd_id)
            if result is None:
                continue

            event_status = result.get("status", "unknown")
            adr_response = result.get("result", {})
            nested_status = (
                adr_response.get("status")
                if isinstance(adr_response, dict) else None
            )

            # IMPORTANT:
            # Envelope status reflects transport/execution completion of the child.
            # Success accounting for map/apply must use the child command's ADR-0029 status
            # from the nested response (result.status), not only the wait envelope status.
            is_command_success = (event_status == "success" and nested_status == "success")

            if is_command_success:
                child_meta = (
                    adr_response.get("metadata")
                    if isinstance(adr_response, dict)
                    else None
                )
                ordered_results.append(
                    child_command_envelope(
                        command_id=cmd_id,
                        command_type=self.data.command_type,
                        data=adr_response.get("data"),
                        metadata=child_meta if isinstance(child_meta, dict) else None,
                        warnings=(
                            adr_response.get("warnings")
                            if isinstance(adr_response, dict)
                            and isinstance(adr_response.get("warnings"), list)
                            else None
                        ),
                    )
                )
                continue

            error_payload = None
            if isinstance(adr_response, dict):
                error_payload = adr_response.get("error")
            if not error_payload:
                error_payload = result.get("error")
            if not isinstance(error_payload, dict):
                error_payload = {
                    "type": "BatchItemFailed",
                    "message": (
                        f"Batch item failed (event_status={event_status}, "
                        f"nested_status={nested_status or 'unknown'})"
                    ),
                    "details": {},
                }

            failure_entry = child_command_envelope(
                command_id=cmd_id,
                command_type=self.data.command_type,
                error=error_payload,
            )
            ordered_results.append(failure_entry)
            failed_results.append(failure_entry)
        
        total_inputs = len(self.data.inputs)
        successful_count = len([r for r in ordered_results if r.get("status") == "success"])
        failed_count = len(failed_results)
        
        # Determine overall status
        if failed_count == 0:
            overall_status = 'success'
        elif successful_count == 0:
            overall_status = 'error'
        else:
            overall_status = 'partial_success'
        
        response = {
            'status': overall_status,
            'data': {
                # results is the authoritative in-order list: one entry per input (success or failure)
                'results': ordered_results,
                'total_inputs': total_inputs,
                'successful': successful_count,
                'failed': failed_count,
                'success_rate': f"{(successful_count / total_inputs * 100):.1f}%" if total_inputs > 0 else "0%"
            },
            'metadata': {
                'command_type': self.data.command_type,
                'batch_size': self.data.batch_size,
                'aggregation_strategy': self.data.aggregation_strategy
            },
            'warnings': []
        }
        
        # Add failed results if any
        if failed_results:
            response['data']['failures'] = failed_results
            response['warnings'].append(f"{failed_count} of {total_inputs} batch items failed")
        
        # Add error for complete failure
        if overall_status == 'error':
            response['error'] = {
                'type': 'BatchProcessingError',
                'message': f'All {total_inputs} batch items failed',
                'details': {'failures': failed_results}
            }
        
        return response
    
    @classmethod
    def create(
        cls,
        command_type: str,
        inputs: List[Dict[str, Any]],
        command_template: Optional[Dict[str, Any]] = None,
        batch_size: Optional[int] = None,
        aggregation_strategy: str = "all_results",
        fail_fast: bool = False,
        **kwargs
    ) -> 'MapCommand':
        """
        Convenience factory method to create a MapCommand.
        
        Args:
            command_type: Type of command to instantiate (e.g., "tool_execution")
            inputs: List of input variations for each command instance
            command_template: Base command parameters (shared across all instances)
            batch_size: Limit concurrent execution
            aggregation_strategy: How to aggregate results
            fail_fast: Stop on first failure
            **kwargs: Additional command parameters (task_id, conversation_id, etc.)
            
        Returns:
            MapCommand instance
        """
        # Create command data
        data = MapCommandData(
            command_type=command_type,
            command_template=command_template or {},
            inputs=inputs,
            batch_size=batch_size,
            aggregation_strategy=aggregation_strategy,
            fail_fast=fail_fast
        )
        
        # Ensure task_id is provided
        if 'task_id' not in kwargs:
            # Generate a UUID without prefix
            import uuid
            kwargs['task_id'] = str(uuid.uuid4())
        
        # Create and return MapCommand
        return cls(task_id=kwargs.pop('task_id'), data=data, **kwargs)


__all__ = [
    "GatherCommand",
    "DispatchCommand",
    "MapCommand",
    "DEFAULT_JOIN_TIMEOUT_SECONDS",
    "join_celery_timeout_seconds",
    "join_wait_timeout_seconds",
]

# Register command types with the base class
from motet.core.commands.distributed import DistributedCommand
DistributedCommand.register_command_type(GatherCommand)
DistributedCommand.register_command_type(DispatchCommand)
DistributedCommand.register_command_type(MapCommand)

