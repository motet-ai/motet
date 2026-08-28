"""
Motet - Surfaces Catalog API

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    REST API for the conversation surfaces catalog. Powers Chat
    Explorer surface pickers and the manage-app Surfaces page. Surfaces are
    created explicitly (POST); chat requests do not auto-create catalog entries.

Dependencies:
    - fastapi: Router and HTTP exceptions
    - motet.core.surfaces: SurfaceRegistry
    - motet.interfaces.api.shared.auth: get_current_principal,
      require_can_access_all_tenants

Usage:
    from motet.interfaces.api.v1.surfaces import router

Notes:
    - List/get require authentication
    - Mutations require admin (``admin`` / ``motet-admin``) or ops_dashboard principal
    - Builtin surfaces cannot be deleted
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
import structlog

from ..shared.auth import (
    can_access_all_tenants,
    get_current_principal,
    require_can_access_all_tenants,
)
from ....core.surfaces import (
    SurfaceConflictError,
    SurfaceNotFoundError,
    SurfaceRecord,
    SurfaceRegistry,
    SurfaceRegistryError,
    SurfaceValidationError,
)
from ....core.types import Principal

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/surfaces", tags=["surfaces"])


def _registry() -> SurfaceRegistry:
    return SurfaceRegistry()


def _require_admin(principal: Principal) -> None:
    require_can_access_all_tenants(
        principal, detail="Admin role required for surface catalog mutations"
    )


class SurfaceCreateRequest(BaseModel):
    """Create a surface in the catalog."""

    id: str = Field(
        ...,
        description=(
            "Stable surface id slug (lowercase; letters, digits, underscores, "
            "hyphens; 2-63 chars)"
        ),
        json_schema_extra={"example": "partner_portal"},
    )
    display_name: Optional[str] = Field(
        None,
        description="Human-readable name (defaults to id)",
        json_schema_extra={"example": "Partner Portal"},
    )
    description: Optional[str] = Field(
        None,
        description="Optional description",
        json_schema_extra={"example": "Partner-facing chat channel"},
    )


class SurfaceUpdateRequest(BaseModel):
    """Update mutable surface fields."""

    display_name: Optional[str] = Field(
        None,
        description="Human-readable name",
        json_schema_extra={"example": "Partner Portal"},
    )
    description: Optional[str] = Field(
        None,
        description="Optional description",
        json_schema_extra={"example": "Updated description"},
    )


class SurfaceResponse(BaseModel):
    """Surface catalog entry."""

    id: str = Field(..., description="Surface id", json_schema_extra={"example": "demo_chat"})
    display_name: str = Field(
        ...,
        description="Human-readable name",
        json_schema_extra={"example": "Demo Chat"},
    )
    description: Optional[str] = Field(
        None,
        description="Optional description",
        json_schema_extra={"example": "Chat Explorer default surface"},
    )
    builtin: bool = Field(
        ...,
        description="True for Motet-seeded surfaces that cannot be deleted",
        json_schema_extra={"example": True},
    )
    created_at: str = Field(
        ...,
        description="ISO creation timestamp",
        json_schema_extra={"example": "2026-08-07T12:00:00+00:00"},
    )
    updated_at: str = Field(
        ...,
        description="ISO update timestamp",
        json_schema_extra={"example": "2026-08-07T12:00:00+00:00"},
    )
    created_by: Optional[str] = Field(
        None,
        description="Principal or system that created the entry",
        json_schema_extra={"example": "system"},
    )


class SurfaceListResponse(BaseModel):
    """List of surfaces."""

    surfaces: List[SurfaceResponse] = Field(
        default_factory=list,
        description="Catalog entries sorted by id",
    )
    total: int = Field(
        ...,
        description="Number of surfaces returned",
        json_schema_extra={"example": 4},
    )
    can_manage: bool = Field(
        ...,
        description="True when the caller may create/update/delete surfaces",
    )


def _surface_response(record: SurfaceRecord) -> SurfaceResponse:
    return SurfaceResponse(**record.to_dict())


def _http_from_registry(exc: Exception) -> HTTPException:
    if isinstance(exc, SurfaceNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, SurfaceConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, SurfaceValidationError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if isinstance(exc, SurfaceRegistryError):
        return HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Surface catalog error: {exc}",
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"Surface catalog error: {exc}",
    )


@router.get(
    "",
    summary="List surfaces",
    description="List conversation surfaces in the catalog. Seeds builtins on first read.",
    response_model=SurfaceListResponse,
    responses={
        200: {"description": "Surface catalog list"},
        401: {"description": "Authentication required"},
    },
)
async def list_surfaces(
    principal: Principal = Depends(get_current_principal),
) -> SurfaceListResponse:
    """Return all catalog surfaces (builtins ensured)."""
    try:
        reg = _registry()
        reg.ensure_builtins()
        records = reg.list_surfaces()
    except Exception as e:
        logger.error("surface_list_failed", error=str(e), exc_info=True)
        raise _http_from_registry(e) from e
    return SurfaceListResponse(
        surfaces=[_surface_response(r) for r in records],
        total=len(records),
        can_manage=can_access_all_tenants(principal),
    )


@router.post(
    "",
    summary="Create surface",
    description="Register a new surface in the catalog (admin).",
    response_model=SurfaceResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Surface created"},
        400: {"description": "Invalid surface id"},
        403: {"description": "Admin required"},
        409: {"description": "Surface already exists"},
    },
)
async def create_surface(
    body: SurfaceCreateRequest,
    principal: Principal = Depends(get_current_principal),
) -> SurfaceResponse:
    """Create a catalog surface explicitly (no auto-create on chat)."""
    _require_admin(principal)
    try:
        reg = _registry()
        reg.ensure_builtins()
        record = reg.create(
            surface_id=body.id,
            display_name=body.display_name,
            description=body.description,
            created_by=getattr(principal, "id", None) or None,
        )
    except Exception as e:
        logger.error("surface_create_failed", error=str(e), exc_info=True)
        raise _http_from_registry(e) from e
    return _surface_response(record)


@router.get(
    "/{surface_id}",
    summary="Get surface",
    description="Get a single surface catalog entry.",
    response_model=SurfaceResponse,
    responses={
        200: {"description": "Surface found"},
        404: {"description": "Surface not found"},
    },
)
async def get_surface(
    surface_id: str,
    _principal: Principal = Depends(get_current_principal),
) -> SurfaceResponse:
    """Return one surface by id."""
    try:
        reg = _registry()
        reg.ensure_builtins()
        record = reg.get(surface_id)
    except Exception as e:
        raise _http_from_registry(e) from e
    return _surface_response(record)


@router.patch(
    "/{surface_id}",
    summary="Update surface",
    description="Update display name / description (admin).",
    response_model=SurfaceResponse,
    responses={
        200: {"description": "Surface updated"},
        403: {"description": "Admin required"},
        404: {"description": "Surface not found"},
    },
)
async def update_surface(
    surface_id: str,
    body: SurfaceUpdateRequest,
    principal: Principal = Depends(get_current_principal),
) -> SurfaceResponse:
    """Update mutable surface metadata."""
    _require_admin(principal)
    try:
        record = _registry().update(
            surface_id,
            display_name=body.display_name,
            description=body.description,
        )
    except Exception as e:
        raise _http_from_registry(e) from e
    return _surface_response(record)


@router.delete(
    "/{surface_id}",
    summary="Delete surface",
    description="Delete a non-builtin surface (admin).",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "Surface deleted"},
        400: {"description": "Builtin surfaces cannot be deleted"},
        403: {"description": "Admin required"},
        404: {"description": "Surface not found"},
    },
)
async def delete_surface(
    surface_id: str,
    principal: Principal = Depends(get_current_principal),
) -> Response:
    """Delete a catalog surface (builtins rejected)."""
    _require_admin(principal)
    try:
        _registry().delete(surface_id)
    except Exception as e:
        raise _http_from_registry(e) from e
    return Response(status_code=status.HTTP_204_NO_CONTENT)
