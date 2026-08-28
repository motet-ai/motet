"""
Motet - Workflows API

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Workflow management API for the Motet distributed framework.
    Provides REST API endpoints for validating and registering LLM/API-authored
    workflow definitions, executing and listing registered workflows,
    enumerating paused runs, resuming checkpoints (issue #149), and operator
    pause/cancel of running or paused runs.

Dependencies:
    - fastapi: Web framework for REST API
    - motet.core.workflow: Workflow execution and registry
    - motet.core.workflow.builder: Shared validate/register/unregister pipeline
    - motet.core.commands.builtin.workflow: distributed resume / list / control

Usage:
    from motet.interfaces.api.v1.workflows import router

    # Include in FastAPI app
    app.include_router(router)

Notes:
    - Registered workflow templates remain discoverable via GET /api/v1/workflows
    - POST /validate and /register share the core.workflow_builder pipeline
    - DELETE /{workflow_id} unregisters user.* definitions only
    - Paused runs are queryable via GET /api/v1/workflows/runs
    - Resume uses the same tagged payload as resume_workflow (kind union)
    - POST.../pause and.../cancel are cooperative for running runs
    - Part of Phase 2: API Organization and URL Standardization
"""

from typing import Dict, List, Optional, Any

import asyncio
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, Body, Query
from pydantic import BaseModel, Field

from ..shared.auth import get_current_principal
from ..shared.identity import get_principal_context
from ....core.types import Principal

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])


class WorkflowExecuteRequest(BaseModel):
    """Request model for executing a workflow."""
    workflow_id: str = Field(
        ...,
        description="Workflow ID to execute",
        json_schema_extra={"example": "wf-123456"}
    )
    workflow_name: str = Field(
        ...,
        description="Workflow name",
        json_schema_extra={"example": "Data Processing Workflow"}
    )
    steps: List[Dict[str, Any]] = Field(
        ...,
        description="List of workflow steps with their configurations",
        json_schema_extra={"example": [
            {
                "step_id": "step1",
                "name": "Extract Data",
                "module_name": "tools",
                "operation": "execute",
                "parameters": {"tool": "data_extractor"},
                "dependencies": [],
                "timeout_seconds": 60
            }
        ]}
    )
    context: Optional[Dict[str, Any]] = Field(
        None,
        description="Optional execution context data",
        json_schema_extra={"example": {"user_id": "user123", "session_id": "session456"}}
    )


class WorkflowResumeRequest(BaseModel):
    """Tagged resume payload mirroring ResumeWorkflowData."""

    kind: str = Field(
        default="handback_tools",
        description=(
            "Resume kind: handback_tools | elicitation | confirmation | oauth | operator."
        ),
        json_schema_extra={"example": "handback_tools"},
    )
    resume_epoch: Optional[int] = Field(
        default=None,
        description="Optional expected resume_epoch for idempotent clients.",
        json_schema_extra={"example": 0},
    )
    observations: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Handback observations: [{tool_call_id, content}, ...].",
    )
    answers: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Elicitation answers for kind=elicitation.",
    )
    decision: Optional[str] = Field(
        default=None,
        description="approve | reject for kind=confirmation.",
    )
    edited_parameters: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional edited tool parameters when confirming with edits.",
    )
    auth_status: Optional[str] = Field(
        default=None,
        description="completed | failed for kind=oauth.",
    )


class WorkflowRunControlRequest(BaseModel):
    """Optional body for operator pause/cancel."""

    reason: Optional[str] = Field(
        default=None,
        description="Optional operator reason for the pause or cancel.",
        json_schema_extra={"example": "user requested stop"},
    )


class WorkflowDefinitionRequest(BaseModel):
    """YAML or structured workflow document for validate/register."""

    yaml: Optional[str] = Field(
        default=None,
        description="Workflow document in bundle workflows/*.yaml shape",
        json_schema_extra={
            "example": (
                "workflow_id: competitor_brief\n"
                "name: Competitor brief\n"
                "required_inputs: [topic]\n"
                "steps:\n"
                "  search:\n"
                "    step_id: search\n"
                "    command_type: core.tool_execution\n"
                "    command_data:\n"
                "      tool_name: core.math_eval\n"
                "      parameters: {expression: '1+1'}\n"
                "    dependencies: []\n"
            )
        },
    )
    workflow: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Structured workflow object matching Workflow.from_dict",
    )
    workflow_id: Optional[str] = Field(
        default=None,
        description="Optional id override (rewritten to user.<owner>.<local>)",
        json_schema_extra={"example": "competitor_brief"},
    )
    replace: bool = Field(
        default=False,
        description="Allow overwrite of an existing user.* workflow on register",
        json_schema_extra={"example": False},
    )


def _builder_http_result(result: Any, *, success_status: int = 200) -> Any:
    """Map BuilderResult to a FastAPI response or HTTPException."""
    from fastapi.responses import JSONResponse

    payload = result.to_dict()
    if result.ok:
        if success_status == 200:
            return payload
        return JSONResponse(status_code=success_status, content=payload)

    codes = {e.code for e in result.errors}
    if "not_found" in codes:
        status = 404
    elif codes & {
        "unregister_denied",
        "replace_denied",
        "namespace_required",
        "ownership_denied",
    }:
        status = 403
    elif "already_exists" in codes:
        status = 409
    else:
        status = 400
    raise HTTPException(status_code=status, detail=payload)


def _scope_slug_for_principal(principal: Principal) -> str:
    from ....core.workflow.builder import sanitize_scope_slug

    tenant = (principal.tenant_id or "").strip()
    principal_id = (principal.id or "").strip()
    return sanitize_scope_slug(tenant or principal_id or "default")


@router.post(
    "/validate",
    summary="Validate a workflow definition",
    description=(
        "Parse YAML/JSON workflow document with the shared workflow builder "
        "pipeline (allowlist + user. namespace). Does not register or execute."
    ),
    response_description="Normalized workflow summary or structured errors",
    responses={
        200: {"description": "Definition is valid"},
        400: {"description": "Validation failed"},
        401: {"description": "Unauthorized"},
    },
)
async def validate_workflow_definition(
    req: WorkflowDefinitionRequest = Body(...),
    principal: Principal = Depends(get_current_principal),
):
    """Dry-run parse and allowlist checks for an authored workflow."""
    from ....core.workflow.builder import run_workflow_builder

    result = run_workflow_builder(
        mode="validate",
        yaml_text=req.yaml,
        workflow_dict=req.workflow,
        workflow_id=req.workflow_id,
        scope_slug=_scope_slug_for_principal(principal),
        motet=None,
        principal_id=principal.id,
        tenant_id=principal.tenant_id,
        persist=False,
        fan_out=False,
    )
    return _builder_http_result(result)


@router.post(
    "/register",
    summary="Register a workflow definition",
    description=(
        "Validate and register a workflow as a user.* definition callable as "
        "workflow_<id>. Persists to the Redis user-workflow catalog and fans "
        "out to live workers via core.sync_user_workflow."
    ),
    response_description="Registered workflow id and tool_name",
    responses={
        200: {"description": "Workflow registered"},
        400: {"description": "Validation failed"},
        401: {"description": "Unauthorized"},
        403: {"description": "Namespace / replace / ownership denied"},
        409: {"description": "Workflow id already exists"},
    },
)
async def register_workflow_definition(
    req: WorkflowDefinitionRequest = Body(...),
    principal: Principal = Depends(get_current_principal),
):
    """Register a new user.* workflow type from YAML or JSON."""
    from ....core.workflow.builder import run_workflow_builder

    result = run_workflow_builder(
        mode="register",
        yaml_text=req.yaml,
        workflow_dict=req.workflow,
        workflow_id=req.workflow_id,
        replace=bool(req.replace),
        scope_slug=_scope_slug_for_principal(principal),
        motet=None,
        principal_id=principal.id,
        tenant_id=principal.tenant_id,
        persist=True,
        fan_out=True,
    )
    return _builder_http_result(result)


@router.get(
    "/{workflow_id}/export",
    summary="Export a user.* workflow as bundle YAML",
    description="Promote-friendly YAML for copying into a bundle workflows/ directory.",
    responses={
        200: {"description": "Bundle-shaped YAML"},
        403: {"description": "Ownership denied"},
        404: {"description": "Workflow not found"},
    },
)
async def export_workflow_definition(
    workflow_id: str,
    principal: Principal = Depends(get_current_principal),
):
    """Export a registered user workflow as bundle-authoring YAML."""
    from ....core.workflow.builder import run_workflow_builder

    result = run_workflow_builder(
        mode="export",
        workflow_id=workflow_id,
        scope_slug=_scope_slug_for_principal(principal),
        motet=None,
        principal_id=principal.id,
        tenant_id=principal.tenant_id,
        persist=False,
        fan_out=False,
    )
    return _builder_http_result(result)


@router.delete(
    "/{workflow_id}",
    summary="Unregister a user.* workflow definition",
    description=(
        "Remove a previously registered user.* workflow from the Redis catalog "
        "and worker registries. Core/bundle ids and other principals' workflows "
        "are rejected."
    ),
    responses={
        200: {"description": "Workflow unregistered"},
        403: {"description": "Not a user.* workflow or ownership denied"},
        404: {"description": "Workflow not found"},
        401: {"description": "Unauthorized"},
    },
)
async def unregister_workflow_definition(
    workflow_id: str,
    principal: Principal = Depends(get_current_principal),
):
    """Unregister a user.* workflow definition (not a run cancel)."""
    from ....core.workflow.builder import run_workflow_builder

    result = run_workflow_builder(
        mode="unregister",
        workflow_id=workflow_id,
        scope_slug=_scope_slug_for_principal(principal),
        motet=None,
        principal_id=principal.id,
        tenant_id=principal.tenant_id,
        persist=True,
        fan_out=True,
    )
    return _builder_http_result(result)


@router.post(
    "/execute",
    summary="Execute a workflow",
    description="Execute a workflow using the distributed workflow system",
    response_description="Workflow execution result"
)
async def execute_workflow(
    req: WorkflowExecuteRequest = Body(...),
    principal: Principal = Depends(get_current_principal),
):
    """Execute a workflow using the distributed workflow system."""
    try:
        from ....core.workers import global_invoker
        from ....core.workflow import Workflow, WorkflowStep

        workflow = Workflow(
            workflow_id=req.workflow_id,
            name=req.workflow_name,
            description=f"Workflow created via API: {req.workflow_name}",
            context=req.context or {}
        )

        for step_data in req.steps:
            step = WorkflowStep(
                step_id=step_data.get("step_id", str(uuid.uuid4())),
                name=step_data.get("name", "API Step"),
                module_name=step_data.get("module_name", "tools"),
                operation=step_data.get("operation", "execute"),
                parameters=step_data.get("parameters", {}),
                dependencies=step_data.get("dependencies", []),
                timeout_seconds=step_data.get("timeout_seconds", 60),
                retry_attempts=step_data.get("retry_attempts", 0)
            )
            workflow.add_step(step)

        from motet.core.commands.builtin.workflow import workflow_execution, WorkflowExecutionData

        workflow_steps = []
        for step_id, step in workflow.steps.items():
            workflow_steps.append({
                "step_id": step_id,
                "name": step.name,
                "command_type": step.command_type,
                "command_data": step.command_data,
                "execution_context": step.execution_context,
                "dependencies": step.dependencies
            })

        workflow_data = WorkflowExecutionData(
            workflow_id=workflow.workflow_id,
            workflow_name=workflow.name,
            workflow_steps=workflow_steps,
            context=workflow.context,
            description=workflow.description
        )

        tenant_id = (principal.tenant_id or "default").strip() or "default"
        principal_id = (principal.id or "").strip()
        workflow_command = workflow_execution(
            task_id=str(uuid.uuid4()),
            conversation_id="",
            tenant_id=tenant_id,
            principal_id=principal_id,
            data=workflow_data
        )

        result = await asyncio.to_thread(global_invoker.execute_command, workflow_command)

        if result and result.get("status") == "completed":
            return {
                "status": "completed",
                "workflow_id": result.get("workflow_id"),
                "workflow_name": result.get("workflow_name"),
                "result": result.get("result", {}),
                "workflow_status": result.get("workflow_status", "completed")
            }
        else:
            return {
                "status": "failed",
                "error": result.get("error", "Unknown error"),
                "workflow_id": req.workflow_id
            }

    except Exception as e:
        logger.error("Failed to execute workflow", workflow_id=req.workflow_id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to execute workflow: {str(e)}")


@router.get(
    "",
    summary="List registered workflows",
    description="Get list of all registered workflows from distributed workflow_list command.",
    response_description="List of registered workflows"
)
async def get_active_workflows(
    principal: Principal = Depends(get_current_principal),
):
    """Get registered workflow templates (not paused runs — see /runs)."""
    try:
        from motet.core.commands.builtin.workflow import workflow_list, WorkflowListData
        from ....core.workers import global_invoker

        _motet_id, tenant_id, principal_id = get_principal_context(principal)

        command_data = WorkflowListData(include_steps=True)
        command = workflow_list(
            task_id=str(uuid.uuid4()),
            conversation_id="",
            data=command_data,
            tenant_id=tenant_id,
            principal_id=principal_id,
            timeout_seconds=30,
        )
        result = await asyncio.to_thread(global_invoker.execute_command, command)

        if result.get("status") == "error":
            raise HTTPException(
                status_code=500,
                detail=f"Failed to list workflows via distributed command: {result.get('error')}",
            )

        adr_response = result.get("result", {})
        response_data = adr_response.get("data", {})
        workflows = response_data.get("workflows", [])
        return {"registered_workflows": workflows}
    except Exception as e:
        logger.error("Failed to list workflows", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list workflows: {str(e)}")


@router.get(
    "/runs",
    summary="List paused workflow runs",
    description="Enumerate checkpointed workflow runs awaiting resume (issue #149).",
    response_description="Paused run summaries",
    responses={
        200: {"description": "Paused runs for the caller's tenant/motet"},
        401: {"description": "Unauthorized"},
    },
)
async def list_workflow_runs(
    status: str = Query(
        default="paused",
        description="Filter status (currently only 'paused' is supported)",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_current_principal),
):
    """List paused workflow runs for the authenticated principal's tenant."""
    try:
        from motet.core.commands.builtin.workflow import (
            workflow_runs_list,
            WorkflowRunsListData,
        )
        from ....core.workers import global_invoker

        motet_id, tenant_id, principal_id = get_principal_context(principal)
        command = workflow_runs_list(
            task_id=str(uuid.uuid4()),
            conversation_id="",
            data=WorkflowRunsListData(status=status, limit=limit, offset=offset),
            tenant_id=tenant_id,
            principal_id=principal_id,
            motet_id=motet_id,
            timeout_seconds=30,
        )
        result = await asyncio.to_thread(global_invoker.execute_command, command)
        if result.get("status") == "error":
            raise HTTPException(
                status_code=500,
                detail=f"Failed to list workflow runs: {result.get('error')}",
            )
        adr = result.get("result") or {}
        data = adr.get("data") if isinstance(adr, dict) else {}
        return data or {"runs": [], "count": 0, "status": status}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error("Failed to list workflow runs", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list workflow runs: {str(e)}")


@router.get(
    "/runs/{workflow_run_id}",
    summary="Get a workflow run checkpoint summary",
    description="Load a WorkflowCheckpoint summary by run id.",
    responses={
        200: {"description": "Run summary"},
        404: {"description": "Run not found or expired"},
    },
)
async def get_workflow_run(
    workflow_run_id: str,
    principal: Principal = Depends(get_current_principal),
):
    """Return an operator-facing summary for a checkpointed run."""
    from motet.core.workflow.checkpoint import load_workflow_checkpoint

    motet_id, tenant_id, principal_id = get_principal_context(principal)
    checkpoint = load_workflow_checkpoint(
        tenant_id=tenant_id,
        motet_id=motet_id or "default",
        workflow_run_id=workflow_run_id,
    )
    if checkpoint is None:
        raise HTTPException(status_code=404, detail=f"workflow run '{workflow_run_id}' not found")
    recorded = str(checkpoint.principal_id or "").strip()
    caller = str(principal_id or "").strip()
    if recorded and caller and recorded != caller:
        raise HTTPException(status_code=403, detail="workflow run belongs to a different principal")
    return checkpoint.summary()


@router.post(
    "/runs/{workflow_run_id}/resume",
    summary="Resume a paused workflow run",
    description="Resume via tagged payload (same contract as resume_workflow).",
    responses={
        200: {"description": "Resume result (completed or re-suspended)"},
        409: {"description": "Resume conflict (already running, terminal, or wrong epoch)"},
        404: {"description": "Run not found"},
    },
)
async def resume_workflow_run(
    workflow_run_id: str,
    req: WorkflowResumeRequest = Body(...),
    principal: Principal = Depends(get_current_principal),
):
    """Resume a paused workflow run through the distributed resume_workflow command."""
    try:
        from motet.core.commands.builtin.workflow import resume_workflow, ResumeWorkflowData
        from motet.core.workflow.checkpoint import WorkflowResumeConflict
        from ....core.workers import global_invoker

        motet_id, tenant_id, principal_id = get_principal_context(principal)
        command = resume_workflow(
            task_id=str(uuid.uuid4()),
            conversation_id="",
            data=ResumeWorkflowData(
                workflow_run_id=workflow_run_id,
                kind=req.kind,
                resume_epoch=req.resume_epoch,
                observations=list(req.observations or []),
                answers=req.answers,
                decision=req.decision,
                edited_parameters=req.edited_parameters,
                auth_status=req.auth_status,
            ),
            tenant_id=tenant_id,
            principal_id=principal_id,
            motet_id=motet_id,
            timeout_seconds=7200,
        )
        result = await asyncio.to_thread(global_invoker.execute_command, command)
        if result.get("status") == "error":
            err = result.get("error") or {}
            message = err.get("message") if isinstance(err, dict) else str(err)
            lower = (message or "").lower()
            if "not found" in lower or "expired" in lower:
                raise HTTPException(status_code=404, detail=message)
            if "different principal" in lower:
                raise HTTPException(status_code=403, detail=message)
            if "not awaiting resume" in lower or "resume epoch" in lower or "being resumed" in lower:
                raise HTTPException(status_code=409, detail=message)
            raise HTTPException(status_code=500, detail=message or "resume failed")
        adr = result.get("result") or {}
        return adr.get("data") if isinstance(adr, dict) and "data" in adr else adr
    except HTTPException:
        raise
    except Exception as e:
        from motet.core.workflow.checkpoint import WorkflowResumeConflict

        if isinstance(e, WorkflowResumeConflict):
            raise HTTPException(status_code=409, detail=str(e)) from e
        logger.error(
            "Failed to resume workflow run",
            workflow_run_id=workflow_run_id,
            error=str(e),
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=f"Failed to resume workflow: {str(e)}")


async def _invoke_workflow_run_control(
    *,
    workflow_run_id: str,
    action: str,
    reason: Optional[str],
    principal: Principal,
) -> Dict[str, Any]:
    """Shared path for POST .../pause and .../cancel."""
    from motet.core.commands.builtin.workflow import (
        workflow_run_control,
        WorkflowRunControlData,
    )
    from motet.core.workflow.checkpoint import WorkflowRunControlConflict
    from ....core.workers import global_invoker

    motet_id, tenant_id, principal_id = get_principal_context(principal)
    command = workflow_run_control(
        task_id=str(uuid.uuid4()),
        conversation_id="",
        data=WorkflowRunControlData(
            workflow_run_id=workflow_run_id,
            action=action,
            reason=reason,
        ),
        tenant_id=tenant_id,
        principal_id=principal_id,
        motet_id=motet_id,
        timeout_seconds=30,
    )
    result = await asyncio.to_thread(global_invoker.execute_command, command)
    if result.get("status") == "error":
        err = result.get("error") or {}
        message = err.get("message") if isinstance(err, dict) else str(err)
        lower = (message or "").lower()
        if "not found" in lower or "expired" in lower:
            raise HTTPException(status_code=404, detail=message)
        if "different principal" in lower:
            raise HTTPException(status_code=403, detail=message)
        if "terminal" in lower or "being resumed" in lower:
            raise HTTPException(status_code=409, detail=message)
        raise HTTPException(status_code=500, detail=message or f"{action} failed")
    adr = result.get("result") or {}
    return adr.get("data") if isinstance(adr, dict) and "data" in adr else adr


@router.post(
    "/runs/{workflow_run_id}/pause",
    summary="Pause a running workflow run",
    description=(
        "Request an operator pause. Already-paused runs are a no-op success. "
        "Running runs pause cooperatively at the next level boundary "
        "(suspend_reason=operator); resume with kind=operator."
    ),
    responses={
        200: {"description": "Pause applied or requested"},
        404: {"description": "Run not found"},
        409: {"description": "Run is terminal"},
    },
)
async def pause_workflow_run(
    workflow_run_id: str,
    req: WorkflowRunControlRequest = Body(default_factory=WorkflowRunControlRequest),
    principal: Principal = Depends(get_current_principal),
):
    """Pause a running workflow run (cooperative) or acknowledge an already-paused run."""
    try:
        return await _invoke_workflow_run_control(
            workflow_run_id=workflow_run_id,
            action="pause",
            reason=req.reason,
            principal=principal,
        )
    except HTTPException:
        raise
    except Exception as e:
        from motet.core.workflow.checkpoint import WorkflowRunControlConflict

        if isinstance(e, WorkflowRunControlConflict):
            raise HTTPException(status_code=409, detail=str(e)) from e
        logger.error(
            "Failed to pause workflow run",
            workflow_run_id=workflow_run_id,
            error=str(e),
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=f"Failed to pause workflow: {str(e)}")


@router.post(
    "/runs/{workflow_run_id}/cancel",
    summary="Cancel a workflow run",
    description=(
        "Cancel a paused run immediately (terminal cancelled), or request cancel "
        "for a running run (honored at the next level boundary). Cascades to nested "
        "child runs and to a parent blocked on this child."
    ),
    responses={
        200: {"description": "Cancel applied or requested"},
        404: {"description": "Run not found"},
        409: {"description": "Run is already completed/failed"},
    },
)
async def cancel_workflow_run(
    workflow_run_id: str,
    req: WorkflowRunControlRequest = Body(default_factory=WorkflowRunControlRequest),
    principal: Principal = Depends(get_current_principal),
):
    """Cancel a paused or running workflow run."""
    try:
        return await _invoke_workflow_run_control(
            workflow_run_id=workflow_run_id,
            action="cancel",
            reason=req.reason,
            principal=principal,
        )
    except HTTPException:
        raise
    except Exception as e:
        from motet.core.workflow.checkpoint import WorkflowRunControlConflict

        if isinstance(e, WorkflowRunControlConflict):
            raise HTTPException(status_code=409, detail=str(e)) from e
        logger.error(
            "Failed to cancel workflow run",
            workflow_run_id=workflow_run_id,
            error=str(e),
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=f"Failed to cancel workflow: {str(e)}")
