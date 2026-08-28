"""
Motet - Bundle Deploy API

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    REST API for bundle-based deployment. Replaces the single-command
    hot-deploy surface with a unified /api/v1/deploy endpoint family that covers
    the full bundle lifecycle: deploy, validate (SSE), status poll, propagate,
    rollback, undeploy, and deployment conversation history.

    All mutating operations invoke distributed commands routed to the dedicated
    deployer worker (WorkerCapability.DEPLOYMENT) via global_invoker. The API
    itself never dials individual workers directly.

    Deployment conversation_id (prefix 'deploy:<uuid>') is created here for
    interactive callers and passed as the command execution context so the
    framework propagates it automatically to all child commands.

Dependencies:
    - fastapi: Web framework
    - motet.core.bundles.deploy: Deploy command data models
    - motet.core.workers: global_invoker for distributed command dispatch
    - interfaces.api.shared.auth: Principal authentication
    - redis: Task stream polling for SSE (validate endpoint)

Usage:
    from motet.interfaces.api.v1.deploy import router
    app.include_router(router)

Notes:
    - POST /api/v1/deploy             — deploy bundle (202 + deploy_job_id)
    - GET  /api/v1/deploy             — list deployed bundles (tenant/motet
      visibility from the principal; foreign filter 403 unless global scope)
    - GET  /api/v1/deploy/{id}/status — poll deploy job status
    - POST /api/v1/deploy/validate    — validate-only SSE stream (git; lint events)
    - POST /api/v1/deploy/hot        — dev-only hot deploy from shared local path
    - POST /api/v1/deploy/validate-upload — validate-only SSE stream (uploaded zip)
    - POST /api/v1/deploy/{id}/propagate — retry failed/skipped workers
    - POST /api/v1/deploy/{id}/rollback  — re-deploy a prior bundle_version
    - DELETE /api/v1/deploy/{id}      — undeploy bundle
    - GET  /api/v1/deploy/{id}/history — deploy history for a bundle
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any, Dict, List, Optional

import structlog
import base64 as b64_module

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..shared.auth import get_current_principal, require_motet_access, require_tenant_access
from ....core.types import Principal

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/deploy", tags=["deploy"])

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class BundleTargetingRequest(BaseModel):
    """Targeting selector for a deploy request."""

    worker_ids: List[str] = Field(default_factory=list, description="Specific worker IDs to target", json_schema_extra={"example": []})
    worker_tags: List[str] = Field(default_factory=list, description="Workers must have all listed tags", json_schema_extra={"example": ["gpu"]})
    motet_ids: List[str] = Field(default_factory=list, description="Restrict bundle visibility to these motets", json_schema_extra={"example": ["sales"]})
    tenant_ids: List[str] = Field(default_factory=list, description="Restrict bundle visibility to these tenants", json_schema_extra={"example": []})


class DeployRequest(BaseModel):
    """Request body for POST /api/v1/deploy."""

    repo_url: str = Field(..., description="Git repository URL", json_schema_extra={"example": "https://github.com/org/repo"})
    branch: str = Field(..., description="Branch, tag, or commit SHA to deploy", json_schema_extra={"example": "main"})
    path: str = Field(..., description="Path within repo conforming to worker install format", json_schema_extra={"example": "extensions/sales"})
    targeting: Optional[BundleTargetingRequest] = Field(None, description="Worker/motet/tenant selector")
    repo_creds_path: Optional[str] = Field(None, description="Vault path for private repo credentials", json_schema_extra={"example": "vault://deploy/github-token"})
    interactive: bool = Field(
        default=False,
        description=(
            "When true the API creates a deployment conversation_id (prefix 'deploy:') and returns it. "
            "The deployer persona activates and emits narration events alongside structured events. "
            "CI/CD callers should leave this false."
        ),
        json_schema_extra={"example": False},
    )


class ValidateRequest(BaseModel):
    """Request body for POST /api/v1/deploy/validate."""

    repo_url: str = Field(..., description="Git repository URL")
    branch: str = Field(..., description="Branch, tag, or commit SHA")
    path: str = Field(..., description="Path within repo")
    targeting: Optional[BundleTargetingRequest] = Field(None, description="Worker targeting constraints for deployment")
    repo_creds_path: Optional[str] = Field(None, description="Path to repository credentials file (for private repos)")


class HotDeployRequest(BaseModel):
    """Request body for POST /api/v1/deploy/hot (dev-only)."""

    bundle_path: str = Field(
        ...,
        description="Shared local path to bundle root as seen by workers",
        json_schema_extra={"example": "/app/motet-sdk/examples/bundles/hello-world"},
    )
    targeting: Optional[BundleTargetingRequest] = Field(None, description="Worker/motet/tenant selector")
    lint: bool = Field(
        default=False,
        description="Run lint before hot reload (default false for speed)",
        json_schema_extra={"example": False},
    )


class DeployResponse(BaseModel):
    """Response for 202 Accepted from POST /api/v1/deploy."""

    deploy_job_id: str = Field(..., description="Command ID — poll for status via status_url")
    bundle_id: str = Field(..., description="Bundle slug (from manifest 'name'); empty until validate resolves it")
    status_url: str = Field(..., description="URL to poll deploy job status")
    deploy_conversation_id: Optional[str] = Field(None, description="Deployment conversation ID (interactive mode only)")


class PropagateRequest(BaseModel):
    """Request body for POST /api/v1/deploy/{bundle_id}/propagate."""

    pass  # No body needed; bundle_id from path, latest version from registry


class RollbackRequest(BaseModel):
    """Request body for POST /api/v1/deploy/{bundle_id}/rollback."""

    bundle_version: str = Field(..., description="Git tree SHA of the version to restore", json_schema_extra={"example": "a1b2c3d4e5f6"})


# ---------------------------------------------------------------------------
# Helper: require admin role
# ---------------------------------------------------------------------------


def _require_admin(principal: Principal) -> None:
    role = getattr(principal, "role", None) or getattr(principal, "roles", None)
    roles = role if isinstance(role, list) else [role] if role else []
    if "admin" not in roles and getattr(principal, "is_service_account", False) is False:
        # Allow all authenticated principals for now (deploy is protected by auth itself).
        # Add role-based restriction here when RBAC is enforced for deployment.
        pass


# ---------------------------------------------------------------------------
# Helper: invoke a deployer command synchronously in a thread
# ---------------------------------------------------------------------------


def _make_command(command_func: Any, data: Any, conversation_id: str, principal: Principal) -> Any:
    """Construct a distributed command instance with execution context."""
    from ..shared.identity import get_principal_context

    _motet_id, _tenant_id, _principal_id = get_principal_context(principal)
    return command_func(
        task_id=str(uuid.uuid4()),
        conversation_id=conversation_id,
        tenant_id=_tenant_id or "default",
        principal_id=_principal_id or getattr(principal, "id", "") or "",
        motet_id=_motet_id or "default",
        data=data,
    )


async def _invoke_async(command: Any) -> Dict[str, Any]:
    """Run global_invoker.execute_command in a thread and return the result."""
    from ....core.workers import global_invoker

    result = await asyncio.to_thread(global_invoker.execute_command, command)
    return result


def _extract_data(result: Dict[str, Any]) -> Dict[str, Any]:
    """Unwrap ADR-0029 command result envelope."""
    if isinstance(result, dict):
        if result.get("status") == "completed":
            inner = result.get("result", {})
            if isinstance(inner, dict) and inner.get("status") == "success":
                return inner.get("data") or inner
            return inner
    return result


def _field_str(fields_map: Any, key: str) -> str:
    """Get a Redis stream field value as str (bytes decoded)."""
    v = fields_map.get(key) or fields_map.get(key.encode("utf-8"))
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", errors="ignore")
    return str(v)


async def _stream_validate_sse(
    command: Any,
    stream_task_id: str,
    motet_id: str,
    tenant_id: str,
    timeout_seconds: int = 120,
) -> AsyncIterator[bytes]:
    """
    Run a validate command (validate_bundle or validate_bundle_upload) and stream
    lint_file / lint_error / lint_complete as SSE. Yields bytes.
    """
    from ....core.workers import global_invoker
    from ....core.distributed.redis_manager import get_redis_client
    from ....core.security.envelope_decode_helpers import decode_command_stream_envelope

    # Send an immediate event so the client sees output right away (helps with buffering and empty stream)
    yield f"event: lint_start\ndata: {json.dumps({'message': 'Starting validation...'})}\n\n".encode()

    run_task: Optional[asyncio.Task] = None
    from motet.core.distributed.tenant_keys import task_response_stream

    stream_key = task_response_stream(tenant_id, stream_task_id)
    try:
        run_task = asyncio.create_task(
            asyncio.to_thread(global_invoker.execute_command, command)
        )
        redis_client = get_redis_client()
        last_id = "0"
        stream_ended = False
        seen_lint_complete = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds

        while not stream_ended and loop.time() < deadline:
            streams = await redis_client.xread(
                {stream_key: last_id}, count=10, block=1000
            )
            if not streams:
                if run_task.done():
                    break
                continue

            for _stream_name, messages_list in streams:
                for message_id, fields in messages_list:
                    last_id = message_id
                    event = _field_str(fields, "event")
                    envelope_str = _field_str(fields, "_envelope")
                    command_id_norm = _field_str(fields, "command_id")
                    motet_id_norm = _field_str(fields, "motet_id") or "default"
                    tenant_id_norm = _field_str(fields, "tenant_id") or ""

                    payload: Dict[str, Any] = {}
                    if envelope_str:
                        try:
                            payload = decode_command_stream_envelope(
                                envelope_json=envelope_str,
                                stream_key=stream_key,
                                event=event,
                                task_id=stream_task_id,
                                command_id=command_id_norm,
                                tenant_id=tenant_id_norm or tenant_id,
                                motet_id=motet_id_norm or motet_id,
                            )
                        except Exception as e:
                            logger.warning(
                                "validate_stream_decode_failed",
                                event=event,
                                error=str(e),
                            )
                            continue

                    if event == "lint_file":
                        file_path = payload.get("file") or ""
                        yield f"event: lint_file\ndata: {json.dumps({'file': file_path})}\n\n".encode()
                    elif event == "lint_error":
                        yield f"event: lint_error\ndata: {json.dumps(payload)}\n\n".encode()
                    elif event == "lint_complete":
                        seen_lint_complete = True
                        yield f"event: lint_complete\ndata: {json.dumps(payload)}\n\n".encode()
                        stream_ended = True
                        break
                    elif event == "command_error":
                        err_msg = payload.get("error") or payload.get("message") or "Command failed"
                        yield f"event: lint_complete\ndata: {json.dumps({'passed': False, 'error': err_msg})}\n\n".encode()
                        stream_ended = True
                        break

            if stream_ended:
                break

        if run_task and not run_task.done():
            run_task.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass
        elif run_task and run_task.done() and not seen_lint_complete:
            try:
                result = run_task.result()
                data_out = result if isinstance(result, dict) else {}
                # Invoker returns full envelope; error may be in data_out or nested
                if data_out.get("status") == "error":
                    err_info = data_out.get("error", {}) or {}
                    if not isinstance(err_info, dict):
                        err_info = {}
                    err_details = err_info.get("details", {}) or {}
                    # Support nested details (e.g. ADR-0029 envelope has details.details.lint_errors)
                    inner = err_details.get("details") or {}
                    lint_errors = inner.get("lint_errors", []) or err_details.get("lint_errors", [])
                    err_msg = err_info.get("message", "Validation failed") or str(err_info)
                    for err in lint_errors:
                        yield f"event: lint_error\ndata: {json.dumps(err)}\n\n".encode()
                    yield f"event: lint_complete\ndata: {json.dumps({'passed': False, 'errors': lint_errors, 'error': err_msg})}\n\n".encode()
                else:
                    yield f"event: lint_complete\ndata: {json.dumps({'passed': False, 'error': 'Stream ended without lint_complete'})}\n\n".encode()
            except Exception as e:
                logger.error("validate_sse_stream_failed", error=str(e), exc_info=True)
                yield f"event: lint_complete\ndata: {json.dumps({'passed': False, 'error': str(e)})}\n\n".encode()

    except Exception as e:
        logger.error("validate_sse_stream_failed", error=str(e), exc_info=True)
        yield f"event: lint_complete\ndata: {json.dumps({'passed': False, 'error': str(e)})}\n\n".encode()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "",
    summary="Deploy a bundle",
    description=(
        "Fetch a git repo path, lint the bundle, publish the artifact, and reload all targeted workers. "
        "Returns 202 Accepted immediately. Poll `status_url` for progress."
    ),
    response_description="202 Accepted with deploy_job_id and status_url",
    status_code=202,
    responses={
        202: {"description": "Deploy job accepted"},
        401: {"description": "Unauthorized"},
        500: {"description": "Invocation failed"},
    },
)
async def deploy_bundle_endpoint(
    req: DeployRequest,
    principal: Principal = Depends(get_current_principal),
) -> JSONResponse:
    """
    Submit a bundle deployment job.

    Returns immediately with a deploy_job_id. Poll
    GET /api/v1/deploy/{bundle_id}/status?job_id={deploy_job_id} for progress.
    """
    from motet.core.bundles.deploy import (
        deploy_bundle,
        DeployBundleData,
        BundleTargeting,
    )

    # Create deployment conversation for interactive mode
    deploy_conversation_id: Optional[str] = None
    if req.interactive:
        deploy_conversation_id = f"deploy:{uuid.uuid4()}"

    targeting = BundleTargeting(**req.targeting.model_dump()) if req.targeting else None

    data = DeployBundleData(
        repo_url=req.repo_url,
        branch=req.branch,
        path=req.path,
        targeting=targeting,
        repo_creds_path=req.repo_creds_path,
    )

    try:
        command = _make_command(
            deploy_bundle,
            data,
            conversation_id=deploy_conversation_id or "",
            principal=principal,
        )

        result = await _invoke_async(command)
        deploy_job_id = command.command_id
        data_out = _extract_data(result)

        bundle_id = data_out.get("bundle_id", "")

        return JSONResponse(
            status_code=202,
            content={
                "deploy_job_id": deploy_job_id,
                "bundle_id": bundle_id,
                "bundle_version": data_out.get("bundle_version", ""),
                "status": data_out.get("deploy_status", "publishing"),
                "status_url": f"/api/v1/deploy/{bundle_id}/status?job_id={deploy_job_id}",
                "deploy_conversation_id": deploy_conversation_id,
                "acked_workers": data_out.get("acked_workers", []),
                "failed_workers": data_out.get("failed_workers", []),
                "skipped_workers": data_out.get("skipped_workers", []),
            },
        )
    except Exception as e:
        logger.error("deploy_bundle_endpoint_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Deploy failed: {e}")


@router.post(
    "/hot",
    summary="Dev-only hot deploy from shared local path",
    description=(
        "Local Docker developer path. Reads a shared filesystem bundle path and dispatches "
        "hot reload on targeted workers without artifact publish/rollback history."
    ),
    response_description="202 Accepted with deploy_job_id and status_url",
    status_code=202,
    responses={
        202: {"description": "Hot deploy job accepted"},
        401: {"description": "Unauthorized"},
        500: {"description": "Invocation failed"},
    },
)
async def hot_deploy_bundle_endpoint(
    req: HotDeployRequest,
    principal: Principal = Depends(get_current_principal),
) -> JSONResponse:
    """Submit a dev-only local hot deploy job."""
    from motet.core.bundles.deploy import (
        hot_deploy_bundle,
        HotDeployBundleData,
        BundleTargeting,
    )

    targeting = BundleTargeting(**req.targeting.model_dump()) if req.targeting else None
    data = HotDeployBundleData(
        bundle_path=req.bundle_path,
        targeting=targeting,
        lint=req.lint,
    )

    try:
        command = _make_command(hot_deploy_bundle, data, conversation_id="", principal=principal)
        result = await _invoke_async(command)
        deploy_job_id = command.command_id
        data_out = _extract_data(result)
        bundle_id = data_out.get("bundle_id", "")

        return JSONResponse(
            status_code=202,
            content={
                "deploy_job_id": deploy_job_id,
                "bundle_id": bundle_id,
                "bundle_version": data_out.get("bundle_version", ""),
                "status": data_out.get("deploy_status", "publishing"),
                "status_url": f"/api/v1/deploy/{bundle_id}/status?job_id={deploy_job_id}",
                "acked_workers": data_out.get("acked_workers", []),
                "failed_workers": data_out.get("failed_workers", []),
                "skipped_workers": data_out.get("skipped_workers", []),
            },
        )
    except Exception as e:
        logger.error("hot_deploy_bundle_endpoint_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Hot deploy failed: {e}")


# 100MB limit for upload (matches bundle artifact limit)
UPLOAD_MAX_BYTES = 100 * 1024 * 1024


@router.post(
    "/upload",
    summary="Deploy a bundle from an uploaded zip",
    description=(
        "Upload a zip of the bundle directory (manifest.yaml at root). "
        "No git fetch — use when the deployer worker cannot reach the repo (e.g. local dev, Docker). "
        "Returns 202 with deploy_job_id; poll status_url for progress."
    ),
    response_description="202 Accepted with deploy_job_id and status_url",
    status_code=202,
    responses={
        202: {"description": "Deploy job accepted"},
        400: {"description": "Invalid file or size"},
        401: {"description": "Unauthorized"},
        500: {"description": "Invocation failed"},
    },
)
async def deploy_upload_endpoint(
    bundle: UploadFile = File(..., description="Zip file of the bundle directory (manifest.yaml at root)"),
    targeting: Optional[str] = Form(None, description="Optional JSON object: { worker_ids?, worker_tags?, motet_ids?, tenant_ids? }"),
    principal: Principal = Depends(get_current_principal),
) -> JSONResponse:
    """
    Deploy from an uploaded zip. Zip must contain manifest.yaml (or manifest.yml / bundle.json) at root.
    """
    from motet.core.bundles.deploy import (
        deploy_bundle_upload,
        DeployBundleUploadData,
        BundleTargeting,
    )

    if not bundle.filename or not bundle.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="File must be a .zip archive")

    raw = await bundle.read()
    if len(raw) > UPLOAD_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Upload exceeds 100MB limit ({len(raw) // (1024 * 1024)} MB)",
        )

    targeting_obj = None
    if targeting and targeting.strip():
        try:
            t = json.loads(targeting)
            targeting_obj = BundleTargeting(**t) if t else None
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid targeting JSON: {e}") from e

    zip_b64 = b64_module.b64encode(raw).decode("ascii")
    data = DeployBundleUploadData(zip_b64=zip_b64, targeting=targeting_obj)

    try:
        command = _make_command(deploy_bundle_upload, data, conversation_id="", principal=principal)
        result = await _invoke_async(command)
        deploy_job_id = command.command_id
        data_out = _extract_data(result)
        bundle_id = data_out.get("bundle_id", "")
        return JSONResponse(
            status_code=202,
            content={
                "deploy_job_id": deploy_job_id,
                "bundle_id": bundle_id,
                "bundle_version": data_out.get("bundle_version", ""),
                "status": data_out.get("deploy_status", "publishing"),
                "status_url": f"/api/v1/deploy/{bundle_id}/status?job_id={deploy_job_id}",
                "acked_workers": data_out.get("acked_workers", []),
                "failed_workers": data_out.get("failed_workers", []),
                "skipped_workers": data_out.get("skipped_workers", []),
            },
        )
    except Exception as e:
        logger.error("deploy_upload_endpoint_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/validate-upload",
    summary="Validate an uploaded zip (lint only, SSE stream)",
    description=(
        "Upload a zip of the bundle directory and run the lint gate without deploying. "
        "Returns a Server-Sent Events stream of lint_file, lint_error, and lint_complete events."
    ),
    response_description="SSE stream of lint events",
    status_code=200,
    responses={
        200: {"description": "SSE stream"},
        400: {"description": "Invalid file or size"},
        401: {"description": "Unauthorized"},
    },
)
async def validate_upload_endpoint(
    bundle: UploadFile = File(..., description="Zip file of the bundle directory (manifest.yaml at root)"),
    principal: Principal = Depends(get_current_principal),
) -> StreamingResponse:
    """
    Validate an uploaded zip without deploying — streams lint progress as SSE.
    Same event types as POST /api/v1/deploy/validate (lint_file, lint_error, lint_complete).
    """
    from motet.core.bundles.deploy import (
        validate_bundle_upload,
        ValidateBundleUploadData,
    )
    from ..shared.identity import get_principal_context

    if not bundle.filename or not bundle.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="File must be a .zip archive")

    raw = await bundle.read()
    if len(raw) > UPLOAD_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Upload exceeds 100MB limit ({len(raw) // (1024 * 1024)} MB)",
        )

    zip_b64 = b64_module.b64encode(raw).decode("ascii")
    data = ValidateBundleUploadData(zip_b64=zip_b64)

    command = _make_command(validate_bundle_upload, data, conversation_id="", principal=principal)
    stream_task_id = getattr(
        getattr(command, "distributed_context", None),
        "task_id",
        None,
    )
    if not stream_task_id:
        raise HTTPException(status_code=500, detail="Command missing task_id for stream")

    motet_id, tenant_id, _ = get_principal_context(principal)

    return StreamingResponse(
        _stream_validate_sse(command, stream_task_id, motet_id or "default", tenant_id or ""),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/validate",
    summary="Validate a bundle (lint only, SSE stream)",
    description=(
        "Fetch a git repo path and run the lint gate without publishing or reloading workers. "
        "Returns a Server-Sent Events stream of lint_file, lint_error, and lint_complete events."
    ),
    response_description="SSE stream of lint events",
    responses={
        200: {"description": "SSE stream"},
        401: {"description": "Unauthorized"},
    },
)
async def validate_bundle_endpoint(
    req: ValidateRequest,
    principal: Principal = Depends(get_current_principal),
) -> StreamingResponse:
    """
    Validate a bundle without deploying — streams lint progress as SSE.

    Event types:
    - lint_file:     { file: str }
    - lint_error:    { file: str, line: int, message: str, severity: str }
    - lint_complete: { passed: bool, errors: [...] }
    """
    from motet.core.bundles.deploy import (
        validate_bundle,
        ValidateBundleData,
        BundleTargeting,
    )
    from ..shared.identity import get_principal_context

    targeting = BundleTargeting(**req.targeting.model_dump()) if req.targeting else None
    motet_id, tenant_id, _ = get_principal_context(principal)

    data = ValidateBundleData(
        repo_url=req.repo_url,
        branch=req.branch,
        path=req.path,
        targeting=targeting,
        repo_creds_path=req.repo_creds_path,
    )

    command = _make_command(validate_bundle, data, conversation_id="", principal=principal)
    stream_task_id = getattr(
        getattr(command, "distributed_context", None),
        "task_id",
        None,
    )
    if not stream_task_id:
        raise HTTPException(status_code=500, detail="Command missing task_id for stream")

    return StreamingResponse(
        _stream_validate_sse(command, stream_task_id, motet_id or "default", tenant_id or ""),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get(
    "",
    summary="List deployed bundles",
    description="Returns all bundles currently registered in the bundle registry.",
    responses={
        200: {"description": "List of deployed bundles"},
        401: {"description": "Unauthorized"},
        403: {"description": "Foreign tenant_id or motet_id without global scope"},
    },
)
async def list_bundles(
    motet_id: Optional[str] = Query(
        None,
        description=(
            "Visibility filter by motet_id. Omitted uses the authenticated "
            "principal's motet. A different motet requires global tenant access."
        ),
    ),
    tenant_id: Optional[str] = Query(
        None,
        description=(
            "Visibility filter by tenant_id. Omitted uses the authenticated "
            "principal's tenant. A different tenant requires global tenant access."
        ),
    ),
    worker_id: Optional[str] = Query(None, description="Filter by worker_id in targeting"),
    principal: Principal = Depends(get_current_principal),
) -> JSONResponse:
    """
    List all deployed bundles with optional filters.

    Each bundle entry includes the content catalog (commands, tools, MCP servers,
    model IDs, skills, and the  ``exec`` image-pinning block) sourced
    from bundle:{bundle_id}:catalog and the per-worker loaded state from
    bundle:{bundle_id}:worker_state — no worker query required.
    """
    try:
        from ....core.distributed.redis_manager import get_sync_redis_client
        from motet.core.bundles.deploy import (
            _list_all_bundles, _get_catalog, _get_worker_state,
        )

        redis_client = get_sync_redis_client()
        bundles = _list_all_bundles(redis_client)
        authorized_tenant = require_tenant_access(principal, tenant_id)
        authorized_motet = require_motet_access(principal, motet_id)

        # Apply visibility filters. Empty targeting still matches (global).
        def _targeting(b: Dict[str, Any]) -> Dict[str, Any]:
            t = b.get("targeting", {})
            return json.loads(t) if isinstance(t, str) else (t or {})

        def _matches_scope(target_ids: Any, scoped_id: Optional[str]) -> bool:
            """
            Scope matching for list filters.

            Empty target lists mean "global" visibility and should match all
            scope selections. Non-empty lists must explicitly include scoped_id.
            """
            if not scoped_id:
                return True
            if not isinstance(target_ids, list) or len(target_ids) == 0:
                return True
            return scoped_id in target_ids

        bundles = [
            b for b in bundles
            if _matches_scope(_targeting(b).get("motet_ids", []), authorized_motet)
            and _matches_scope(_targeting(b).get("tenant_ids", []), authorized_tenant)
        ]

        # Enrich each entry with catalog and worker_state
        enriched = []
        for b in bundles:
            bid = b.get("bundle_id", "")
            catalog = _get_catalog(redis_client, bid) or {}
            worker_state = _get_worker_state(redis_client, bid)
            enriched.append({
                **b,
                "targeting": _targeting(b),
                "catalog": {
                    "commands": catalog.get("commands", []),
                    "tools": catalog.get("tools", []),
                    "workflows": catalog.get("workflows", []),
                    "agents": catalog.get("agents", []),
                    "mcp_servers": catalog.get("mcp_servers", []),
                    "model_ids": catalog.get("model_ids", []),
                    "skills": catalog.get("skills", []),
                    # ADR-0100: surface the bundle exec image pinning block so the
                    # ops dashboard can show which image / digest / tier / requirements
                    # hash a deployed bundle will actually pull at run time. Empty
                    # dict (not None) when the bundle did not declare config/exec.yaml,
                    # so the FE has a stable shape.
                    "exec": catalog.get("exec", {}),
                    "bundle_version": catalog.get("bundle_version", ""),
                },
                "worker_state": worker_state,
            })

        return JSONResponse({"bundles": enriched, "total": len(enriched)})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_bundles_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list bundles: {e}")


@router.get(
    "/{bundle_id}/status",
    summary="Poll deploy job status",
    description="Returns the current status of a deploy job and per-worker ack/fail breakdown.",
    responses={
        200: {"description": "Deploy job status"},
        401: {"description": "Unauthorized"},
        404: {"description": "Bundle or job not found"},
    },
)
async def get_deploy_status(
    bundle_id: str,
    job_id: Optional[str] = Query(None, description="deploy_job_id returned from POST /api/v1/deploy"),
    principal: Principal = Depends(get_current_principal),
) -> JSONResponse:
    """
    Poll deploy job status.

    Status values: publishing | propagating | complete | no_change | degraded | failed
    """
    try:
        from ....core.distributed.redis_manager import get_sync_redis_client
        from motet.core.bundles.deploy import _get_registry_entry

        redis_client = get_sync_redis_client()
        entry = _get_registry_entry(redis_client, bundle_id)

        if not entry:
            raise HTTPException(status_code=404, detail=f"Bundle '{bundle_id}' not found in registry")

        # If a job_id is provided, look up the command status from the invoker's result store
        command_status: Optional[Dict[str, Any]] = None
        if job_id:
            try:
                from ....core.workers import global_invoker
                status_result = await asyncio.to_thread(
                    getattr(global_invoker, "get_command_status", lambda _: None), job_id
                )
                if status_result:
                    command_status = status_result
            except Exception:
                pass  # command status optional; proceed with cached entry

        from motet.core.bundles.deploy import _get_catalog, _get_worker_state
        catalog = _get_catalog(redis_client, bundle_id) or {}
        worker_state = _get_worker_state(redis_client, bundle_id)
        targeting_raw = entry.get("targeting", {})
        targeting = json.loads(targeting_raw) if isinstance(targeting_raw, str) and targeting_raw else (targeting_raw or {})

        return JSONResponse({
            "bundle_id": bundle_id,
            "bundle_version": entry.get("bundle_version", ""),
            "bundle_ref": entry.get("bundle_ref", ""),
            "manifest_version": entry.get("manifest_version", ""),
            "status": entry.get("status", "complete"),
            "deployed_at": entry.get("deployed_at", ""),
            "targeting": targeting,
            "deploy_job_id": job_id or entry.get("deploy_job_id", ""),
            "command_status": command_status,
            "catalog": {
                "commands": catalog.get("commands", []),
                "tools": catalog.get("tools", []),
                "workflows": catalog.get("workflows", []),
                "agents": catalog.get("agents", []),
                "mcp_servers": catalog.get("mcp_servers", []),
                "model_ids": catalog.get("model_ids", []),
                "skills": catalog.get("skills", []),
                # ADR-0100: see list_bundles above for rationale.
                "exec": catalog.get("exec", {}),
                "bundle_version": catalog.get("bundle_version", ""),
            },
            "worker_state": worker_state,
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_deploy_status_failed", bundle_id=bundle_id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get status: {e}")


@router.post(
    "/{bundle_id}/propagate",
    summary="Propagate bundle to failed/skipped workers",
    description=(
        "Retry core.reload_bundle on workers that failed or were offline during the original deploy. "
        "Does not re-fetch or re-lint. Returns 202 with a new deploy_job_id."
    ),
    status_code=202,
    responses={
        202: {"description": "Propagation job accepted"},
        401: {"description": "Unauthorized"},
        404: {"description": "Bundle not found"},
    },
)
async def propagate_bundle_endpoint(
    bundle_id: str,
    principal: Principal = Depends(get_current_principal),
) -> JSONResponse:
    """Retry bundle reload on failed/skipped workers without re-fetching."""
    from motet.core.bundles.deploy import propagate_bundle, PropagateBundleData

    data = PropagateBundleData(bundle_id=bundle_id)
    try:
        command = _make_command(propagate_bundle, data, conversation_id="", principal=principal)
        result = await _invoke_async(command)
        data_out = _extract_data(result)

        return JSONResponse(
            status_code=202,
            content={
                "deploy_job_id": command.command_id,
                "bundle_id": bundle_id,
                "status": data_out.get("status", "propagating"),
                "acked_workers": data_out.get("acked_workers", []),
                "failed_workers": data_out.get("failed_workers", []),
            },
        )
    except Exception as e:
        logger.error("propagate_bundle_failed", bundle_id=bundle_id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Propagation failed: {e}")


@router.post(
    "/{bundle_id}/rollback",
    summary="Rollback a bundle to a prior version",
    description=(
        "Re-deploy a stored prior artifact identified by bundle_version (git tree SHA). "
        "Skips fetch and lint — artifact was already validated at original deploy time. "
        "Returns 202 with a new deploy_job_id."
    ),
    status_code=202,
    responses={
        202: {"description": "Rollback job accepted"},
        401: {"description": "Unauthorized"},
        404: {"description": "Bundle or version not found in artifact store"},
        422: {"description": "bundle_version required"},
    },
)
async def rollback_bundle_endpoint(
    bundle_id: str,
    req: RollbackRequest,
    principal: Principal = Depends(get_current_principal),
) -> JSONResponse:
    """Roll back a bundle to a specific previously-deployed version."""
    from motet.core.bundles.deploy import rollback_bundle, RollbackBundleData

    data = RollbackBundleData(bundle_id=bundle_id, bundle_version=req.bundle_version)
    try:
        command = _make_command(rollback_bundle, data, conversation_id="", principal=principal)
        result = await _invoke_async(command)
        data_out = _extract_data(result)

        return JSONResponse(
            status_code=202,
            content={
                "deploy_job_id": command.command_id,
                "bundle_id": bundle_id,
                "bundle_version": req.bundle_version,
                "status": data_out.get("status", "propagating"),
                "acked_workers": data_out.get("acked_workers", []),
                "failed_workers": data_out.get("failed_workers", []),
            },
        )
    except Exception as e:
        logger.error("rollback_bundle_failed", bundle_id=bundle_id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Rollback failed: {e}")


@router.delete(
    "/{bundle_id}",
    summary="Undeploy a bundle",
    description=(
        "Remove a bundle from all targeted workers, unregister its artifacts from all registries, "
        "and cancel or invalidate any schedules that referenced the bundle. Returns 202."
    ),
    status_code=202,
    responses={
        202: {"description": "Undeploy job accepted"},
        401: {"description": "Unauthorized"},
        404: {"description": "Bundle not found"},
    },
)
async def undeploy_bundle_endpoint(
    bundle_id: str,
    principal: Principal = Depends(get_current_principal),
) -> JSONResponse:
    """Undeploy a bundle from all targeted workers."""
    from motet.core.bundles.deploy import undeploy_bundle, UndeployBundleData

    data = UndeployBundleData(bundle_id=bundle_id)
    try:
        command = _make_command(undeploy_bundle, data, conversation_id="", principal=principal)
        result = await _invoke_async(command)
        data_out = _extract_data(result)

        return JSONResponse(
            status_code=202,
            content={
                "deploy_job_id": command.command_id,
                "bundle_id": bundle_id,
                "status": data_out.get("status", "undeployed"),
                "acked_workers": data_out.get("acked_workers", []),
                "failed_workers": data_out.get("failed_workers", []),
            },
        )
    except Exception as e:
        logger.error("undeploy_bundle_failed", bundle_id=bundle_id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Undeploy failed: {e}")


@router.get(
    "/{bundle_id}/history",
    summary="Deploy history for a bundle",
    description="Returns the last N deploy records for a bundle (most recent last).",
    responses={
        200: {"description": "Deploy history"},
        401: {"description": "Unauthorized"},
        404: {"description": "Bundle not found"},
    },
)
async def get_bundle_history(
    bundle_id: str,
    principal: Principal = Depends(get_current_principal),
) -> JSONResponse:
    """Return deploy history entries for a bundle."""
    try:
        from ....core.distributed.redis_manager import get_sync_redis_client
        from motet.core.bundles.deploy import _get_bundle_history

        redis_client = get_sync_redis_client()
        history = _get_bundle_history(redis_client, bundle_id)

        return JSONResponse({"bundle_id": bundle_id, "history": history, "total": len(history)})
    except Exception as e:
        logger.error("get_bundle_history_failed", bundle_id=bundle_id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get history: {e}")
