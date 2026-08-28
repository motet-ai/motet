"""
Motet - Distributed Workflow Commands

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Distributed workflow execution command system for the Motet distributed framework.
    Provides unified distributed commands for executing complex workflows with dependency
    management, parallel execution, and step coordination. Uses the decorated function
    pattern with WorkflowExecutor for workflow execution.

    Issue #149: ``workflow_execution`` may return a suspended envelope when the
    graph pauses for handback/elicitation/confirmation/oauth; ``resume_workflow``
    claims the run (resume_epoch) then continues from the WorkflowCheckpoint.
    ``workflow_runs_list`` enumerates paused runs for operators / HTTP.
    ``workflow_run_control`` requests operator pause/cancel (cooperative for
    running runs; immediate cancel when already paused).

Dependencies:
    - uuid: Unique identifier generation
    - typing: Type hints and annotations
    - Distributed command system with decorator pattern
    - Workflow orchestration
    - MotetContext for resource access

Usage:
    # NEW: Decorated function pattern (recommended - ADR-0030)
    from motet.core.commands.builtin.workflow import (
        workflow_execution, WorkflowExecutionData,
        resume_workflow, ResumeWorkflowData,
        full_workflow_execution, FullWorkflowData
    )
    
    # Simple workflow with tool steps (smart defaults):
    workflow_data = WorkflowExecutionData(
        workflow_id="workflow_123",
        workflow_name="My Workflow",
        workflow_steps=[{"step_id": "step1", "tool_name": "web_search", "dependencies": []}]
    )
    result = motet.do(workflow_execution, data=workflow_data)
    
    # Full workflow with orchestrator integration (smart defaults):
    full_data = FullWorkflowData(workflow=workflow_obj)
    result = motet.do(full_workflow_execution, data=full_data)
    
    # Outside decorated command (explicit params):
    command = workflow_execution(
        task_id=task_id,
        conversation_id=conversation_id,
        data=workflow_data
    )
    result = global_invoker.execute_command(command)
    
    # DEPRECATED: Legacy class-based pattern (being phased out - ADR-0031)
    # Use decorated functions instead - see migration guide in class docstrings

Notes:
    - Supports complex workflow execution with dependency management
    - Includes parallel execution optimization and step coordination
    - Uses WorkflowExecutor for workflow execution
    - Includes comprehensive workflow monitoring and progress tracking
    - Integrates with distributed worker routing and capability management
    - Smart context-aware defaults for command composition
    - Automatic context propagation through motet.do pattern
    - Refactored to use concise command composition helpers (motet.do)
"""


import structlog
from typing import Any, Dict, List, Optional
from uuid import uuid4

from motet import motet
from motet.core.commands.capabilities import WorkerCapability
from motet.core.commands.decorator import get_motet_context
from motet.core.commands.command_data_classes import (
    ResumeWorkflowData,
    WorkflowExecutionData,
    WorkflowListData,
    WorkflowRunControlData,
    WorkflowRunsListData,  # re-exported for API / callers
)
from motet.core.workflow import Workflow, WorkflowStatus
from motet.core.registry import namespace_from_qualified_name
from motet.core.workers.observers import EventPriority


# Config classes have been replaced by CommandData classes in command_data_classes.py

logger = structlog.get_logger(__name__)


# ============================================================================
# DECORATED COMMANDS (New Pattern - ADR-0030)
# ============================================================================

# Workflow capability inference removed (ADR-0049)
# workflow_execution only needs TASK_SCHEDULING - steps route individually


@motet.command(
    description="Start and run a workflow by id with inputs: multi-step command graph execution with parallelism and templating.",
    # Parent workflow can run for a long time (e.g. app-builder implement_cycle
    # with foreach agent_turn chunks). Celery's global task_time_limit is overridden
    # per send_task from this value (see WorkerCommunicator).
    timeout_seconds=7200
    # No required_capabilities - can run on ANY worker (ADR-0049)
    # Steps route individually via motet.do() based on their capabilities
)
def workflow_execution(data: WorkflowExecutionData) -> Dict[str, Any]:
    """
    Execute a workflow through the distributed system (ADR-0049: Unified Workflow Architecture).
    
    Supports:
    - Tool sequences (previous ToolWorkflow)
    - Model/memory/reasoning operations (previous Workflow)
    - Mixed workflows (any combination of commands)
    - Sub-workflow composition (workflows calling workflows)
    
    Automatically handles:
    - Context propagation (task_id, conversation_id, etc. from parent motet)
    - Dependency graph building and execution ordering
    - Dynamic worker capability routing (infers from workflow steps)
    - Error handling and retries
    
    Dynamic Capabilities:
    - Inspects each step's command metadata
    - Aggregates required capabilities
    - Routes to workers with all necessary capabilities
    
    Args:
        data: WorkflowExecutionData with workflow definition
        motet: MotetContext for resource access (injected by decorator)
    
    Returns:
        Dict with workflow execution results
    """
    motet = get_motet_context()

    import time
    start_time = time.time()
    stream_key = getattr(motet, "stream_key", None)
    
    try:
        # Convert WorkflowExecutionData to Workflow object using helper method
        from motet.core.workflow import Workflow, WorkflowExecutor, WorkflowRegistry
        
        # Get original workflow definition from registry to preserve input_parameters
        # This also allows us to get step count for logging if workflow_steps is None
        original_workflow = WorkflowRegistry.get(data.workflow_id)
        from motet.core.workflow.user_catalog import (
            assert_user_workflow_invokable,
            is_user_workflow_id,
        )

        if is_user_workflow_id(str(data.workflow_id or "")):
            original_workflow = assert_user_workflow_invokable(
                data.workflow_id,
                getattr(motet, "tenant_id", None),
            )

        # Determine step count for logging (use provided steps or lookup from registry)
        step_count = len(data.workflow_steps) if data.workflow_steps else (
            len(original_workflow.steps) if original_workflow else 0
        )
        
        logger.info(
            f"🚀 Starting workflow execution: {data.workflow_name or (original_workflow.name if original_workflow else 'Unknown')} ({data.workflow_id}) "
            f"with {step_count} steps"
        )
        
        try:
            motet.stream_event(
                "tool_use",
                kind="workflow",
                tool_name=data.workflow_id,
                tool_call_id=None,
                status="started",
                stream_key=stream_key,
            )
        except Exception:
            pass  # stream event best-effort; must not crash execution

        # Use Workflow.from_execution_data to convert, preserving metadata from original
        workflow = Workflow.from_execution_data(data, original_workflow=original_workflow)
        
        # Execute via WorkflowExecutor
        executor = WorkflowExecutor()
        result = executor.execute_workflow(workflow, motet)

        if isinstance(result, dict) and result.get("suspended"):
            logger.info(
                "workflow_execution_suspended",
                workflow_id=data.workflow_id,
                workflow_run_id=result.get("workflow_run_id"),
                suspend_reason=result.get("suspend_reason"),
            )
            try:
                motet.stream_event(
                    "tool_use",
                    kind="workflow",
                    tool_name=data.workflow_id,
                    tool_call_id=None,
                    status="suspended",
                    stream_key=stream_key,
                    workflow_run_id=result.get("workflow_run_id"),
                    suspend_reason=result.get("suspend_reason"),
                )
            except Exception:
                pass
            return result

        logger.info(
            f"✅ Workflow execution completed: {data.workflow_id} "
            f"in {result.get('execution_time_ms')}ms"
        )
        
        # Return in ADR-0029 format (decorator will wrap it)
        try:
            motet.stream_event(
                "tool_use",
                kind="workflow",
                tool_name=data.workflow_id,
                tool_call_id=None,
                status="success",
                stream_key=stream_key,
            )
        except Exception:
            pass  # stream event best-effort; must not crash execution

        return result
            
    except Exception as e:
        logger.error(f"❌ Workflow execution failed: {e}")
        try:
            motet.stream_event(
                "tool_use",
                kind="workflow",
                tool_name=data.workflow_id,
                tool_call_id=None,
                status="error",
                stream_key=stream_key,
            )
        except Exception:
            pass  # stream event best-effort; must not crash error path
        raise


@motet.command(
    description="Resume a paused workflow from its checkpoint after elicitation, approval, or operator continue.",
    # Resume may run remaining Motet steps for a long time.
    timeout_seconds=7200,
)
def resume_workflow(data: ResumeWorkflowData) -> Dict[str, Any]:
    """
    Resume a paused workflow from its WorkflowCheckpoint (issue #149).

    Claims the run under a distributed lock (paused → running, epoch++) so a
    replayed payload cannot re-execute Motet side effects. Tagged payload union
    mirrors resume_turn: kind selects validation. Principal must match; forged
    interaction ids are rejected. Malformed payloads release the run back to
    paused (epoch stays bumped so the bad payload cannot retry).
    """
    motet = get_motet_context()
    from motet.core.checkpoints.redis_store import (
        assert_checkpoint_principal,
        bind_resume_conversation,
    )
    from motet.core.workflow.checkpoint import (
        WorkflowResumeConflict,
        claim_workflow_run_for_resume,
        find_workflow_run_id_by_interaction,
        load_workflow_checkpoint,
        release_workflow_run_to_paused,
    )
    from motet.core.workflow.executor import WorkflowExecutor

    tenant_id = getattr(motet, "tenant_id", None)
    motet_id = getattr(motet, "motet_id", None) or "default"

    workflow_run_id = (data.workflow_run_id or "").strip()
    if not workflow_run_id and data.interaction_id:
        workflow_run_id = (
            find_workflow_run_id_by_interaction(
                tenant_id=tenant_id,
                motet_id=motet_id,
                interaction_id=str(data.interaction_id).strip(),
            )
            or ""
        )
    if not workflow_run_id:
        raise ValueError(
            "resume_workflow: supply workflow_run_id or a recorded interaction_id "
            "(checkpoints expire after their TTL)"
        )

    # Peek for principal check before claim (claim loads again under lock).
    peek = load_workflow_checkpoint(
        tenant_id=tenant_id,
        motet_id=motet_id,
        workflow_run_id=workflow_run_id,
    )
    if peek is None:
        raise ValueError(
            f"resume_workflow: checkpoint '{workflow_run_id}' not found or expired"
        )
    assert_checkpoint_principal(
        peek.principal_id,
        getattr(motet, "principal_id", None),
        resource_label="resume_workflow",
        resource_id=workflow_run_id,
    )
    bind_resume_conversation(
        motet,
        peek.conversation_id,
        log_context={"workflow_run_id": workflow_run_id},
    )

    expected_epoch = data.resume_epoch
    try:
        checkpoint = claim_workflow_run_for_resume(
            tenant_id=tenant_id,
            motet_id=motet_id,
            workflow_run_id=workflow_run_id,
            expected_epoch=expected_epoch,
        )
    except WorkflowResumeConflict:
        raise
    if checkpoint is None:
        # In-process hand-built checkpoint path is unused for the command; treat
        # as missing if claim returned None after a successful peek race.
        raise ValueError(
            f"resume_workflow: checkpoint '{workflow_run_id}' not found or expired"
        )

    logger.info(
        "resume_workflow_resuming",
        workflow_run_id=workflow_run_id,
        workflow_id=checkpoint.workflow_id,
        kind=data.kind,
        resume_epoch=checkpoint.resume_epoch,
    )
    executor = WorkflowExecutor()
    try:
        result = executor.resume_from_checkpoint(
            checkpoint,
            motet,
            kind=(data.kind or "handback_tools").strip(),
            observations=list(data.observations or []),
            answers=data.answers,
            decision=data.decision,
            edited_parameters=data.edited_parameters,
            auth_status=data.auth_status,
        )
    except ValueError:
        # Malformed payload: interactions still outstanding — release to paused.
        release_workflow_run_to_paused(checkpoint)
        raise
    if isinstance(result, dict):
        result["resumed_from_workflow_run_id"] = workflow_run_id
        result["resume_epoch"] = checkpoint.resume_epoch
    return result


@motet.command(
    description="List checkpointed workflow runs for the current tenant (paused runs by default) for ops and resume.",
    timeout_seconds=30,
)
def workflow_runs_list(data: WorkflowRunsListData) -> Dict[str, Any]:
    """List checkpointed workflow runs for the current tenant (paused by default)."""
    motet = get_motet_context()
    from motet.core.workflow.checkpoint import list_paused_workflow_runs

    status = (data.status or "paused").strip().lower()
    if status != "paused":
        raise ValueError(
            f"workflow_runs_list: status '{status}' is not supported; use 'paused'"
        )
    runs = list_paused_workflow_runs(
        tenant_id=getattr(motet, "tenant_id", None),
        motet_id=getattr(motet, "motet_id", None) or "default",
        limit=int(data.limit or 50),
        offset=int(data.offset or 0),
    )
    return {"runs": runs, "count": len(runs), "status": status}


@motet.command(
    description="Pause or cancel a checkpointed workflow run (operator control).",
    timeout_seconds=30,
)
def workflow_run_control(data: WorkflowRunControlData) -> Dict[str, Any]:
    """Pause or cancel a checkpointed workflow run (operator control)."""
    motet = get_motet_context()
    from motet.core.checkpoints.redis_store import assert_checkpoint_principal
    from motet.core.workflow.checkpoint import (
        load_workflow_checkpoint,
        request_workflow_run_control,
    )

    workflow_run_id = (data.workflow_run_id or "").strip()
    if not workflow_run_id:
        raise ValueError("workflow_run_control: workflow_run_id is required")

    tenant_id = getattr(motet, "tenant_id", None)
    motet_id = getattr(motet, "motet_id", None) or "default"
    peek = load_workflow_checkpoint(
        tenant_id=tenant_id,
        motet_id=motet_id,
        workflow_run_id=workflow_run_id,
    )
    if peek is None:
        raise ValueError(
            f"workflow_run_control: run '{workflow_run_id}' not found or expired"
        )
    assert_checkpoint_principal(
        peek.principal_id,
        getattr(motet, "principal_id", None),
        resource_label="workflow_run_control",
        resource_id=workflow_run_id,
    )
    return request_workflow_run_control(
        tenant_id=tenant_id,
        motet_id=motet_id,
        workflow_run_id=workflow_run_id,
        action=data.action,
        principal_id=getattr(motet, "principal_id", None),
        reason=data.reason,
    )


@motet.command(
    description="List workflows visible in the current runtime (built-in and bundle), for discovery and selection.",
    timeout_seconds=30,
)
def workflow_list(data: WorkflowListData) -> Dict[str, Any]:
    """
    List workflows visible in the current runtime context.

    Returns workflows from local WorkflowRegistry plus bundle catalog fallbacks
    (bundle:*:catalog), then applies optional filters and pagination.
    """
    motet = get_motet_context()
    motet_id = (getattr(motet, "motet_id", "") or "").strip()
    tenant_id = (getattr(motet, "tenant_id", "") or "").strip()

    def _targeting_allows_context(targeting: Optional[Dict[str, Any]]) -> bool:
        if not targeting:
            return True
        motet_ids = targeting.get("motet_ids") or []
        tenant_ids = targeting.get("tenant_ids") or []
        if not motet_ids and not tenant_ids:
            return True
        motet_ok = not motet_ids or motet_id in motet_ids
        tenant_ok = not tenant_ids or tenant_id in tenant_ids
        return motet_ok and tenant_ok

    try:
        entries: List[Dict[str, Any]] = []
        known_ids = set()

        from motet.core.workflow.user_catalog import (
            is_user_workflow_id,
            list_visible_workflows,
        )

        for wf in list_visible_workflows(tenant_id):
            item: Dict[str, Any] = {
                "workflow_id": wf.workflow_id,
                "name": wf.name or wf.workflow_id,
                "description": wf.description or "",
                "step_count": len(wf.steps) if wf.steps else 0,
                "source": (
                    "user_catalog"
                    if is_user_workflow_id(wf.workflow_id)
                    else "registry"
                ),
                "bundle_id": namespace_from_qualified_name(wf.workflow_id),
                "input_parameters": wf.input_parameters,
                "required_inputs": wf.required_inputs,
                "use_for": wf.use_for,
                "output_field": wf.output_field,
                "presentation": wf.presentation,
            }
            if data.include_steps:
                item["steps"] = {
                    sid: {
                        "step_id": step.step_id,
                        "name": step.name,
                        "command_type": step.command_type,
                        "command_data": step.command_data if hasattr(step, "command_data") else {},
                        "dependencies": step.dependencies or [],
                        "execution_context": step.execution_context or {},
                    }
                    for sid, step in (wf.steps or {}).items()
                }
                item["execution_order"] = wf.execution_order or []
            entries.append(item)
            known_ids.add(wf.workflow_id)

        try:
            from motet.core.distributed.redis_manager import get_sync_redis_client
            from motet.core.bundles.deploy import _list_all_catalogs

            redis_client = get_sync_redis_client()
            catalogs = _list_all_catalogs(redis_client)
            for bundle_id, catalog in sorted(catalogs.items()):
                targeting = catalog.get("targeting") or {}
                if not _targeting_allows_context(targeting):
                    continue
                for workflow_id in catalog.get("workflows", []):
                    if workflow_id in known_ids:
                        continue
                    entries.append(
                        {
                            "workflow_id": workflow_id,
                            "name": workflow_id,
                            "description": f"Bundle workflow from '{bundle_id}' (catalog)",
                            "step_count": 0,
                            "source": "catalog",
                            "bundle_id": bundle_id,
                        }
                    )
                    known_ids.add(workflow_id)
        except Exception as catalog_err:
            logger.warning("workflow_list_catalog_fetch_failed", error=str(catalog_err))

        bundle_filter = (data.bundle_id or "").strip()
        if bundle_filter:
            if bundle_filter == "core":
                entries = [it for it in entries if it.get("bundle_id") == "core"]
            else:
                entries = [it for it in entries if it.get("workflow_id", "").startswith(f"{bundle_filter}.")]

        name_filter = (data.name_contains or "").strip().lower()
        if name_filter:
            entries = [
                it
                for it in entries
                if name_filter in str(it.get("workflow_id", "")).lower()
                or name_filter in str(it.get("description", "")).lower()
            ]

        entries.sort(key=lambda x: x.get("workflow_id", ""))
        total = len(entries)
        start = data.offset
        end = start + data.limit if data.limit else None
        paged = entries[start:end] if end is not None else entries[start:]

        return {
            "total_workflows": total,
            "workflows": paged,
            "limit": data.limit,
            "offset": data.offset,
        }
    except Exception as e:
        logger.error("workflow_list_failed", error=str(e), exc_info=True)
        raise


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    # ADR-0049: Unified Workflow Architecture
    "workflow_list",
    "WorkflowListData",
    "workflow_runs_list",
    "WorkflowRunsListData",
    "workflow_run_control",
    "WorkflowRunControlData",
    "workflow_execution",
    "WorkflowExecutionData",
    "resume_workflow",
    "ResumeWorkflowData",
]
