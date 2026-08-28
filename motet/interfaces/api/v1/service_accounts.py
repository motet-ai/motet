"""
Motet - Service Accounts API

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Service Accounts API for the Motet distributed framework.
    Provides REST API endpoints for creating, listing, and revoking service account tokens.
    Service accounts are long-lived tokens for CLI/automation use cases, including
    OpenAI-compatible facade policy (mode, allowlist, force_thinking).

Dependencies:
    - fastapi: Web framework for REST API
    - motet.core.security.service_accounts: ServiceAccountManager
    - motet.core.distributed.redis_manager: Redis client

Usage:
    from motet.interfaces.api.v1.service_accounts import router
    
    # Include in FastAPI app
    app.include_router(router)

Notes:
    - Service accounts are stored in Redis (not in vault)
    - Tokens are prefixed with "sa_" for identification
    - Part of Week 3-4: Demo Chat JWT + API Updates
    - tenant_id / motet_id on create are names, not permission (issue #214).
      Foreign values 403 unless can_access_all_tenants. List defaults to the
      caller's tenant; revoke checks the token's tenant.
"""

from typing import Dict, List, Optional, Any
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, Field
import structlog
import os

from ..shared.auth import get_current_principal, require_motet_access, require_tenant_access
from ....core.types import Principal
from ....core.distributed.redis_manager import get_sync_redis_client
from ....core.security.service_accounts import ServiceAccountManager, ServiceAccountToken
from ....core.config import Config

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/service-accounts", tags=["service-accounts"])


def get_service_account_manager() -> ServiceAccountManager:
    """Get ServiceAccountManager instance."""
    redis_client = get_sync_redis_client("service_accounts")
    return ServiceAccountManager(redis_client)


class CreateServiceAccountRequest(BaseModel):
    """Request model for creating a service account."""
    name: str = Field(..., description="Service account name", json_schema_extra={"example": "ci-pipeline"})
    tenant_id: Optional[str] = Field(
        None,
        description=(
            "Tenant ID. Omitted uses the authenticated principal's tenant. "
            "A different tenant requires global tenant access; otherwise 403."
        ),
        json_schema_extra={"example": "acme-corp"},
    )
    motet_id: Optional[str] = Field(
        None,
        description=(
            "Motet/environment ID. Omitted uses the authenticated principal's motet. "
            "A different motet requires global tenant access; otherwise 403."
        ),
        json_schema_extra={"example": "production"},
    )
    roles: List[str] = Field(..., description="List of roles", json_schema_extra={"example": ["admin", "ci"]})
    expires_days: int = Field(default=365, description="Expiration in days", json_schema_extra={"example": 365})
    facade_mode: Optional[str] = Field(
        None,
        description=(
            "OpenAI-compatible facade mode bound to this token: passthrough, hosted_tools, "
            "or agent. Omit to use the configured default."
        ),
        json_schema_extra={"example": "passthrough"},
    )
    allowed_models: List[str] = Field(
        default_factory=list,
        description=(
            "OpenAI-compatible facade model allowlist as 'provider/model' ids. Empty falls back "
            "to the configured default, which denies all models unless set."
        ),
        json_schema_extra={"example": ["openai/gpt-4o-mini", "anthropic/claude-sonnet-4"]},
    )
    force_thinking: Optional[bool] = Field(
        None,
        description=(
            "When true, enable Motet thinking for CAP_REASONING models even without client "
            "reasoning opt-in. Omit to use MOTET_OPENAI_COMPAT_FORCE_THINKING."
        ),
        json_schema_extra={"example": True},
    )
    force_thinking_effort: Optional[str] = Field(
        None,
        description=(
            "Default reasoning effort when force_thinking applies and the client omits effort. "
            "Omit to use MOTET_OPENAI_COMPAT_FORCE_THINKING_EFFORT."
        ),
        json_schema_extra={"example": "medium"},
    )
    agent_id: Optional[str] = Field(
        None,
        description=(
            "Default Motet agent id for facade agent mode when the client omits "
            "motet_agent_id (e.g. cursor.backend). Omit to use "
            "MOTET_OPENAI_COMPAT_DEFAULT_AGENT_ID."
        ),
        json_schema_extra={"example": "cursor.backend"},
    )


class CreateServiceAccountResponse(BaseModel):
    """Response model for service account creation."""
    token: str = Field(..., description="Service account token (save this - it won't be shown again)", json_schema_extra={"example": "sa_20251122_abc123_ci-pipeline"})
    name: str = Field(..., description="Service account name", json_schema_extra={"example": "ci-pipeline"})
    principal_id: str = Field(..., description="Principal ID", json_schema_extra={"example": "service-account:ci-pipeline"})
    tenant_id: Optional[str] = Field(None, description="Tenant ID", json_schema_extra={"example": "acme-corp"})
    motet_id: Optional[str] = Field(None, description="Motet/environment ID", json_schema_extra={"example": "production"})
    roles: List[str] = Field(..., description="List of roles", json_schema_extra={"example": ["admin", "ci"]})
    created_at: str = Field(..., description="Creation timestamp", json_schema_extra={"example": "2025-11-24T10:00:00Z"})
    expires_at: str = Field(..., description="Expiration timestamp", json_schema_extra={"example": "2026-11-24T10:00:00Z"})


class ServiceAccountInfo(BaseModel):
    """Service account information model."""
    id: str = Field(..., description="Token ID", json_schema_extra={"example": "sa_20251122_abc123_ci-pipeline"})
    name: str = Field(..., description="Service account name", json_schema_extra={"example": "ci-pipeline"})
    principal_id: str = Field(..., description="Principal ID", json_schema_extra={"example": "service-account:ci-pipeline"})
    tenant_id: Optional[str] = Field(None, description="Tenant ID", json_schema_extra={"example": "acme-corp"})
    motet_id: Optional[str] = Field(None, description="Motet/environment ID", json_schema_extra={"example": "production"})
    roles: List[str] = Field(..., description="List of roles", json_schema_extra={"example": ["admin", "ci"]})
    created_at: str = Field(..., description="Creation timestamp", json_schema_extra={"example": "2025-11-24T10:00:00Z"})
    expires_at: str = Field(..., description="Expiration timestamp", json_schema_extra={"example": "2026-11-24T10:00:00Z"})
    last_used_at: Optional[str] = Field(None, description="Last usage timestamp", json_schema_extra={"example": "2025-11-24T12:00:00Z"})
    revoked_at: Optional[str] = Field(None, description="Revocation timestamp if revoked", json_schema_extra={"example": None})
    facade_mode: Optional[str] = Field(
        None,
        description="OpenAI-compatible facade mode bound to this token",
        json_schema_extra={"example": "passthrough"},
    )
    allowed_models: List[str] = Field(
        default_factory=list,
        description="OpenAI-compatible facade model allowlist",
        json_schema_extra={"example": ["openai/gpt-4o-mini"]},
    )
    force_thinking: Optional[bool] = Field(
        None,
        description="Facade force-thinking policy bound to this token",
        json_schema_extra={"example": True},
    )
    force_thinking_effort: Optional[str] = Field(
        None,
        description="Default effort when force_thinking applies",
        json_schema_extra={"example": "medium"},
    )
    agent_id: Optional[str] = Field(
        None,
        description="Default Motet agent id for facade agent mode",
        json_schema_extra={"example": "cursor.backend"},
    )


class ListServiceAccountsResponse(BaseModel):
    """Response model for listing service accounts."""
    service_accounts: List[ServiceAccountInfo] = Field(..., description="List of service accounts")


@router.post(
    "",
    summary="Create service account",
    description="Create a new service account token for automation/CLI use",
    response_model=CreateServiceAccountResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Service account created successfully"},
        400: {"description": "Invalid request"},
        401: {"description": "Authentication required"},
        403: {"description": "Foreign tenant_id or motet_id without global scope"},
        500: {"description": "Internal server error"}
    }
)
async def create_service_account(
    request: CreateServiceAccountRequest,
    principal: Principal = Depends(get_current_principal)
) -> CreateServiceAccountResponse:
    """
    Create a new service account token.
    
    Service accounts are long-lived tokens for CLI/automation use cases.
    They are self-managed in Redis and do not require an external identity provider.
    
    **Important:** The token is only returned once. Save it securely.
    
    Args:
        request: Service account creation request
        principal: Authenticated principal (from JWT or service account)
        
    Returns:
        Service account token and metadata
        
    Raises:
        HTTPException: If creation fails
    """
    try:
        sa_manager = get_service_account_manager()
        cfg = Config()

        tenant_id = require_tenant_access(principal, request.tenant_id)
        motet_fallback = (
            principal.motet_id
            or getattr(cfg, "motet_id", None)
            or os.getenv("MOTET_MOTET_ID", "default")
        )
        motet_id = require_motet_access(
            principal, request.motet_id, fallback=motet_fallback
        )
        
        if request.facade_mode:
            from ....core.security.facade_policy import FACADE_MODE_VALUES

            if request.facade_mode not in FACADE_MODE_VALUES:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"facade_mode must be one of {sorted(FACADE_MODE_VALUES)}",
                )

        token = sa_manager.create_service_account(
            name=request.name,
            tenant_id=tenant_id,
            motet_id=motet_id,
            roles=request.roles,
            created_by=principal.id,
            expires_days=request.expires_days,
            facade_mode=request.facade_mode,
            allowed_models=request.allowed_models,
            force_thinking=request.force_thinking,
            force_thinking_effort=request.force_thinking_effort,
            agent_id=request.agent_id,
        )
        
        # Retrieve the created token to get full metadata
        token_meta = sa_manager.verify_service_account(token)
        if not token_meta:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve created service account"
            )
        
        logger.info(
            "Service account created via API",
            token_id=token,
            name=request.name,
            tenant_id=tenant_id,
            motet_id=motet_id,
            created_by=principal.id
        )
        
        return CreateServiceAccountResponse(
            token=token,
            name=token_meta.name,
            principal_id=token_meta.principal_id,
            tenant_id=token_meta.tenant_id,
            motet_id=token_meta.motet_id,
            roles=token_meta.roles,
            created_at=token_meta.created_at.isoformat(),
            expires_at=token_meta.expires_at.isoformat(),
        )
        
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning("Invalid service account creation request", error=str(e), principal_id=principal.id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.error("Failed to create service account", error=str(e), exc_info=True, principal_id=principal.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create service account: {str(e)}"
        )


@router.get(
    "",
    summary="List service accounts",
    description="List all service accounts (optionally filtered by tenant and motet)",
    response_model=ListServiceAccountsResponse,
    responses={
        200: {"description": "Service accounts listed successfully"},
        401: {"description": "Authentication required"},
        403: {"description": "Foreign tenant_id or motet_id without global scope"},
        500: {"description": "Internal server error"}
    }
)
async def list_service_accounts(
    tenant_id: Optional[str] = None,
    motet_id: Optional[str] = None,
    principal: Principal = Depends(get_current_principal)
) -> ListServiceAccountsResponse:
    """
    List service accounts in the caller's authorized tenant.

    Omitted tenant_id / motet_id resolve to the authenticated principal.
    A foreign filter requires global tenant access; otherwise 403.
    Only active (non-revoked, non-expired) accounts are returned.
    
    Args:
        tenant_id: Optional tenant ID filter (authorized via require_tenant_access)
        motet_id: Optional motet/environment filter (authorized via require_motet_access)
        principal: Authenticated principal (from JWT or service account)
        
    Returns:
        List of service accounts
        
    Raises:
        HTTPException: If listing fails
    """
    try:
        sa_manager = get_service_account_manager()
        authorized_tenant = require_tenant_access(principal, tenant_id)
        authorized_motet = require_motet_access(principal, motet_id) if motet_id else None
        accounts = sa_manager.list_service_accounts(
            tenant_id=authorized_tenant, motet_id=authorized_motet
        )
        
        account_infos = [
            ServiceAccountInfo(
                id=acc.id,
                name=acc.name,
                principal_id=acc.principal_id,
                tenant_id=acc.tenant_id,
                motet_id=acc.motet_id,
                roles=acc.roles,
                created_at=acc.created_at.isoformat(),
                expires_at=acc.expires_at.isoformat(),
                last_used_at=acc.last_used_at.isoformat() if acc.last_used_at else None,
                revoked_at=(
                    _ra.isoformat()
                    if (_ra := getattr(acc, "revoked_at", None)) is not None
                    else None
                ),
                facade_mode=getattr(acc, "facade_mode", None),
                allowed_models=list(getattr(acc, "allowed_models", None) or []),
                force_thinking=getattr(acc, "force_thinking", None),
                force_thinking_effort=getattr(acc, "force_thinking_effort", None),
                agent_id=getattr(acc, "agent_id", None),
            )
            for acc in accounts
        ]
        
        logger.debug(
            "Service accounts listed via API",
            count=len(account_infos),
            tenant_id=authorized_tenant,
            motet_id=authorized_motet,
            principal_id=principal.id
        )
        
        return ListServiceAccountsResponse(service_accounts=account_infos)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to list service accounts", error=str(e), exc_info=True, principal_id=principal.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list service accounts: {str(e)}"
        )


@router.delete(
    "/{token_id}",
    summary="Revoke service account",
    description="Revoke a service account token",
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Service account revoked successfully"},
        401: {"description": "Authentication required"},
        403: {"description": "Token belongs to another tenant"},
        404: {"description": "Service account not found"},
        500: {"description": "Internal server error"}
    }
)
async def revoke_service_account(
    token_id: str,
    principal: Principal = Depends(get_current_principal)
) -> Dict[str, Any]:
    """
    Revoke a service account token.
    
    Revoked tokens cannot be used for authentication.
    The token will be marked as revoked but not deleted (for audit purposes).
    
    Args:
        token_id: Service account token ID to revoke
        principal: Authenticated principal (from JWT or service account)
        
    Returns:
        Success message
        
    Raises:
        HTTPException: If revocation fails
    """
    try:
        sa_manager = get_service_account_manager()
        token_meta = sa_manager.verify_service_account(token_id)
        if not token_meta:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Service account token not found: {token_id}"
            )
        require_tenant_access(principal, token_meta.tenant_id)
        success = sa_manager.revoke_service_account(token_id)
        
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Service account token not found: {token_id}"
            )
        
        logger.info(
            "Service account revoked via API",
            token_id=token_id,
            principal_id=principal.id
        )
        
        return {"status": "success", "message": f"Service account token '{token_id}' revoked successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to revoke service account", error=str(e), exc_info=True, principal_id=principal.id, token_id=token_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to revoke service account: {str(e)}"
        )

