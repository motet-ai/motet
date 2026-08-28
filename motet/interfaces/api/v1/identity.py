"""
Motet - Identity API

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Identity API for the Motet distributed framework.
    Provides REST API endpoints for retrieving authenticated principal and tenant information.

Dependencies:
    - fastapi: Web framework for REST API
    - motet.core.security: Principal extraction and authentication

Usage:
    from motet.interfaces.api.v1.identity import router
    
    # Include in FastAPI app
    app.include_router(router)

Notes:
    - Provides endpoints for current user identity and tenant context
    - Integrates with JWT, service account, and header-based authentication
    - Part of Phase 2: API Organization and URL Standardization
"""

from typing import Dict, Any, Optional, List
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
import structlog

from ..shared.auth import get_current_principal
from ....core.types import Principal

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/identity", tags=["identity"])


class PrincipalResponse(BaseModel):
    """Response model for principal information."""
    id: Optional[str] = Field(None, description="Principal ID", json_schema_extra={"example": "user-123"})
    roles: List[str] = Field(default_factory=list, description="List of principal roles", json_schema_extra={"example": ["admin", "user"]})
    tenant_id: Optional[str] = Field(None, description="Tenant ID", json_schema_extra={"example": "acme-corp"})
    display_name: Optional[str] = Field(None, description="Display name", json_schema_extra={"example": "John Doe"})
    email: Optional[str] = Field(None, description="Email address", json_schema_extra={"example": "john@example.com"})
    organization_name: Optional[str] = Field(None, description="Organization/tenant display name", json_schema_extra={"example": "Demo Org"})
    claims: Optional[Dict[str, Any]] = Field(None, description="Full JWT claims dictionary", json_schema_extra={"example": {"name": "John Doe", "email": "john@example.com"}})


class TenantResponse(BaseModel):
    """Response model for tenant information."""
    tenant_id: Optional[str] = Field(None, description="Current tenant ID", json_schema_extra={"example": "acme-corp"})


@router.get(
    "/me",
    summary="Get current principal information",
    description="Get information about the currently authenticated principal",
    response_model=PrincipalResponse,
    response_description="Principal information including ID, roles, and tenant"
)
async def get_current_principal_info(
    principal: Principal = Depends(get_current_principal)
) -> PrincipalResponse:
    """
    Get current principal information.
    
    Returns information about the authenticated principal including:
    - Principal ID
    - Roles
    - Tenant ID
    - Display name (extracted from JWT claims: 'name', 'given_name'/'family_name', or 'preferred_username')
    - Email (extracted from JWT claims)
    - Organization name (extracted from JWT 'organization' claim, keyed by tenant_id)
    - Full JWT claims dictionary (for access to all claim data)
    
    The principal is extracted from JWT tokens, service account tokens,
    or development headers (if enabled).
    
    Display name extraction priority:
    1. Principal.display_name (if already set)
    2. JWT claim 'name'
    3. Constructed from 'given_name' + 'family_name'
    4. JWT claim 'preferred_username'
    
    Args:
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        PrincipalResponse with principal information including extracted display_name and full claims
        
    Raises:
        HTTPException: 401 if authentication fails
    """
    try:
        # Extract display_name from claims if not already set on Principal
        display_name = principal.display_name
        if not display_name and principal.claims:
            # Try 'name' claim first (most common)
            display_name = principal.claims.get("name")
            # Fallback to constructing from given_name + family_name
            if not display_name:
                given_name = principal.claims.get("given_name", "")
                family_name = principal.claims.get("family_name", "")
                if given_name or family_name:
                    display_name = f"{given_name} {family_name}".strip()
            # Fallback to preferred_username if nothing else
            if not display_name:
                display_name = principal.claims.get("preferred_username")
        
        # Extract email from claims if not already set on Principal
        email = principal.email
        if not email and principal.claims:
            email = principal.claims.get("email")
        
        # Extract organization name from claims
        # Organization claim structure: { "<tenant_id>": { "displayName": ["Org Name"], ... } }
        organization_name = None
        if principal.claims and principal.tenant_id:
            org_claim = principal.claims.get("organization")
            if org_claim and isinstance(org_claim, dict):
                # Look up organization data by tenant_id
                tenant_org_data = org_claim.get(principal.tenant_id)
                if tenant_org_data and isinstance(tenant_org_data, dict):
                    # displayName is typically an array in Keycloak
                    display_name_array = tenant_org_data.get("displayName")
                    if display_name_array:
                        if isinstance(display_name_array, list) and len(display_name_array) > 0:
                            organization_name = display_name_array[0]
                        elif isinstance(display_name_array, str):
                            organization_name = display_name_array
        
        return PrincipalResponse(
            id=principal.id,
            roles=list(principal.roles or []),
            tenant_id=principal.tenant_id,
            display_name=display_name,
            email=email,
            organization_name=organization_name,
            claims=principal.claims if principal.claims else None
        )
    except Exception as e:
        logger.error("Failed to get principal info", error=str(e), exc_info=True)
        # Return minimal response on error
        return PrincipalResponse(
            id=principal.id if principal else None,
            roles=list(principal.roles or []) if principal else [],
            tenant_id=principal.tenant_id if principal else None,
            display_name=None,
            email=None,
            organization_name=None,
            claims=None,
        )


@router.get(
    "/tenant",
    summary="Get current tenant information",
    description="Get the current tenant ID from the authenticated principal",
    response_model=TenantResponse,
    response_description="Current tenant ID"
)
async def get_current_tenant(
    principal: Principal = Depends(get_current_principal)
) -> TenantResponse:
    """
    Get current tenant information.
    
    Returns the tenant ID associated with the authenticated principal.
    Useful for multi-tenant applications to determine the current tenant context.
    
    Args:
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        TenantResponse with tenant_id
        
    Raises:
        HTTPException: 401 if authentication fails
    """
    return TenantResponse(tenant_id=principal.tenant_id)

