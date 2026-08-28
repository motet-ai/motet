"""
Motet - Tenants / Motets Catalog API

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    REST API for the operator-managed tenant and Motet (environment) catalog.
    Powers manage-app scope selectors and CLI catalog management.

Dependencies:
    - fastapi: Router and HTTP exceptions
    - motet.core.tenancy.tenant_registry: TenantRegistry
    - motet.interfaces.api.shared.auth: get_current_principal,
      can_access_all_tenants, require_can_access_all_tenants

Usage:
    from motet.interfaces.api.v1.tenants import router

Notes:
    - Mutations require admin (``admin`` / ``motet-admin``) or ops_dashboard principal
    - Non-admins may list/get only their own tenant and its Motets
    - Catalog is independent of JWT claim remapping
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
import structlog

from ..shared.auth import (
    can_access_all_tenants,
    get_current_principal,
    require_can_access_all_tenants,
)
from ....core.tenancy.tenant_registry import (
    MotetConflictError,
    MotetNotFoundError,
    MotetRecord,
    TenantConflictError,
    TenantNotFoundError,
    TenantRecord,
    TenantRegistry,
    TenantValidationError,
    validate_catalog_id,
)
from ....core.types import Principal

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/tenants", tags=["tenants"])


def _registry() -> TenantRegistry:
    return TenantRegistry()


def _require_admin(principal: Principal) -> None:
    require_can_access_all_tenants(
        principal, detail="Admin role required for tenant catalog mutations"
    )


def _assert_tenant_readable(principal: Principal, tenant_id: str) -> None:
    if can_access_all_tenants(principal):
        return
    caller = (principal.tenant_id or "").strip().lower()
    target = (tenant_id or "").strip().lower()
    if not caller or caller != target:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this tenant",
        )


class TenantCreateRequest(BaseModel):
    """Create a tenant in the catalog."""

    id: str = Field(
        ...,
        description="Tenant id slug (lowercase)",
        json_schema_extra={"example": "acme"},
    )
    name: Optional[str] = Field(
        None,
        description="Display name (defaults to id)",
        json_schema_extra={"example": "Acme Corp"},
    )
    description: Optional[str] = Field(
        None,
        description="Optional description",
        json_schema_extra={"example": "Acme production organization"},
    )
    status: str = Field(
        default="active",
        description="active or disabled",
        json_schema_extra={"example": "active"},
    )


class TenantUpdateRequest(BaseModel):
    """Update mutable tenant fields."""

    name: Optional[str] = Field(
        None,
        description="Display name",
        json_schema_extra={"example": "Acme Corporation"},
    )
    description: Optional[str] = Field(
        None,
        description="Optional description",
        json_schema_extra={"example": "Updated description"},
    )
    status: Optional[str] = Field(
        None,
        description="active or disabled",
        json_schema_extra={"example": "disabled"},
    )


class MotetCreateRequest(BaseModel):
    """Create a Motet (environment) under a tenant."""

    id: str = Field(
        ...,
        description="Motet id slug (lowercase)",
        json_schema_extra={"example": "prod"},
    )
    name: Optional[str] = Field(
        None,
        description="Display name (defaults to id)",
        json_schema_extra={"example": "Production"},
    )
    description: Optional[str] = Field(
        None,
        description="Optional description",
        json_schema_extra={"example": "Production environment"},
    )
    status: str = Field(
        default="active",
        description="active or disabled",
        json_schema_extra={"example": "active"},
    )


class MotetUpdateRequest(BaseModel):
    """Update mutable Motet fields."""

    name: Optional[str] = Field(
        None,
        description="Display name",
        json_schema_extra={"example": "Production US"},
    )
    description: Optional[str] = Field(
        None,
        description="Optional description",
        json_schema_extra={"example": "US region production"},
    )
    status: Optional[str] = Field(
        None,
        description="active or disabled",
        json_schema_extra={"example": "active"},
    )


class MotetResponse(BaseModel):
    """Motet catalog entry."""

    id: str = Field(..., description="Motet id", json_schema_extra={"example": "prod"})
    tenant_id: str = Field(
        ..., description="Parent tenant id", json_schema_extra={"example": "acme"}
    )
    name: str = Field(
        ..., description="Display name", json_schema_extra={"example": "Production"}
    )
    status: str = Field(
        ..., description="active or disabled", json_schema_extra={"example": "active"}
    )
    created_at: str = Field(
        ...,
        description="ISO creation timestamp",
        json_schema_extra={"example": "2026-07-27T12:00:00+00:00"},
    )
    updated_at: str = Field(
        ...,
        description="ISO update timestamp",
        json_schema_extra={"example": "2026-07-27T12:00:00+00:00"},
    )
    description: Optional[str] = Field(
        None,
        description="Optional description",
        json_schema_extra={"example": "Production environment"},
    )


class TenantResponse(BaseModel):
    """Tenant catalog entry."""

    id: str = Field(..., description="Tenant id", json_schema_extra={"example": "acme"})
    name: str = Field(
        ..., description="Display name", json_schema_extra={"example": "Acme Corp"}
    )
    status: str = Field(
        ..., description="active or disabled", json_schema_extra={"example": "active"}
    )
    created_at: str = Field(
        ...,
        description="ISO creation timestamp",
        json_schema_extra={"example": "2026-07-27T12:00:00+00:00"},
    )
    updated_at: str = Field(
        ...,
        description="ISO update timestamp",
        json_schema_extra={"example": "2026-07-27T12:00:00+00:00"},
    )
    description: Optional[str] = Field(
        None,
        description="Optional description",
        json_schema_extra={"example": "Acme production organization"},
    )
    motets: Optional[List[MotetResponse]] = Field(
        None,
        description="Nested Motets when include_motets=true",
    )


class TenantListResponse(BaseModel):
    """List of tenants."""

    tenants: List[TenantResponse] = Field(
        ..., description="Tenant catalog entries"
    )
    can_access_all_tenants: bool = Field(
        ...,
        description="True when the caller may see the full catalog (admin / global scope)",
    )


class MotetListResponse(BaseModel):
    """List of Motets for a tenant."""

    motets: List[MotetResponse] = Field(..., description="Motet catalog entries")


class EnsureDefaultsResponse(BaseModel):
    """Result of seeding default catalog entries."""

    created: Dict[str, int] = Field(
        ...,
        description="Counts of newly created tenants and motets",
        json_schema_extra={"example": {"tenants": 2, "motets": 3}},
    )


def _tenant_response(record: TenantRecord, *, include_motets: bool = False) -> TenantResponse:
    data = record.to_dict(include_motets=include_motets or record.motets is not None)
    motets_raw = data.pop("motets", None)
    motets = (
        [MotetResponse(**m) for m in motets_raw] if motets_raw is not None else None
    )
    return TenantResponse(**data, motets=motets)


def _motet_response(record: MotetRecord) -> MotetResponse:
    return MotetResponse(**record.to_dict())


def _http_from_registry(exc: Exception) -> HTTPException:
    if isinstance(exc, TenantNotFoundError) or isinstance(exc, MotetNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, TenantConflictError) or isinstance(exc, MotetConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, TenantValidationError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"Tenant catalog error: {exc}",
    )


@router.get(
    "",
    summary="List tenants",
    description="List tenants visible to the caller. Admins see all; others see only their tenant.",
    response_model=TenantListResponse,
    responses={
        200: {"description": "Tenant list"},
        401: {"description": "Authentication required"},
    },
)
async def list_tenants(
    include_motets: bool = Query(
        False,
        description="When true, nest Motets under each tenant (for scope selectors)",
    ),
    status_filter: Optional[str] = Query(
        None,
        alias="status",
        description="Filter by status (active or disabled)",
    ),
    principal: Principal = Depends(get_current_principal),
) -> TenantListResponse:
    """List catalog tenants visible to the authenticated principal."""
    registry = _registry()
    can_all = can_access_all_tenants(principal)
    tenant_filter: Optional[Set[str]] = None
    if not can_all:
        if not principal.tenant_id:
            return TenantListResponse(tenants=[], can_access_all_tenants=False)
        try:
            tenant_filter = {
                validate_catalog_id(principal.tenant_id, field_name="tenant_id")
            }
        except TenantValidationError:
            return TenantListResponse(tenants=[], can_access_all_tenants=False)

    try:
        records = registry.list_tenants(
            include_motets=include_motets,
            status=status_filter,
            tenant_ids=tenant_filter,
        )
    except TenantValidationError as exc:
        raise _http_from_registry(exc) from exc

    return TenantListResponse(
        tenants=[
            _tenant_response(r, include_motets=include_motets) for r in records
        ],
        can_access_all_tenants=can_all,
    )


@router.post(
    "",
    summary="Create tenant",
    description="Create a tenant catalog entry. Requires admin.",
    response_model=TenantResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Tenant created"},
        400: {"description": "Invalid payload"},
        403: {"description": "Admin required"},
        409: {"description": "Tenant already exists"},
    },
)
async def create_tenant(
    body: TenantCreateRequest,
    principal: Principal = Depends(get_current_principal),
) -> TenantResponse:
    """Create a tenant in the catalog."""
    _require_admin(principal)
    registry = _registry()
    try:
        record = registry.create_tenant(
            tenant_id=body.id,
            name=body.name,
            description=body.description,
            status=body.status,
        )
    except (TenantConflictError, TenantValidationError) as exc:
        raise _http_from_registry(exc) from exc
    return _tenant_response(record)


@router.post(
    "/ensure-defaults",
    summary="Seed default tenants and Motets",
    description="Idempotently create default/demo catalog entries. Requires admin.",
    response_model=EnsureDefaultsResponse,
    responses={
        200: {"description": "Defaults ensured"},
        403: {"description": "Admin required"},
    },
)
async def ensure_defaults(
    principal: Principal = Depends(get_current_principal),
) -> EnsureDefaultsResponse:
    """Seed default catalog entries used by local/dev manage-app."""
    _require_admin(principal)
    created = _registry().ensure_defaults()
    return EnsureDefaultsResponse(created=created)


@router.get(
    "/{tenant_id}",
    summary="Get tenant",
    description="Get a tenant by id. Non-admins may only read their own tenant.",
    response_model=TenantResponse,
    responses={
        200: {"description": "Tenant"},
        403: {"description": "Not authorized"},
        404: {"description": "Not found"},
    },
)
async def get_tenant(
    tenant_id: str,
    include_motets: bool = Query(
        False, description="When true, include nested Motets"
    ),
    principal: Principal = Depends(get_current_principal),
) -> TenantResponse:
    """Get one tenant from the catalog."""
    _assert_tenant_readable(principal, tenant_id)
    try:
        record = _registry().get_tenant(tenant_id, include_motets=include_motets)
    except (TenantNotFoundError, TenantValidationError) as exc:
        raise _http_from_registry(exc) from exc
    return _tenant_response(record, include_motets=include_motets)


@router.patch(
    "/{tenant_id}",
    summary="Update tenant",
    description="Update tenant display fields or status. Requires admin.",
    response_model=TenantResponse,
    responses={
        200: {"description": "Tenant updated"},
        400: {"description": "Invalid payload"},
        403: {"description": "Admin required"},
        404: {"description": "Not found"},
    },
)
async def update_tenant(
    tenant_id: str,
    body: TenantUpdateRequest,
    principal: Principal = Depends(get_current_principal),
) -> TenantResponse:
    """Update a tenant catalog entry."""
    _require_admin(principal)
    try:
        record = _registry().update_tenant(
            tenant_id,
            name=body.name,
            description=body.description,
            status=body.status,
        )
    except (TenantNotFoundError, TenantValidationError) as exc:
        raise _http_from_registry(exc) from exc
    return _tenant_response(record)


@router.delete(
    "/{tenant_id}",
    summary="Delete tenant",
    description="Delete a tenant. Refuses when Motets remain unless force=true. Requires admin.",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={
        204: {"description": "Deleted"},
        400: {"description": "Motets still present without force"},
        403: {"description": "Admin required"},
        404: {"description": "Not found"},
    },
)
async def delete_tenant(
    tenant_id: str,
    force: bool = Query(
        False,
        description="When true, also delete all Motets under the tenant",
    ),
    principal: Principal = Depends(get_current_principal),
) -> Response:
    """Delete a tenant from the catalog."""
    _require_admin(principal)
    try:
        _registry().delete_tenant(tenant_id, force=force)
    except (TenantNotFoundError, TenantValidationError) as exc:
        raise _http_from_registry(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{tenant_id}/motets",
    summary="List Motets for a tenant",
    description="List Motets (environments) registered under a tenant.",
    response_model=MotetListResponse,
    responses={
        200: {"description": "Motet list"},
        403: {"description": "Not authorized"},
        404: {"description": "Tenant not found"},
    },
)
async def list_motets(
    tenant_id: str,
    status_filter: Optional[str] = Query(
        None,
        alias="status",
        description="Filter by status (active or disabled)",
    ),
    principal: Principal = Depends(get_current_principal),
) -> MotetListResponse:
    """List Motets for one tenant."""
    _assert_tenant_readable(principal, tenant_id)
    try:
        records = _registry().list_motets(tenant_id, status=status_filter)
    except (TenantNotFoundError, TenantValidationError) as exc:
        raise _http_from_registry(exc) from exc
    return MotetListResponse(motets=[_motet_response(r) for r in records])


@router.post(
    "/{tenant_id}/motets",
    summary="Create Motet",
    description="Create a Motet under a tenant. Requires admin.",
    response_model=MotetResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Motet created"},
        400: {"description": "Invalid payload"},
        403: {"description": "Admin required"},
        404: {"description": "Tenant not found"},
        409: {"description": "Motet already exists"},
    },
)
async def create_motet(
    tenant_id: str,
    body: MotetCreateRequest,
    principal: Principal = Depends(get_current_principal),
) -> MotetResponse:
    """Create a Motet under a tenant."""
    _require_admin(principal)
    try:
        record = _registry().create_motet(
            tenant_id=tenant_id,
            motet_id=body.id,
            name=body.name,
            description=body.description,
            status=body.status,
        )
    except (
        TenantNotFoundError,
        MotetConflictError,
        TenantValidationError,
    ) as exc:
        raise _http_from_registry(exc) from exc
    return _motet_response(record)


@router.get(
    "/{tenant_id}/motets/{motet_id}",
    summary="Get Motet",
    description="Get one Motet under a tenant.",
    response_model=MotetResponse,
    responses={
        200: {"description": "Motet"},
        403: {"description": "Not authorized"},
        404: {"description": "Not found"},
    },
)
async def get_motet(
    tenant_id: str,
    motet_id: str,
    principal: Principal = Depends(get_current_principal),
) -> MotetResponse:
    """Get one Motet from the catalog."""
    _assert_tenant_readable(principal, tenant_id)
    try:
        record = _registry().get_motet(tenant_id, motet_id)
    except (TenantNotFoundError, MotetNotFoundError, TenantValidationError) as exc:
        raise _http_from_registry(exc) from exc
    return _motet_response(record)


@router.patch(
    "/{tenant_id}/motets/{motet_id}",
    summary="Update Motet",
    description="Update Motet display fields or status. Requires admin.",
    response_model=MotetResponse,
    responses={
        200: {"description": "Motet updated"},
        400: {"description": "Invalid payload"},
        403: {"description": "Admin required"},
        404: {"description": "Not found"},
    },
)
async def update_motet(
    tenant_id: str,
    motet_id: str,
    body: MotetUpdateRequest,
    principal: Principal = Depends(get_current_principal),
) -> MotetResponse:
    """Update a Motet catalog entry."""
    _require_admin(principal)
    try:
        record = _registry().update_motet(
            tenant_id,
            motet_id,
            name=body.name,
            description=body.description,
            status=body.status,
        )
    except (TenantNotFoundError, MotetNotFoundError, TenantValidationError) as exc:
        raise _http_from_registry(exc) from exc
    return _motet_response(record)


@router.delete(
    "/{tenant_id}/motets/{motet_id}",
    summary="Delete Motet",
    description="Delete a Motet from the catalog. Requires admin.",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={
        204: {"description": "Deleted"},
        403: {"description": "Admin required"},
        404: {"description": "Not found"},
    },
)
async def delete_motet(
    tenant_id: str,
    motet_id: str,
    principal: Principal = Depends(get_current_principal),
) -> Response:
    """Delete a Motet from the catalog."""
    _require_admin(principal)
    try:
        _registry().delete_motet(tenant_id, motet_id)
    except (TenantNotFoundError, MotetNotFoundError, TenantValidationError) as exc:
        raise _http_from_registry(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
