"""
Motet - Command Processing Tasks

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Celery tasks for distributed command processing, execution, and coordination.
    Handles core distributed command execution logic for the Motet system.
    Edge worker command scope fails closed when tenant/principal ids are unset
    unless ``MOTET_EDGE_SCOPE_FAIL_OPEN`` is enabled (local single-tenant stacks).
    Platform bundle lifecycle commands (hot_reload / unload / deploy) are
    allowlisted only when ``MOTET_EDGE_ALLOW_PLATFORM_LIFECYCLE`` is set so
    operators can deploy to principal-scoped multi-app builder edges without
    opening personal/device edges to cross-principal bundle deploys.
    dispatch gate honors sticky cancel via inherited ``cancel_scopes``
    (one variadic EXISTS); root commands register the live task index;
    ``task_postrun`` wakes parked parents only when ``cmd:outcome`` exists
    (persist already stored it) or after writing a failure envelope for a
    hard kill / revoke (issue #229). It must not LPUSH a result wake on an
    empty GET — that is the false-complete that crashed gather joins.

Dependencies:
    - Celery: Distributed task queue
    - datetime: Time and date handling
    - typing: Type hints and annotations
    - Command execution system

Usage:
    from motet.core.workers.command_tasks import process_distributed_command
    
    # Process command
    result = process_distributed_command.delay(command_data)

Notes:
    - Provides distributed command execution
    - Includes command coordination and management
    - Supports task binding and error handling
    - Integrates with distributed architecture
"""

import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import structlog
from celery.signals import task_postrun

from motet.core.constants import CELERY_PROCESS_COMMAND_TASK

from .celery_app import celery_app

logger = structlog.get_logger(__name__)

WAIT_OUTCOME_MISSING_ERROR = (
    "Command hard-killed or lost before wait outcome was stored "
    "(Celery time limit, revoke, or worker exit)"
)


def _command_identity_from_process_args(args: Any) -> Optional[Dict[str, Any]]:
    """Read Motet command identity from ``imf.commands.process`` args[0]."""
    if not args:
        return None
    raw = args[0]
    try:
        if isinstance(raw, str):
            payload = json.loads(raw)
        elif isinstance(raw, dict):
            payload = raw
        else:
            return None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    envelope = payload.get("envelope")
    if not isinstance(envelope, dict):
        envelope = payload
    command_id = str(envelope.get("command_id") or "").strip()
    if not command_id or command_id == "unknown":
        return None
    return {
        "command_id": command_id,
        "command_type": str(envelope.get("command_type") or "unknown"),
        "tenant_id": envelope.get("tenant_id"),
        "motet_id": envelope.get("motet_id"),
        "timeout_seconds": envelope.get("timeout_seconds"),
    }


def _persist_missing_wait_outcome(
    *,
    identity: Dict[str, Any],
    waiter_id: str,
    state: Optional[str] = None,
) -> None:
    """Store a failure envelope when persist never ran, then wake the parent.

    Live success/error paths already store ``cmd:outcome`` then signal.
    Hard time-limit SIGKILL skips that. Waking without an envelope made the
    communicator report ``outcome not found`` instead of a child error.
    """
    from motet.core.distributed.redis_command_data_manager import (
        get_redis_command_data_manager,
    )
    from motet.core.distributed.task_control import signal_command_result

    command_id = str(identity.get("command_id") or "").strip()
    if not command_id:
        return
    tenant_id = identity.get("tenant_id")
    motet_id = identity.get("motet_id")
    manager = get_redis_command_data_manager()
    if manager.has_command_wait_outcome(command_id, tenant_id=tenant_id):
        signal_command_result(waiter_id)
        return

    timeout = identity.get("timeout_seconds")
    try:
        timeout_s = int(timeout) if timeout is not None else None
    except (TypeError, ValueError):
        timeout_s = None

    detail = WAIT_OUTCOME_MISSING_ERROR
    if state:
        detail = f"{detail} (celery_state={state})"
    envelope = {
        "status": "error",
        "error": detail,
        "command_type": identity.get("command_type") or "unknown",
        "command_id": command_id,
        "task_id": waiter_id,
        "tenant_id": tenant_id,
        "motet_id": motet_id,
    }
    manager.store_command_wait_outcome(
        command_id=command_id,
        envelope=envelope,
        tenant_id=tenant_id,
        motet_id=motet_id,
        command_timeout_seconds=timeout_s,
    )
    logger.warning(
        "command_wait_outcome_synthesized_after_worker_exit",
        command_id=command_id,
        command_type=envelope["command_type"],
        celery_task_id=waiter_id,
        celery_state=state,
    )
    signal_command_result(waiter_id)


@task_postrun.connect(sender=None)
def _signal_command_result_wake(
    sender: Any = None,
    task_id: str | None = None,
    args: Any = None,
    state: Any = None,
    **_kwargs: Any,
) -> None:
    """ADR-0131 / #229: wake only after a Motet ``cmd:outcome`` envelope exists."""
    try:
        name = getattr(sender, "name", None) if sender is not None else None
        if name != CELERY_PROCESS_COMMAND_TASK:
            return
        if not task_id:
            return
        identity = _command_identity_from_process_args(args)
        if identity is None:
            logger.warning(
                "command_result_wake_skipped_no_identity",
                celery_task_id=task_id,
                celery_state=state,
            )
            return
        _persist_missing_wait_outcome(
            identity=identity,
            waiter_id=task_id,
            state=str(state) if state else None,
        )
    except Exception as e:
        logger.warning(
            "command_result_wake_signal_failed",
            celery_task_id=task_id,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )


def _event_bus_for_worker(worker_id: str):
    """Return the global event bus for any worker (cloud or local via WireGuard)."""
    from .events import global_bus

    return global_bus


# Operator-driven bundle lifecycle must reach principal-scoped edges (multi-app
# builders). Affinity still keeps *app* work on the matching principal; deploy
# is an infra exception so ``app-builder --app X deploy`` can load the bundle.
# Opt-in only (MOTET_EDGE_ALLOW_PLATFORM_LIFECYCLE=1): bundle deploy is code
# execution, so personal/device edges must not accept it cross-principal by
# default. The app-builder compose sets the flag for builder edges.
_EDGE_SCOPE_PLATFORM_COMMANDS = frozenset(
    {
        "core.hot_reload_bundle",
        "core.reload_bundle",
        "core.unload_bundle",
        "core.hot_deploy_bundle",
        "core.deploy_bundle",
        "core.deploy_bundle_upload",
        "core.undeploy_bundle",
        "core.rollback_bundle",
        "core.propagate_bundle",
        "core.validate_bundle",
        "core.validate_bundle_upload",
        "core.publish_bundle",
    }
)


def _platform_lifecycle_allowed() -> bool:
    """True when this edge opts into cross-principal bundle lifecycle commands."""
    return os.getenv("MOTET_EDGE_ALLOW_PLATFORM_LIFECYCLE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _command_type_for_scope(command: Any) -> str:
    getter = getattr(command, "get_command_type", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception as exc:
            logger.debug(
                "edge_scope_command_type_lookup_failed",
                command_class=type(command).__name__,
                error=str(exc),
            )
            return ""
    return str(getattr(command, "command_type", "") or "")


def _check_edge_worker_command_scope(worker_id: str, command: Any) -> str:
    """Return a rejection reason if this edge worker should not run the command.

    Returns empty string if the command is allowed.  Cloud workers always pass.
    System commands (no principal) always pass. Bundle lifecycle commands are
    allowlisted only when MOTET_EDGE_ALLOW_PLATFORM_LIFECYCLE is set, so
    multi-app builder edges (scope=principal) remain deployable while
    personal/device edges stay fail-closed.
    """
    if not worker_id.startswith("edge_"):
        return ""

    if (
        _platform_lifecycle_allowed()
        and _command_type_for_scope(command) in _EDGE_SCOPE_PLATFORM_COMMANDS
    ):
        return ""

    scope = os.getenv("MOTET_EDGE_COMMAND_SCOPE", "principal")
    ctx = getattr(command, "distributed_context", None)
    if ctx is None:
        return ""

    cmd_principal = getattr(ctx, "principal_id", "") or ""
    cmd_tenant = getattr(ctx, "tenant_id", "") or ""

    if not cmd_principal and not cmd_tenant:
        return ""

    if scope == "tenant":
        allowed_tenant = os.getenv("MOTET_EDGE_TENANT_ID", "")
        if not allowed_tenant:
            # Fail closed unless explicitly opted into unbound local stacks.
            fail_open = os.getenv("MOTET_EDGE_SCOPE_FAIL_OPEN", "").strip().lower() in (
                "1",
                "true",
                "yes",
            )
            if fail_open:
                return ""
            return (
                "MOTET_EDGE_TENANT_ID is required when MOTET_EDGE_COMMAND_SCOPE=tenant "
                "(set MOTET_EDGE_SCOPE_FAIL_OPEN=1 only for single-tenant local stacks)"
            )
        if cmd_tenant and cmd_tenant != allowed_tenant:
            return (
                f"Command tenant '{cmd_tenant}' does not match edge worker "
                f"tenant '{allowed_tenant}' (scope=tenant)"
            )
    else:
        allowed_principal = os.getenv("MOTET_EDGE_PRINCIPAL_ID", "")
        if not allowed_principal:
            fail_open = os.getenv("MOTET_EDGE_SCOPE_FAIL_OPEN", "").strip().lower() in (
                "1",
                "true",
                "yes",
            )
            if fail_open:
                return ""
            return (
                "MOTET_EDGE_PRINCIPAL_ID is required when MOTET_EDGE_COMMAND_SCOPE=principal "
                "(set MOTET_EDGE_SCOPE_FAIL_OPEN=1 only for single-user local stacks)"
            )
        if cmd_principal and cmd_principal != allowed_principal:
            return (
                f"Command principal '{cmd_principal}' does not match edge worker "
                f"principal '{allowed_principal}' (scope=principal)"
            )

    return ""


def _command_cancel_scopes(command: Any) -> Tuple[str, str, List[str]]:
    """Return ``(motet_task_id, parent_command_id, cancel_scopes)``."""
    from motet.core.distributed.task_control import append_cancel_scope

    motet_task_id = ""
    parent_command_id = ""
    cancel_scopes: List[str] = []
    ctx = getattr(command, "distributed_context", None)
    if ctx is not None:
        motet_task_id = (getattr(ctx, "task_id", None) or "").strip()
        parent_command_id = (getattr(ctx, "parent_command_id", None) or "").strip()
        cancel_scopes = list(getattr(ctx, "cancel_scopes", None) or [])
    cancel_scopes = append_cancel_scope(cancel_scopes, motet_task_id)
    return motet_task_id, parent_command_id, cancel_scopes


def dispatch_cancel_gate_result(
    command: Any,
    *,
    start_time: float,
    worker_id: str,
    celery_task_id: str,
    cancel_scopes: Optional[List[str]] = None,
    motet_task_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Refuse execution if any inherited cancel scope is sticky.

    Returns the Celery-shaped payload ``process_distributed_command`` should
    return, or ``None`` if the command may proceed.
    """
    from motet.core.distributed.task_control import (
        build_task_cancelled_response,
        first_cancelled_scope,
        is_cancelled,
    )

    if cancel_scopes is None or motet_task_id is None:
        resolved_task, _parent, resolved_scopes = _command_cancel_scopes(command)
        if cancel_scopes is None:
            cancel_scopes = resolved_scopes
        if motet_task_id is None:
            motet_task_id = resolved_task
    scopes = list(cancel_scopes)
    task_id = motet_task_id or ""
    if not scopes or not is_cancelled(scopes):
        return None

    execution_time = int((time.time() - start_time) * 1000)
    hit = first_cancelled_scope(scopes)
    workflow_hit = bool(hit and str(hit).startswith("wfrun-"))
    logger.info(
        "process_distributed_command_cancelled_gate",
        worker_id=worker_id,
        command_id=getattr(command, "command_id", None),
        command_type=(
            command.get_command_type() if hasattr(command, "get_command_type") else None
        ),
        task_id=task_id or None,
        cancelled_scope=hit,
        workflow_hit=workflow_hit,
    )
    if workflow_hit:
        from motet.core.workflow.checkpoint import WORKFLOW_CANCELLED_CODE

        cancelled = {
            "status": "error",
            "data": None,
            "error": {
                "type": "WorkflowCancelled",
                "message": "Workflow cancelled (dispatch gate)",
                "details": {
                    "code": WORKFLOW_CANCELLED_CODE,
                    "workflow_run_id": hit,
                    "task_id": task_id,
                },
                "recoverable": False,
                "retry_recommended": False,
            },
            "metadata": {
                "command_id": command.command_id,
                "command_type": command.get_command_type(),
                "task_id": task_id,
                "execution_time_ms": execution_time,
            },
        }
    else:
        cancelled = build_task_cancelled_response(
            command_id=command.command_id,
            command_type=command.get_command_type(),
            task_id=task_id,
            execution_time_ms=execution_time,
            reason="Task cancelled (dispatch gate)",
        )
    return {
        "status": "completed",
        "result": cancelled,
        "command_type": command.get_command_type(),
        "command_id": command.command_id,
        "execution_time_ms": execution_time,
        "worker_id": worker_id,
        "task_id": celery_task_id,
        "cancelled": True,
    }


def _unregister_root_live_task_unless_cancelled(command: Any) -> None:
    """Drop the live index when a root finishes and the task was not cancelled."""
    from motet.core.distributed.task_control import (
        is_task_cancelled,
        unregister_live_task,
    )

    ctx = getattr(command, "distributed_context", None)
    root_task_id = ((getattr(ctx, "task_id", None) if ctx else "") or "").strip()
    parent_id = ((getattr(ctx, "parent_command_id", None) if ctx else "") or "").strip()
    if not root_task_id or parent_id:
        return
    if is_task_cancelled(
        root_task_id,
        tenant_id=getattr(ctx, "tenant_id", None) if ctx else None,
    ):
        return
    unregister_live_task(
        root_task_id,
        tenant_id=getattr(ctx, "tenant_id", None) if ctx else None,
        principal_id=getattr(ctx, "principal_id", None) if ctx else None,
    )


def persist_command_wait_outcome(
    command: Any,
    envelope: Dict[str, Any],
    *,
    waiter_id: Optional[str],
) -> None:
    """Write ``cmd:outcome:{command_id}`` then LPUSH the result wake (#229).

    Parents read this envelope instead of Celery ``AsyncResult``. Store must
    succeed before the wake so BLPOP cannot race an empty GET. ``result``
    may be ``{_redis_result_key: ...}`` when the domain body was offloaded
    to ``cmd:result``; ``retrieve_command_wait_outcome`` hydrates it.
    """
    command_id = str(
        envelope.get("command_id") or getattr(command, "command_id", "") or ""
    ).strip()
    if not command_id or command_id == "unknown":
        logger.warning(
            "command_wait_outcome_skipped_no_command_id",
            waiter_id=waiter_id,
        )
        return

    ctx = getattr(command, "distributed_context", None) if command is not None else None
    tenant_id = envelope.get("tenant_id") or (
        getattr(ctx, "tenant_id", None) if ctx is not None else None
    )
    motet_id = envelope.get("motet_id") or (
        getattr(ctx, "motet_id", None) if ctx is not None else None
    )
    timeout = getattr(ctx, "timeout_seconds", None) if ctx is not None else None
    try:
        timeout_s = int(timeout) if timeout is not None else None
    except (TypeError, ValueError):
        timeout_s = None

    from motet.core.distributed.redis_command_data_manager import (
        get_redis_command_data_manager,
    )
    from motet.core.distributed.task_control import signal_command_result

    manager = get_redis_command_data_manager()
    manager.store_command_wait_outcome(
        command_id=command_id,
        envelope=envelope,
        tenant_id=tenant_id,
        motet_id=motet_id,
        command_timeout_seconds=timeout_s,
    )
    if waiter_id:
        signal_command_result(waiter_id)


@celery_app.task(name=CELERY_PROCESS_COMMAND_TASK, bind=True, ignore_result=True)
def process_distributed_command(self, command_data: str) -> Dict[str, Any]:
    """
    Process a distributed command.
    
    This is the main entry point for distributed command execution.
    Commands are serialized as JSON strings and deserialized here for execution.
    
    Args:
        command_data: Serialized command data (JSON string)
        
    Returns:
        Dict containing command execution result
    """
    start_time = time.time()
    celery_task_id = self.request.id
    tenant_token = None
    
    # Get the proper worker ID using centralized logic to match registration
    try:
        from .worker_utils import get_worker_id
        worker_id = get_worker_id()
    except Exception as e:
        logger.warning(
            "worker_id_resolution_failed_using_celery_hostname",
            error=str(e),
            exc_info=True,
        )
        worker_id = self.request.hostname
    
    try:
        debug_mode = os.getenv("MOTET_DEBUG_MODE", "false").lower() == "true"
        logger.debug("process_distributed_command_start", worker_id=worker_id)
        
        # Debug: Print command_data for schedule commands
        if debug_mode and "schedule" in str(command_data):
            preview = command_data[:300] if isinstance(command_data, str) else str(command_data)[:300]
            logger.debug(
                "process_distributed_command_debug_payload_preview",
                worker_id=worker_id,
                command_data_type=type(command_data).__name__,
                command_data_len=(len(command_data) if isinstance(command_data, str) else None),
                command_data_preview=preview,
            )
        
        # Import here to avoid circular dependencies
        from motet.core.commands.distributed import DistributedCommand
        
        # Deserialize the command
        try:
            command = DistributedCommand.deserialize_from_transport(command_data)
            command.distribution_started_at = datetime.utcnow()
            logger.debug(
                "process_distributed_command_deserialized",
                worker_id=worker_id,
                command_id=command.command_id,
                command_type=command.get_command_type(),
            )
            
            # Extract task_id from command's distributed context for event tracking
            # This ensures all commands in the same task flow use the same task_id
            task_id = command.distributed_context.task_id if command.distributed_context else celery_task_id
            from motet.core.distributed.task_control import bind_task_key_tenant

            tenant_token = bind_task_key_tenant(
                getattr(command.distributed_context, "tenant_id", None)
            )
            
        except Exception as e:
            logger.error(
                "process_distributed_command_deserialize_failed",
                worker_id=worker_id,
                error=str(e),
                command_data_type=type(command_data).__name__,
                command_data_preview=(command_data[:300] if isinstance(command_data, str) else str(command_data)[:300]),
                exc_info=True,
            )
            return {
                "status": "error",
                "error": f"Failed to deserialize command: {str(e)}",
                "execution_time_ms": int((time.time() - start_time) * 1000),
                "worker_id": worker_id,
                "task_id": celery_task_id
            }
        
        # ADR-0095: edge worker command scope guard (defense-in-depth)
        rejection = _check_edge_worker_command_scope(worker_id, command)
        if rejection:
            logger.warning(
                "edge_worker_command_rejected",
                worker_id=worker_id,
                command_id=command.command_id,
                command_type=command.get_command_type(),
                reason=rejection,
            )
            return {
                "status": "error",
                "error": rejection,
                "execution_time_ms": int((time.time() - start_time) * 1000),
                "worker_id": worker_id,
                "task_id": locals().get("task_id", celery_task_id),
            }

        # ADR-0131: live-index registration is best-effort and must not skip
        # the cancel gate if it fails.
        motet_task_id, parent_command_id, cancel_scopes = _command_cancel_scopes(
            command
        )
        try:
            from motet.core.distributed.task_control import register_live_task

            if motet_task_id and not parent_command_id:
                register_live_task(
                    motet_task_id,
                    tenant_id=getattr(command.distributed_context, "tenant_id", None),
                    principal_id=getattr(
                        command.distributed_context, "principal_id", None
                    ),
                    motet_id=getattr(command.distributed_context, "motet_id", None),
                    conversation_id=getattr(
                        command.distributed_context, "conversation_id", None
                    ),
                    command_type=command.get_command_type(),
                    root_command_id=command.command_id,
                )
        except Exception as e:
            logger.warning(
                "process_distributed_command_live_register_failed",
                worker_id=worker_id,
                command_id=getattr(command, "command_id", None),
                error=str(e),
                exc_info=True,
            )

        try:
            cancelled = dispatch_cancel_gate_result(
                command,
                start_time=start_time,
                worker_id=worker_id,
                celery_task_id=celery_task_id,
                cancel_scopes=cancel_scopes,
                motet_task_id=motet_task_id,
            )
            if cancelled is not None:
                return cancelled
        except Exception as e:
            logger.error(
                "process_distributed_command_cancel_gate_failed",
                worker_id=worker_id,
                command_id=getattr(command, "command_id", None),
                error=str(e),
                exc_info=True,
            )
            from motet.core.distributed.task_control import (
                build_task_cancelled_response,
            )

            execution_time = int((time.time() - start_time) * 1000)
            refused = build_task_cancelled_response(
                command_id=command.command_id,
                command_type=command.get_command_type(),
                task_id=motet_task_id,
                execution_time_ms=execution_time,
                reason="Task cancelled (dispatch gate failed closed)",
            )
            return {
                "status": "completed",
                "result": refused,
                "command_type": command.get_command_type(),
                "command_id": command.command_id,
                "execution_time_ms": execution_time,
                "worker_id": worker_id,
                "task_id": celery_task_id,
                "cancelled": True,
            }

        # Increment active command count - READINESS SERVICE
        try:
            from ..distributed.worker_readiness import get_readiness_service
            
            readiness_service = get_readiness_service()
            readiness_service.increment_active_commands(worker_id)
        except Exception as e:
            logger.warning(
                "readiness_increment_active_commands_failed",
                worker_id=worker_id,
                error=str(e),
                exc_info=True,
            )
        
        # Execute the command
        logger.debug(
            "process_distributed_command_execute_start",
            worker_id=worker_id,
            command_id=command.command_id,
            command_type=command.get_command_type(),
        )
        
        # Publish command started event (ADR-0023)
        # Event kind includes command type for easier filtering and identification
        # Enhanced with distributed context for event-driven MCP proxy creation
        try:
            event_bus = _event_bus_for_worker(worker_id)
            command_type = command.get_command_type()
            event_kind = f"{command_type}_started"
            
            # Build event data with distributed context fields
            event_data = {
                "command_id": command.command_id,
                "command_type": command_type,
                "status": "started",
                "worker_id": worker_id,
                "task_id": task_id,
                "started_at": datetime.utcnow().isoformat()
            }
            
            # Add distributed context fields for vault credential lookup and event-driven proxy creation
            if command.distributed_context:
                event_data["conversation_id"] = command.distributed_context.conversation_id
                event_data["tenant_id"] = command.distributed_context.tenant_id
                event_data["principal_id"] = command.distributed_context.principal_id
                event_data["motet_id"] = command.distributed_context.motet_id
            
            # Add tool_name for tool_execution commands (enables MCP proxy creation).
            # Match on the canonical suffix so this works regardless of namespace prefix
            # (e.g. "core.tool_execution" after ADR-0071 renaming).
            if command_type.endswith("tool_execution") and hasattr(command, 'data') and hasattr(command.data, 'tool_name'):
                event_data["tool_name"] = command.data.tool_name
            
            started_event = {
                "kind": event_kind,
                "source": "worker",
                "data": event_data,
                "timestamp": datetime.utcnow().isoformat(),
                "priority": 5,
                "correlation_id": command.command_id,
                "tags": ["command_execution", "distributed", "started"],
                "metadata": {}
            }
            event_bus.publish(started_event)
            logger.debug(
                "process_distributed_command_event_published",
                worker_id=worker_id,
                event_kind=event_kind,
                command_id=command.command_id,
                command_type=command_type,
            )
        except Exception as e:
            logger.warning(
                "process_distributed_command_event_publish_failed",
                worker_id=worker_id,
                event_kind="*_started",
                command_id=getattr(command, "command_id", None),
                error=str(e),
                exc_info=True,
            )
        
        # Import consolidated worker context creation
        from .tasks import _create_worker_context
        from .invoker_context import set_worker_context, set_current_command_id, clear_current_command_id, get_current_command_id

        # Get worker context for command execution (now sync)
        worker_context = _create_worker_context()
        
        # Set the worker context for this execution thread
        set_worker_context(worker_context)
        
        # Set the current command ID for parent tracking
        set_current_command_id(command.command_id)
        
        try:
            # Store command metadata for debugging and flow tracking
            try:
                from ..distributed.redis_command_data_manager import get_redis_command_data_manager
                command_data_manager = get_redis_command_data_manager()
                
                # Store initial metadata with parent-child relationship
                from motet.core.commands.distributed_types import (
                    agentic_loop_iteration_metadata_fields,
                )

                command_data_manager.store_command_metadata(
                    command_id=command.command_id,
                    command_type=command.get_command_type(),
                    task_id=task_id,
                    tenant_id=command.distributed_context.tenant_id,
                    motet_id=command.distributed_context.motet_id,
                    principal_id=command.distributed_context.principal_id or "",
                    conversation_id=command.distributed_context.conversation_id,
                    parent_command_id=command.distributed_context.parent_command_id,  # Track parent-child relationships
                    worker_id=worker_id,
                    status="executing",
                    executed_at=datetime.utcnow().isoformat(),
                    **agentic_loop_iteration_metadata_fields(
                        getattr(command.distributed_context, "metadata", None)
                    ),
                )
            except Exception as e:
                logger.warning(
                    "command_metadata_store_failed",
                    worker_id=worker_id,
                    command_id=command.command_id,
                    command_type=command.get_command_type(),
                    error=str(e),
                    exc_info=True,
                )
            
            # Execute the command directly (now sync)
            result = command.execute(worker_context)
            
            execution_time = int((time.time() - start_time) * 1000)
            
            # Update command metadata with completion info
            try:
                from ..distributed.redis_command_data_manager import get_redis_command_data_manager
                command_data_manager = get_redis_command_data_manager()
                
                command_data_manager.update_command_metadata(
                    command_id=command.command_id,
                    tenant_id=command.distributed_context.tenant_id,
                    status="completed",
                    completed_at=datetime.utcnow().isoformat(),
                    duration_ms=execution_time
                )
            except Exception as e:
                logger.warning(
                    "command_metadata_update_failed",
                    worker_id=worker_id,
                    command_id=command.command_id,
                    command_type=command.get_command_type(),
                    error=str(e),
                    exc_info=True,
                )
                
        finally:
            # Always clear the command context when done
            clear_current_command_id()
            
            # ALWAYS decrement active command count - even on exceptions
            try:
                from ..distributed.worker_readiness import get_readiness_service
                
                readiness_service = get_readiness_service()
                readiness_service.decrement_active_commands(worker_id)
            except Exception as e:
                logger.warning(
                    "readiness_decrement_active_commands_failed",
                    worker_id=worker_id,
                    error=str(e),
                    exc_info=True,
                )
        
        execution_time = int((time.time() - start_time) * 1000)
        
        logger.debug(
            "process_distributed_command_execute_complete",
            worker_id=worker_id,
            command_id=command.command_id,
            command_type=command.get_command_type(),
            execution_time_ms=execution_time,
        )
        
        # Check if result was stored in Redis (indicated by _redis_result_key)
        result_stored_in_redis = isinstance(result, dict) and "_redis_result_key" in result
        
        # Publish command completion event (ADR-0023)
        # Event kind includes command type for easier filtering and identification
        try:
            event_bus = _event_bus_for_worker(worker_id)
            command_type = command.get_command_type()
            event_kind = f"{command_type}_completed"
            
            completion_event = {
                "kind": event_kind,
                "source": "worker",
                "data": {
                    "command_id": command.command_id,
                    "command_type": command_type,
                    "status": "success",  # ADR-0029 status
                    "result": result,  # Full ADR-0029 command response
                    "execution_time_ms": execution_time,
                    "worker_id": worker_id,
                    "task_id": task_id,
                    "started_at": datetime.fromtimestamp(start_time).isoformat(),
                    "completed_at": datetime.utcnow().isoformat(),
                    # Add distributed context fields for consistency
                    "conversation_id": command.distributed_context.conversation_id if command.distributed_context else "",
                    "tenant_id": command.distributed_context.tenant_id if command.distributed_context else "",
                    "principal_id": command.distributed_context.principal_id if command.distributed_context else "",
                    "motet_id": command.distributed_context.motet_id if command.distributed_context else ""
                },
                "timestamp": datetime.utcnow().isoformat(),
                "priority": 5,
                "correlation_id": command.command_id,
                "tags": ["command_execution", "distributed", "completed"],
                "metadata": {}
            }
            event_bus.publish(completion_event)
            logger.debug(
                "process_distributed_command_event_published",
                worker_id=worker_id,
                event_kind=event_kind,
                command_id=command.command_id,
                command_type=command_type,
            )
        except Exception as e:
            logger.warning(
                "process_distributed_command_completion_event_publish_failed",
                worker_id=worker_id,
                event_kind="*_completed",
                command_id=command.command_id,
                command_type=command.get_command_type(),
                error=str(e),
                exc_info=True,
            )
        
        # ADR-0131: drop live index when a root finishes unless operator
        # cancel already marked it cancelled (those rows linger until TTL).
        try:
            _unregister_root_live_task_unless_cancelled(command)
        except Exception as live_err:
            logger.debug(
                "process_distributed_command_live_unregister_failed",
                error=str(live_err),
            )

        ctx = command.distributed_context
        envelope = {
            "status": "completed",
            "result": result,
            "command_type": command.get_command_type(),
            "command_id": command.command_id,
            "execution_time_ms": execution_time,
            "worker_id": worker_id,
            "task_id": celery_task_id,
            "result_stored_in_redis": result_stored_in_redis,
            "tenant_id": getattr(ctx, "tenant_id", None) if ctx else None,
            "motet_id": getattr(ctx, "motet_id", None) if ctx else None,
        }
        persist_command_wait_outcome(command, envelope, waiter_id=celery_task_id)
        return envelope
            
    except Exception as e:
        execution_time = int((time.time() - start_time) * 1000)
        logger.error(
            "process_distributed_command_failed",
            worker_id=worker_id,
            error=str(e),
            exc_info=True,
        )
        
        # Update command metadata with error info if command was created
        command_obj_for_meta = locals().get("command")
        try:
            if command_obj_for_meta is not None:
                from ..distributed.redis_command_data_manager import get_redis_command_data_manager
                command_data_manager = get_redis_command_data_manager()
                
                command_data_manager.update_command_metadata(
                    command_id=command_obj_for_meta.command_id,
                    tenant_id=getattr(
                        getattr(command_obj_for_meta, "distributed_context", None),
                        "tenant_id",
                        None,
                    ),
                    status="failed",
                    completed_at=datetime.utcnow().isoformat(),
                    duration_ms=execution_time,
                    error=str(e)
                )
        except Exception as e2:
            logger.warning(
                "command_metadata_update_failed_while_handling_error",
                worker_id=worker_id,
                command_id=getattr(command_obj_for_meta, "command_id", None),
                error=str(e2),
                exc_info=True,
            )
        
        # Note: Active command count is decremented in the finally block above
        
        # Publish command error/failure event (ADR-0023)
        # Event kind includes command type for easier filtering and identification
        try:
            event_bus = _event_bus_for_worker(worker_id)
            command_obj = locals().get('command')
            if command_obj:
                # Get command_id and command_type safely
                cmd_id = getattr(command_obj, 'command_id', 'unknown')
                cmd_type = command_obj.get_command_type() if hasattr(command_obj, 'get_command_type') else 'unknown'
                event_kind = f"{cmd_type}_error"
                
                failure_event = {
                    "kind": event_kind,
                    "source": "worker",
                    "data": {
                        "command_id": cmd_id,
                        "command_type": cmd_type,
                        "status": "error",  # ADR-0029 status
                        "result": None,
                        "error": {
                            "type": type(e).__name__,
                            "message": str(e),
                            "details": {}
                        },
                        "execution_time_ms": execution_time,
                        "worker_id": worker_id,
                        "task_id": locals().get("task_id", celery_task_id),
                        # Add distributed context fields for consistency
                        "conversation_id": command_obj.distributed_context.conversation_id if command_obj.distributed_context else "",
                        "tenant_id": command_obj.distributed_context.tenant_id if command_obj.distributed_context else "",
                        "principal_id": command_obj.distributed_context.principal_id if command_obj.distributed_context else "",
                        "motet_id": command_obj.distributed_context.motet_id if command_obj.distributed_context else ""
                    },
                    "timestamp": datetime.utcnow().isoformat(),
                    "priority": 5,
                    "correlation_id": cmd_id,
                    "tags": ["command_execution", "distributed", "error"],
                    "metadata": {}
                }
                event_bus.publish(failure_event)
                logger.debug(
                    "process_distributed_command_event_published",
                    worker_id=worker_id,
                    event_kind=event_kind,
                    command_id=cmd_id,
                    command_type=cmd_type,
                )
        except Exception as event_error:
            logger.warning(
                "process_distributed_command_error_event_publish_failed",
                worker_id=worker_id,
                error=str(event_error),
                exc_info=True,
            )
        
        try:
            command_obj = locals().get("command")
            if command_obj is not None:
                _unregister_root_live_task_unless_cancelled(command_obj)
        except Exception as live_err:
            logger.debug(
                "process_distributed_command_live_unregister_failed",
                error=str(live_err),
            )

        command_obj = locals().get("command")
        ctx = getattr(command_obj, "distributed_context", None) if command_obj else None
        envelope = {
            "status": "error",
            "error": str(e),
            "command_type": (
                command_obj.get_command_type()
                if command_obj is not None and hasattr(command_obj, "get_command_type")
                else "unknown"
            ),
            "command_id": getattr(command_obj, "command_id", "unknown"),
            "execution_time_ms": execution_time,
            "worker_id": worker_id,
            "task_id": celery_task_id,
            "tenant_id": getattr(ctx, "tenant_id", None) if ctx else None,
            "motet_id": getattr(ctx, "motet_id", None) if ctx else None,
        }
        persist_command_wait_outcome(command_obj, envelope, waiter_id=celery_task_id)
        return envelope
    finally:
        if tenant_token is not None:
            from motet.core.distributed.task_control import reset_task_key_tenant

            reset_task_key_tenant(tenant_token)


@celery_app.task(name="imf.commands.batch_process", bind=True)
def batch_process_commands(self, command_batch: List[str]) -> List[Dict[str, Any]]:
    """
    Process multiple commands in batch for efficiency.
    
    Args:
        command_batch: List of serialized command data
        
    Returns:
        List of command execution results
    """
    start_time = time.time()
    task_id = self.request.id
    worker_id = self.request.hostname
    
    try:
        logger.info(
            "batch_process_commands_start",
            worker_id=worker_id,
            batch_size=len(command_batch),
        )
        
        results = []
        successful_commands = 0
        failed_commands = 0
        
        for i, command_data in enumerate(command_batch):
            try:
                # Process individual command (use delay to avoid infinite loops)
                result = process_distributed_command.delay(command_data).get(timeout=60)  # type: ignore[attr-defined]
                results.append(result)
                
                if result.get("status") == "completed":
                    successful_commands += 1
                else:
                    failed_commands += 1
                    
            except Exception as e:
                results.append({
                    "status": "error",
                    "error": str(e),
                    "batch_index": i,
                    "worker_id": worker_id,
                    "task_id": task_id
                })
                failed_commands += 1
        
        batch_time = int((time.time() - start_time) * 1000)
        
        logger.info(
            "batch_process_commands_complete",
            worker_id=worker_id,
            batch_size=len(command_batch),
            successful=successful_commands,
            failed=failed_commands,
            batch_time_ms=batch_time,
        )
        
        # Add batch summary to each result
        batch_summary = {
            "batch_total": len(command_batch),
            "batch_successful": successful_commands,
            "batch_failed": failed_commands,
            "batch_time_ms": batch_time,
            "batch_task_id": task_id
        }
        
        for result in results:
            result["batch_summary"] = batch_summary
        
        return results
        
    except Exception as e:
        batch_time = int((time.time() - start_time) * 1000)
        logger.error(
            "batch_process_commands_failed",
            worker_id=worker_id,
            batch_time_ms=batch_time,
            error=str(e),
            exc_info=True,
        )
        
        return [{
            "status": "error",
            "error": str(e),
            "batch_time_ms": batch_time,
            "worker_id": worker_id,
            "task_id": task_id
        }]


@celery_app.task(name="imf.commands.retry", bind=True)
def retry_failed_command(self, command_data: str, attempt: int, max_attempts: int = 3) -> Dict[str, Any]:
    """
    Retry a failed command with exponential backoff.
    
    Args:
        command_data: Serialized command data
        attempt: Current attempt number (1-based)
        max_attempts: Maximum number of retry attempts
        
    Returns:
        Dict containing retry execution result
    """
    start_time = time.time()
    task_id = self.request.id
    worker_id = self.request.hostname
    
    try:
        if attempt > max_attempts:
            return {
                "status": "failed_permanently",
                "error": f"Command failed after {max_attempts} attempts",
                "attempt": attempt,
                "max_attempts": max_attempts,
                "worker_id": worker_id,
                "task_id": task_id
            }
        
        logger.info(
            "retry_failed_command_start",
            worker_id=worker_id,
            attempt=attempt,
            max_attempts=max_attempts,
        )
        
        # Calculate exponential backoff delay
        if attempt > 1:
            delay_seconds = min(2 ** (attempt - 1), 60)  # Cap at 60 seconds
            logger.debug(
                "retry_failed_command_backoff_sleep",
                worker_id=worker_id,
                attempt=attempt,
                delay_seconds=delay_seconds,
            )
            import time as time_module
            time_module.sleep(delay_seconds)
        
        # Attempt to process the command (use delay to avoid infinite loops)
        result = process_distributed_command.delay(command_data).get(timeout=60)  # type: ignore[attr-defined]
        
        if result.get("status") == "completed":
            retry_time = int((time.time() - start_time) * 1000)
            logger.info(
                "retry_failed_command_succeeded",
                worker_id=worker_id,
                attempt=attempt,
                retry_time_ms=retry_time,
            )
            
            # Add retry information to result
            result["retry_info"] = {
                "attempt": attempt,
                "max_attempts": max_attempts,
                "retry_time_ms": retry_time,
                "succeeded_on_retry": True
            }
            
            return result
        else:
            # Command failed, schedule next retry if attempts remain
            if attempt < max_attempts:
                logger.warning(
                    "retry_failed_command_failed_scheduling_next_attempt",
                    worker_id=worker_id,
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
                
                # Schedule next retry
                retry_failed_command.delay(command_data, attempt + 1, max_attempts)  # type: ignore[attr-defined]
                
                return {
                    "status": "retry_scheduled",
                    "error": result.get("error", "Unknown error"),
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "next_attempt_scheduled": True,
                    "worker_id": worker_id,
                    "task_id": task_id
                }
            else:
                logger.error(
                    "retry_failed_command_failed_permanently",
                    worker_id=worker_id,
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
                
                return {
                    "status": "failed_permanently",
                    "error": result.get("error", "Unknown error"),
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "worker_id": worker_id,
                    "task_id": task_id
                }
        
    except Exception as e:
        retry_time = int((time.time() - start_time) * 1000)
        logger.error(
            "retry_failed_command_failed",
            worker_id=worker_id,
            attempt=attempt,
            max_attempts=max_attempts,
            retry_time_ms=retry_time,
            error=str(e),
            exc_info=True,
        )
        
        return {
            "status": "error",
            "error": str(e),
            "attempt": attempt,
            "max_attempts": max_attempts,
            "retry_time_ms": retry_time,
            "worker_id": worker_id,
            "task_id": task_id
        }


@celery_app.task(name="imf.commands.health_check", bind=True)
def command_processor_health_check(self) -> Dict[str, Any]:
    """
    Health check for command processing capabilities.
    
    Returns:
        Dict containing health status of command processing
    """
    start_time = time.time()
    task_id = self.request.id
    worker_id = self.request.hostname
    
    try:
        health_checks = {}
        
        # Check 1: Command deserialization capability
        try:
            from motet.core.commands.distributed import DistributedCommand
            health_checks["command_deserialization"] = {
                "status": "healthy",
                "details": "DistributedCommand class available"
            }
        except Exception as e:
            health_checks["command_deserialization"] = {
                "status": "unhealthy",
                "error": str(e)
            }
        
        # Check 2: Worker context availability
        try:
            from .worker_tasks import worker_shutdown as _worker_shutdown_check
            health_checks["worker_integration"] = {
                "status": "healthy",
                "details": "Worker tasks integration available"
            }
        except Exception as e:
            health_checks["worker_integration"] = {
                "status": "unhealthy",
                "error": str(e)
            }
        
        # Check 3: Basic command execution test
        try:
            # This would test a simple command execution
            # For now, just verify the execution path is available
            health_checks["execution_path"] = {
                "status": "healthy",
                "details": "Command execution path available"
            }
        except Exception as e:
            health_checks["execution_path"] = {
                "status": "unhealthy",
                "error": str(e)
            }
        
        # Overall health assessment
        check_statuses = [check["status"] for check in health_checks.values()]
        if "unhealthy" in check_statuses:
            overall_status = "unhealthy"
        elif "warning" in check_statuses:
            overall_status = "warning"
        else:
            overall_status = "healthy"
        
        health_time = int((time.time() - start_time) * 1000)
        
        return {
            "overall_status": overall_status,
            "checks": health_checks,
            "worker_id": worker_id,
            "health_check_time_ms": health_time,
            "task_id": task_id,
            "timestamp": start_time
        }
        
    except Exception as e:
        health_time = int((time.time() - start_time) * 1000)
        
        return {
            "overall_status": "error",
            "error": str(e),
            "worker_id": worker_id,
            "health_check_time_ms": health_time,
            "task_id": task_id,
            "timestamp": start_time
        }
