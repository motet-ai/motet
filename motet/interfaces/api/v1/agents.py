"""
Motet - Agents API

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Agent discovery API for agent configuration registry. Provides a
    list endpoint for available agents filtered by principal role visibility
    (full AgentConfig fields including model, loop limits, skills, metadata),
    plus a manage-UI endpoint to set per-agent surface allow-list overlays
    (surfaces catalog).

Dependencies:
    - fastapi: Route declaration and dependency injection
    - pydantic: Response models with OpenAPI metadata
    - motet.core.agents: AgentConfigRegistry singleton access
    - motet.core.surfaces: SurfaceRegistry agent allow-list overlays

Usage:
    from motet.interfaces.api.v1.agents import router
    app.include_router(router)

Notes:
    - Endpoint lists only agents visible to the current principal role set.
    - Qualified IDs follow namespace rules (`core.<id>` or `<bundle>.<id>`).
    - ``allowed_surface_ids`` null means all catalog surfaces; Redis overlays override
      AgentConfig for manage-UI edits.
    - Optional ``tenant_id`` / ``motet_id`` hide bundle agents whose catalog
      targeting excludes that scope. Core agents stay visible.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
import structlog

from ..shared.auth import can_access_all_tenants, get_current_principal
from ..shared.scope import ManageAppScope, get_manage_app_scope
from ....core.types import Principal

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])
logger = structlog.get_logger(__name__)


def _agent_matches_bundle_scope(
    agent: "AgentListItem",
    catalogs: Dict[str, Dict[str, Any]],
    tenant_id: Optional[str],
    motet_id: Optional[str],
) -> bool:
    """Core agents are global; bundle agents honor catalog targeting lists."""
    wanted_tenant = (tenant_id or "").strip()
    wanted_motet = (motet_id or "").strip()
    if not wanted_tenant and not wanted_motet:
        return True
    bundle_id = (agent.bundle_id or "").strip()
    if not bundle_id:
        return True
    targeting = (catalogs.get(bundle_id) or {}).get("targeting") or {}
    if not isinstance(targeting, dict):
        return True
    tenant_ids = targeting.get("tenant_ids") or []
    motet_ids = targeting.get("motet_ids") or []
    if wanted_tenant and tenant_ids and wanted_tenant not in tenant_ids:
        return False
    if wanted_motet and motet_ids and wanted_motet not in motet_ids:
        return False
    return True


class AgentListItem(BaseModel):
    """Single agent entry returned by the discovery API."""

    qualified_id: str = Field(
        ...,
        description="Fully-qualified agent identifier.",
        json_schema_extra={"example": "core.default"},
    )
    agent_id: str = Field(
        ...,
        description="Bare agent identifier from registry entry.",
        json_schema_extra={"example": "default"},
    )
    bundle_id: Optional[str] = Field(
        default=None,
        description="Bundle namespace for deployed agents. Null for built-ins.",
        json_schema_extra={"example": None},
    )
    display_name: str = Field(
        default="",
        description="Human-readable name for UI display.",
        json_schema_extra={"example": "Motet Agent"},
    )
    description: str = Field(
        default="",
        description="Short description of the agent behavior.",
        json_schema_extra={"example": "General-purpose agent with full tool discovery."},
    )
    allowed_roles: List[str] = Field(
        default_factory=list,
        description="Roles allowed to invoke this agent (`*` means any principal).",
        json_schema_extra={"example": ["*"]},
    )
    aliases: List[str] = Field(
        default_factory=list,
        description="Configured bare aliases that resolve to this agent.",
        json_schema_extra={"example": ["agent", "default"]},
    )
    system_prompt: str = Field(
        default="",
        description="System prompt defining the agent's identity, behavior, and constraints.",
        json_schema_extra={"example": "You are a helpful assistant."},
    )
    tool_filter: Dict[str, Any] = Field(
        default_factory=dict,
        description="ToolFilter config used to resolve callable tools for the agent.",
    )
    turn_hooks: Dict[str, Any] = Field(
        default_factory=dict,
        description="Turn hook command mapping around the core reasoning loop.",
    )
    allowed_surface_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "Surfaces this agent may use. Null means all catalog surfaces. "
            "Effective value merges AgentConfig with manage-UI Redis overlay."
        ),
        json_schema_extra={"example": ["demo_chat", "openai_compat"]},
    )
    model_provider: Optional[str] = Field(
        default=None,
        description="LLM provider override. Null uses the stack default.",
        json_schema_extra={"example": "xai"},
    )
    model_name: Optional[str] = Field(
        default=None,
        description="Model name override. Null uses the stack default.",
        json_schema_extra={"example": "grok-4.5"},
    )
    model_profile_name: Optional[str] = Field(
        default=None,
        description="Model profile name for routing.",
        json_schema_extra={"example": "default"},
    )
    temperature: float = Field(
        default=0.2,
        description="Sampling temperature.",
        json_schema_extra={"example": 0.2},
    )
    max_iterations: int = Field(
        default=20,
        description="Maximum Motet-tool recursion iterations in the agentic loop.",
        json_schema_extra={"example": 20},
    )
    max_model_calls: Optional[int] = Field(
        default=None,
        description=(
            "Hard cap on model inference calls per turn. Null defaults to "
            "max(max_iterations * 3, 30)."
        ),
        json_schema_extra={"example": 60},
    )
    max_tools: int = Field(
        default=20,
        description="Maximum tools per iteration.",
        json_schema_extra={"example": 20},
    )
    enable_thinking: bool = Field(
        default=False,
        description="Whether extended thinking is enabled for capable models.",
        json_schema_extra={"example": False},
    )
    reasoning_effort: Optional[str] = Field(
        default=None,
        description="Reasoning effort when enable_thinking is true.",
        json_schema_extra={"example": "medium"},
    )
    conversation_id_prefix: Optional[str] = Field(
        default=None,
        description="Prefix for auto-generated conversation IDs.",
        json_schema_extra={"example": "admin:"},
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Opaque agent metadata (e.g. prompt_policy) passed to LoopContext.",
        json_schema_extra={"example": {"prompt_policy": "client_system_primary"}},
    )
    skill_ids: Optional[List[str]] = Field(
        default=None,
        description="Explicit skill allowlist (canonical skill ids).",
        json_schema_extra={"example": ["skills-vendor-demo.pdf"]},
    )
    skill_mode: str = Field(
        default="allowlist",
        description="Skill selection mode: allowlist or discovery.",
        json_schema_extra={"example": "allowlist"},
    )
    skill_max_per_turn: int = Field(
        default=3,
        description="Maximum explicit user-requested skills activated per turn.",
        json_schema_extra={"example": 3},
    )


class AgentSurfacesUpdateRequest(BaseModel):
    """Set or clear the surface allow-list overlay for an agent."""

    allowed_surface_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "Surface catalog ids the agent may use. Omit/null with clear=true to "
            "remove the overlay (fall back to AgentConfig / all). Empty list means "
            "all catalog surfaces. Non-empty list must reference existing surfaces."
        ),
        json_schema_extra={"example": ["demo_chat", "cli"]},
    )
    clear: bool = Field(
        default=False,
        description="When true, delete the Redis overlay so config/default-all applies.",
        json_schema_extra={"example": False},
    )


class AgentSurfacesResponse(BaseModel):
    """Effective surface allow-list for an agent after update."""

    qualified_id: str = Field(
        ...,
        description="Fully-qualified agent identifier.",
        json_schema_extra={"example": "core.default"},
    )
    allowed_surface_ids: Optional[List[str]] = Field(
        default=None,
        description="Effective allow-list; null means all catalog surfaces.",
        json_schema_extra={"example": ["demo_chat"]},
    )


class AgentListResponse(BaseModel):
    """Response model for listing visible agents."""

    agents: List[AgentListItem] = Field(
        default_factory=list,
        description="Agents visible to the current principal.",
    )
    total: int = Field(
        ...,
        description="Total number of returned agents.",
        json_schema_extra={"example": 2},
    )


def sync_bundle_agents_into_api_registry() -> int:
    """Backward-compatible wrapper for chat path registry hydration."""
    from ....core.agents.discovery import sync_bundle_agents_into_registry
    return sync_bundle_agents_into_registry()


@router.get(
    "",
    summary="List available agents",
    description="Return agent configurations visible to the current principal.",
    response_model=AgentListResponse,
    response_description="List of visible agent configurations.",
    responses={
        200: {"description": "Successfully returned visible agents."},
    },
)
async def list_agents(
    role: Optional[str] = Header(default=None, alias="X-Role"),
    scope: ManageAppScope = Depends(get_manage_app_scope),
    principal: Principal = Depends(get_current_principal),
) -> AgentListResponse:
    """List visible agents via distributed core.agent_list command."""
    principal_roles = list(getattr(principal, "roles", []) or [])
    if role:
        principal_roles.append(role)

    try:
        from motet.core.commands.builtin.agents import agent_list, AgentListData
        from ....core.workers import global_invoker

        command = agent_list(
            task_id=str(uuid4()),
            conversation_id="",
            data=AgentListData(principal_roles=principal_roles),
            tenant_id=scope.tenant_id or getattr(principal, "tenant_id", None) or "default",
            motet_id=scope.motet_id or getattr(principal, "motet_id", None) or "",
            principal_id=getattr(principal, "id", "") or "",
            trace_id=None,
            timeout_seconds=30,
            priority=5,
            max_retries=1,
        )
        result = await asyncio.to_thread(global_invoker.execute_command, command)
    except Exception as e:
        logger.warning("distributed_agent_list_unavailable", error=str(e))
        raise HTTPException(status_code=503, detail="Agent listing unavailable: no worker route")

    if result.get("status") == "error":
        raise HTTPException(status_code=503, detail=f"Agent listing failed: {result.get('error')}")

    inner = result.get("result", {})
    if inner.get("status") != "success":
        raise HTTPException(status_code=503, detail=f"Agent listing failed: {inner.get('error')}")

    payload = inner.get("data", {}) or {}
    raw_agents = payload.get("agents", [])
    agents = [AgentListItem(**item) for item in raw_agents if isinstance(item, dict)]
    if scope.is_set:
        catalogs: Dict[str, Dict[str, Any]] = {}
        try:
            from motet.core.bundles.deploy import _list_all_catalogs
            from motet.core.distributed.redis_manager import get_sync_redis_client

            catalogs = _list_all_catalogs(get_sync_redis_client("agent_list_scope")) or {}
        except Exception as e:
            logger.warning("agent_list_catalog_scope_unavailable", error=str(e))
        agents = [
            agent
            for agent in agents
            if _agent_matches_bundle_scope(agent, catalogs, scope.tenant_id, scope.motet_id)
        ]
    return AgentListResponse(agents=agents, total=len(agents))


@router.put(
    "/{qualified_id}/surfaces",
    summary="Set agent surface allow-list",
    description=(
        "Set or clear the Redis surface allow-list overlay for an agent (admin). "
        "Does not mutate AgentConfig in the in-process registry; overlays win at "
        "serialize/list time."
    ),
    response_model=AgentSurfacesResponse,
    responses={
        200: {"description": "Allow-list updated"},
        400: {"description": "Invalid surface ids"},
        403: {"description": "Admin required"},
        404: {"description": "Agent not found"},
    },
)
async def put_agent_surfaces(
    qualified_id: str,
    body: AgentSurfacesUpdateRequest,
    principal: Principal = Depends(get_current_principal),
) -> AgentSurfacesResponse:
    """Update the manage-UI surface allow-list overlay for one agent."""
    if not can_access_all_tenants(principal):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required to update agent surface allow-lists",
        )

    qid = (qualified_id or "").strip()
    if not qid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="qualified_id required")

    # Best-effort existence check against local registry (may miss remote-only agents).
    try:
        from ....core.agents import get_agent_registry
        from ....core.agents.discovery import sync_bundle_agents_into_registry

        sync_bundle_agents_into_registry()
        if get_agent_registry().get(qid) is None:
            # Still allow overlay write for agents that exist only on workers.
            logger.info("agent_surfaces_update_unknown_locally", qualified_id=qid)
    except Exception as e:
        logger.warning("agent_surfaces_existence_check_failed", error=str(e))

    try:
        from ....core.surfaces import SurfaceRegistry, resolve_effective_allowlist

        reg = SurfaceRegistry()
        reg.ensure_builtins()
        if body.clear:
            reg.clear_agent_allowlist(qid)
        else:
            reg.set_agent_allowlist(qid, body.allowed_surface_ids, clear=False)

        config_ids: Optional[List[str]] = None
        try:
            from ....core.agents import get_agent_registry

            cfg = get_agent_registry().get(qid)
            if cfg is not None:
                raw = getattr(cfg, "allowed_surface_ids", None)
                if isinstance(raw, list):
                    config_ids = list(raw)
        except Exception:
            config_ids = None

        effective = resolve_effective_allowlist(
            qualified_agent_id=qid,
            config_allowed_surface_ids=config_ids,
            registry=reg,
        )
    except Exception as e:
        from ....core.surfaces import SurfaceValidationError

        if isinstance(e, SurfaceValidationError):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
        logger.error("agent_surfaces_update_failed", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update agent surfaces: {e}",
        ) from e

    return AgentSurfacesResponse(qualified_id=qid, allowed_surface_ids=effective)

