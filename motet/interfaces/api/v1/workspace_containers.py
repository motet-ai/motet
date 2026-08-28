"""
Motet - Workspace Containers API

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Read-only operator surface for per-conversation workspace containers.
    Surfaces the bindings the
    ``WorkspaceContainerRegistry`` already publishes — one row per
    ``(tenant_id, conversation_id, bundle_id, skill_name, image_stack)`` tuple
    — together with the operator-visible knobs (idle TTL, per-tenant cap,
    master kill switch, stateful-mode gate) so the dashboard can show "what's the
    platform's current opinion of workspace containers?" without the operator
    opening a shell.

    Why a dedicated route instead of a sub-resource of /workers:
    Workspace containers are intentionally *worker-stateless*. They have no parent worker — any worker may
        dispatch to any container. Hanging them off /workers would
        misrepresent that ownership relationship and force a
        cross-product join in the UI. They get their own top-level
    resource for the same reason instance-managers do.

    Endpoints:
    - GET /api/v1/workspace-containers
        List all currently-bound workspace containers with per-tenant
        filtering. Includes a ``config`` block describing the active
        operator gates, and per-row ``idle_seconds`` so the dashboard
        can age containers in the table.

    Authentication:
    JWT or service account. Non-admins see only their own
    tenant. Admins may omit ``tenant_id`` to list every tenant.
    Sensitive container internals (env, mounts, full ID) are
    deliberately not surfaced here.
"""

from __future__ import annotations

import os
import time
from typing import Dict, List, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..shared.auth import can_access_all_tenants, get_current_principal, require_tenant_access
from ....core.types import Principal

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/workspace-containers", tags=["workspace-containers"])


class WorkspaceContainerEntry(BaseModel):
    """One per-workspace container as exposed to the ops UI.

    Mirrors :class:`WorkspaceContainerBinding` minus low-value internals
    (Redis keys, raw substrate metadata) and plus computed niceties
    (``idle_seconds``, short ``container_id_short``) so the dashboard
    doesn't have to redo arithmetic on every render.
    """

    tenant_id: str = Field(..., description="Owning tenant; first segment of the routing key")
    conversation_id: str = Field(..., description="Conversation identity; defines workspace lifetime")
    bundle_id: str = Field(..., description="Owning bundle for runner-scoped workspaces")
    skill_name: str = Field(..., description="Owning skill for runner-scoped workspaces")
    image_stack: str = Field(..., description="Platform image stack id")

    container_id: str = Field(..., description="Substrate container id")
    container_id_short: str = Field(
        ...,
        description="First 12 chars of container_id; what the UI displays in tables.",
    )
    image: str = Field(..., description="Resolved OCI image reference")
    mode: str = Field(..., description="Internal execution mode: 'cold' or 'warm'.")
    endpoint: Optional[str] = Field(
        None,
        description=(
            "Optional dial endpoint for stateful-mode RPC servers. "
            "None for workspace-mode containers."
        ),
    )

    created_at: float = Field(..., description="Unix epoch seconds the container was bound")
    last_active_at: float = Field(..., description="Unix epoch seconds of last successful dispatch")
    idle_seconds: float = Field(
        ...,
        description=(
            "Server-computed seconds since last_active_at; lets the UI age "
            "rows without re-deriving on every render."
        ),
    )

    worker_attribution: Optional[str] = Field(
        None,
        description=(
            "Observability-only worker_id of the worker that created the container. "
            "NOT used for routing — any worker may dispatch."      ),
    )

    script_sha256: Optional[str] = Field(
        None,
        description=(
            "Stateful-mode only: SHA-256 of the supervisor's loaded skill module bytes. "
            "Bundle redeploys with a different SHA force a fresh container."
        ),
    )
    script_logical_name: Optional[str] = Field(
        None,
        description="Stateful-mode only: bundle-relative script filename loaded by the supervisor.",
    )


class WorkspaceContainersConfig(BaseModel):
    """Snapshot of the operator-controlled knobs for the panel header.

    Surfaced so the dashboard can show "stateful mode is OFF on this stack"
    without operators having to grep env.
    """

    enabled: bool = Field(
        ..., description="MOTET_WORKSPACE_CONTAINER_ENABLED — master kill switch."
    )
    stateful_mode_enabled: bool = Field(
        ...,
        description=(
            "MOTET_WORKSPACE_STATEFUL_MODE_ENABLED — when False, ``lifetime: stateful`` "
            "declarations downgrade to ``lifetime: workspace`` (loses module-level "
            "globals; keeps ``/scratch``)."
        ),
    )
    idle_ttl_seconds: int = Field(
        ...,
        description=(
            "MOTET_WORKSPACE_CONTAINER_IDLE_TTL_SECONDS — registry entry TTL; "
            "containers with no calls in this window are reaped."
        ),
    )
    max_per_tenant: int = Field(
        ...,
        description=(
            "MOTET_WORKSPACE_CONTAINER_MAX_PER_TENANT — global ceiling on "
            "simultaneously-alive containers per tenant; reaper enforces "
            "by killing oldest-idle when at cap."
        ),
    )
    max_bytes: int = Field(
        ...,
        description="MOTET_WORKSPACE_CONTAINER_MAX_BYTES — per-container disk quota.",
    )


class WorkspaceContainersResponse(BaseModel):
    """Response shape for GET /api/v1/workspace-containers."""

    status: str = Field("success", description="``success`` on a normal read.")
    config: WorkspaceContainersConfig = Field(..., description="Operator-knob snapshot.")
    tenants: Dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Per-tenant cardinality (``tenant_id`` → container count). Sums "
            "across all returned containers; not affected by the optional "
            "``tenant_id`` filter so the panel header can show totals "
            "regardless of the table's current scope."
        ),
    )
    containers: List[WorkspaceContainerEntry] = Field(
        default_factory=list,
        description=(
            "Bindings sorted by ``last_active_at`` descending. When "
            "``tenant_id`` is set, only that tenant's containers are returned."
        ),
    )
    timestamp: float = Field(
        default_factory=time.time, description="Server-side time when this snapshot was taken."
    )


def _coerce_int(raw: Optional[str], fallback: int) -> int:
    if raw is None:
        return fallback
    try:
        v = int(str(raw).strip())
        return v if v > 0 else fallback
    except (TypeError, ValueError):
        return fallback


def _workspace_container_env(name: str) -> Optional[str]:
    return os.getenv(f"MOTET_WORKSPACE_CONTAINER_{name}")


def _read_config() -> WorkspaceContainersConfig:
    # Imports are local so a misconfigured Redis on this single endpoint
    # doesn't take the whole app's import path down at boot.
    from ....core.execution.workspace_container_manager import (
        is_workspace_container_enabled,
        is_stateful_mode_enabled,
    )
    from ....core.distributed.workspace_container_registry import (
        WorkspaceContainerRegistry,
    )

    return WorkspaceContainersConfig(
        enabled=is_workspace_container_enabled(),
        stateful_mode_enabled=is_stateful_mode_enabled(),
        idle_ttl_seconds=WorkspaceContainerRegistry.DEFAULT_IDLE_TTL_SECONDS
        if not (_workspace_container_env("IDLE_TTL_SECONDS") or "").strip()
        else _coerce_int(
            _workspace_container_env("IDLE_TTL_SECONDS"),
            WorkspaceContainerRegistry.DEFAULT_IDLE_TTL_SECONDS,
        ),
        max_per_tenant=_coerce_int(_workspace_container_env("MAX_PER_TENANT"), 100),
        max_bytes=_coerce_int(
            _workspace_container_env("MAX_BYTES"), 1073741824
        ),
    )


def _binding_to_entry(binding, *, now: float) -> WorkspaceContainerEntry:
    md = binding.metadata or {}
    return WorkspaceContainerEntry(
        tenant_id=binding.tenant_id,
        conversation_id=binding.conversation_id,
        bundle_id=binding.bundle_id,
        skill_name=binding.skill_name,
        image_stack=binding.image_stack,
        container_id=binding.container_id,
        container_id_short=binding.container_id[:12],
        image=binding.image,
        mode=binding.mode,
        endpoint=binding.endpoint,
        created_at=binding.created_at,
        last_active_at=binding.last_active_at,
        idle_seconds=max(0.0, now - binding.last_active_at),
        worker_attribution=binding.worker_attribution,
        script_sha256=md.get("script_sha256"),
        script_logical_name=md.get("script_logical_name"),
    )


@router.get(
    "",
    response_model=WorkspaceContainersResponse,
    summary="List workspace containers",
)
async def list_workspace_containers(
    tenant_id: Optional[str] = Query(
        None,
        description=(
            "Optional tenant filter. When provided, only that tenant's "
            "containers are returned in ``containers``. The ``tenants`` map "
            "in the response is unfiltered so the panel header always shows "
            "the global per-tenant distribution."
        ),
    ),
    principal: Principal = Depends(get_current_principal),
) -> JSONResponse:
    """Return the current set of bound per-workspace containers.

    Requires authentication. Callers without global tenant scope are
    limited to their own tenant even when ``tenant_id`` is omitted.
    """
    try:
        from ....core.distributed.workspace_container_registry import (
            WorkspaceContainerRegistry,
        )

        registry = WorkspaceContainerRegistry()
        all_bindings = registry.list_all()
        now = time.time()

        # Per-tenant cardinality covers the *unfiltered* set so the
        # dashboard's "tenants × containers" header is stable as the
        # operator narrows the table.
        tenants: Dict[str, int] = {}
        for b in all_bindings:
            tenants[b.tenant_id] = tenants.get(b.tenant_id, 0) + 1

        if tenant_id or not can_access_all_tenants(principal):
            authorized = require_tenant_access(principal, tenant_id)
            bindings = [b for b in all_bindings if b.tenant_id == authorized]
            if not can_access_all_tenants(principal):
                tenants = {authorized: tenants.get(authorized, 0)}
        else:
            bindings = list(all_bindings)

        bindings.sort(key=lambda b: b.last_active_at, reverse=True)
        entries = [_binding_to_entry(b, now=now) for b in bindings]

        payload = WorkspaceContainersResponse(
            config=_read_config(),
            tenants=tenants,
            containers=entries,
            timestamp=now,
        )
        # JSONResponse rather than ``return payload`` so the
        # ``status: success`` envelope shape matches the
        # ``/managers/status`` neighbor exactly (the ops UI parses them
        # identically).
        return JSONResponse(payload.model_dump())

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Failed to list workspace containers", error=str(exc), exc_info=True
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list workspace containers: {exc}",
        )
