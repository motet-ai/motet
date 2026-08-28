"""
Motet - MCP Servers API

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Operator API for per-MCP-service health and control-plane actions.
    Lists records published by the sibling MCP instance manager (Redis hash)
    and enqueues register / unregister / restart / disable / enable commands
    on the manager control stream. Does not scrape YAML from this process.

Dependencies:
    - fastapi: REST surface
    - motet.core.tools.mcp_motet.manager.service_status: Redis status hash
    - motet.core.tools.mcp_motet.manager.control_commands: Redis control stream

Usage:
    GET /api/v1/mcp/servers
    POST /api/v1/mcp/servers/{service_id}/restart

Notes:
    - GET and mutations require authentication. Mutations require admin.
    - Mutations enqueue Redis commands; they do not call get_instance_manager().
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..shared.auth import get_current_principal, require_admin_principal
from ....core.types import Principal

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/mcp", tags=["mcp"])


class MCPServerEntry(BaseModel):
    """One configured MCP service as shown to operators (no secrets)."""

    service_id: str = Field(..., description="Configured service_id")
    manager_id: str = Field(..., description="Owning sibling manager_id")
    status: str = Field(..., description="running | starting | failed | auth_required | not_started | disabled")
    healthy: bool = Field(..., description="True when a child process/transport is live")
    transport: str = Field(..., description="stdio | http | streamable-http")
    visibility: Optional[str] = Field(default=None, description="Visibility")
    lifecycle_duration: Optional[str] = Field(default=None, description="Lifecycle")
    state_model: Optional[str] = Field(default=None, description="State model")
    auth_type: str = Field(default="none", description="none | oauth2 | api_key | service_account")
    instance_count: int = Field(..., description="Live instance keys")
    instance_ids: List[str] = Field(default_factory=list, description="Live instance keys")
    pids: List[int] = Field(default_factory=list, description="Live child PIDs")
    restart_count_window: int = Field(default=0, description="Restarts in budget window")
    restart_budget_remaining: int = Field(default=0, description="Restarts still allowed")
    last_error: Optional[str] = Field(default=None, description="Short last error")
    last_ready_at: Optional[float] = Field(default=None, description="Unix time of service_ready")
    last_removed_at: Optional[float] = Field(default=None, description="Unix time of service_removed")
    last_restarted_at: Optional[float] = Field(default=None, description="Unix time of last restart")
    tool_names: List[str] = Field(default_factory=list, description="Discovery tool names")
    tool_count: int = Field(default=0, description="len(tool_names)")
    updated_at: float = Field(..., description="Unix time this record was published")
    disabled: bool = Field(default=False, description="Operator/bundle disabled")


class MCPServersResponse(BaseModel):
    """List payload for the MCP Servers dashboard page."""

    status: str = Field(..., description="success")
    servers: List[MCPServerEntry] = Field(default_factory=list)
    timestamp: float = Field(..., description="Unix time of this response")


class MCPServerActionRequest(BaseModel):
    """Optional body for control-plane mutations."""

    manager_id: Optional[str] = Field(
        default=None,
        description="Target manager_id; defaults to MOTET_MCP_MANAGER_ID",
        json_schema_extra={"example": "mcp-local-default"},
    )
    config: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Required for register: MCPInstanceConfig-compatible dict",
    )


def _resolve_target_manager_id(explicit: Optional[str]) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    from ....core.tools.mcp_motet.manager.control_commands import resolve_mcp_manager_id

    resolved = resolve_mcp_manager_id()
    if not resolved:
        raise HTTPException(
            status_code=503,
            detail="MOTET_MCP_MANAGER_ID is not set; cannot target the sibling MCP manager",
        )
    return resolved


@router.get(
    "/servers",
    response_model=MCPServersResponse,
    summary="List MCP servers and health",
    responses={200: {"description": "Per-service health records"}},
)
async def list_mcp_servers(
    manager_id: Optional[str] = Query(
        None,
        description="Filter to one manager_id. Omit to list all managers.",
    ),
    principal: Principal = Depends(get_current_principal),
) -> JSONResponse:
    """Return per-service MCP health published by sibling manager(s).

    Requires authentication. Payloads contain no vault tokens.
    """
    import time

    from ....core.tools.mcp_motet.manager.service_status import list_mcp_service_statuses

    try:
        records = list_mcp_service_statuses(manager_id)
        servers = [
            MCPServerEntry(
                **rec.model_dump(),
                tool_count=len(rec.tool_names),
            )
            for rec in records
        ]
        servers.sort(key=lambda s: (s.manager_id, s.service_id))
        return JSONResponse(
            MCPServersResponse(
                status="success",
                servers=servers,
                timestamp=time.time(),
            ).model_dump()
        )
    except Exception as e:
        logger.error("mcp_servers_list_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list MCP servers: {e}") from e


def _enqueue(
    op: str,
    service_id: str,
    body: Optional[MCPServerActionRequest],
    principal: Principal,
) -> JSONResponse:
    from ....core.tools.mcp_motet.manager.control_commands import enqueue_mcp_control_command

    require_admin_principal(principal, detail="Admin role required for MCP control actions")
    payload: Dict[str, Any] = {"op": op, "service_id": service_id}
    manager_id = _resolve_target_manager_id(body.manager_id if body else None)
    if op == "register":
        if not body or not body.config:
            raise HTTPException(status_code=400, detail="register requires config")
        payload["config"] = body.config
    try:
        entry_id = enqueue_mcp_control_command(manager_id, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error("mcp_control_enqueue_failed", op=op, error=str(e), exc_info=True)
        raise HTTPException(status_code=502, detail=f"Failed to enqueue {op}: {e}") from e
    logger.info(
        "admin_audit",
        action=f"mcp_server_{op}",
        service_id=service_id,
        manager_id=manager_id,
        principal_id=principal.id,
        tenant_id=principal.tenant_id,
        entry_id=entry_id,
    )
    return JSONResponse(
        {
            "status": "accepted",
            "op": op,
            "service_id": service_id,
            "manager_id": manager_id,
            "entry_id": entry_id,
        },
        status_code=202,
    )


@router.post(
    "/servers/{service_id}/restart",
    summary="Restart one MCP service",
    responses={202: {"description": "Command enqueued"}},
)
async def restart_mcp_server(
    service_id: str,
    body: Optional[MCPServerActionRequest] = None,
    principal: Principal = Depends(get_current_principal),
) -> JSONResponse:
    """Enqueue restart for one service on the sibling manager."""
    return _enqueue("restart", service_id, body, principal)


@router.post(
    "/servers/{service_id}/disable",
    summary="Disable one MCP service",
    responses={202: {"description": "Command enqueued"}},
)
async def disable_mcp_server(
    service_id: str,
    body: Optional[MCPServerActionRequest] = None,
    principal: Principal = Depends(get_current_principal),
) -> JSONResponse:
    """Stop instances but keep the service config (disabled)."""
    return _enqueue("disable", service_id, body, principal)


@router.post(
    "/servers/{service_id}/enable",
    summary="Enable one MCP service",
    responses={202: {"description": "Command enqueued"}},
)
async def enable_mcp_server(
    service_id: str,
    body: Optional[MCPServerActionRequest] = None,
    principal: Principal = Depends(get_current_principal),
) -> JSONResponse:
    """Re-enable a disabled service and bootstrap it."""
    return _enqueue("enable", service_id, body, principal)


@router.post(
    "/servers/{service_id}/register",
    summary="Register or replace one MCP service",
    responses={202: {"description": "Command enqueued"}},
)
async def register_mcp_server(
    service_id: str,
    body: MCPServerActionRequest,
    principal: Principal = Depends(get_current_principal),
) -> JSONResponse:
    """Enqueue register_server_config on the sibling manager."""
    return _enqueue("register", service_id, body, principal)


@router.delete(
    "/servers/{service_id}",
    summary="Unregister one MCP service",
    responses={202: {"description": "Command enqueued"}},
)
async def unregister_mcp_server(
    service_id: str,
    manager_id: Optional[str] = Query(None, description="Target manager_id"),
    principal: Principal = Depends(get_current_principal),
) -> JSONResponse:
    """Enqueue unregister_server_config on the sibling manager."""
    return _enqueue(
        "unregister",
        service_id,
        MCPServerActionRequest(manager_id=manager_id),
        principal,
    )
