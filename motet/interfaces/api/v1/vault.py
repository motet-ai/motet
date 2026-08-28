"""
Motet - Vault API

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Vault API for the Motet distributed framework.
    Provides REST API endpoints for secure storage and retrieval of sensitive data.
    includes POST /resolve endpoint for local worker vault access
    via HTTPS through WireGuard tunnel.

Dependencies:
    - fastapi: Web framework for REST API
    - pydantic: Data validation and serialization
    - structlog: Structured logging
    - datetime: Time and date handling
    - motet.interfaces.api.shared.auth: Shared authentication utilities

Usage:
    from motet.interfaces.api.v1.vault import router
    
    # Include in FastAPI app
    app.include_router(router)

Notes:
    - Provides secure storage for sensitive data
    - Includes encryption and access control
    - Supports CRUD operations for vault items
    - Integrates with distributed architecture
"""

from typing import Dict, Any, Optional, List
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends, Request, status
from pydantic import BaseModel, Field
import structlog

from ....core.security.vault_service import (
    DistributedVaultService,
    CredentialType,
    CredentialScope,
    CredentialSecurityLevel,
    CredentialMetadata
)
from ....core.security.vault_client import VaultClient, get_vault_client
from ..shared.auth import (
    get_current_principal,
    is_admin_principal,
    require_admin_principal,
    require_tenant_access,
)
from ..shared.identity import get_principal_context
from ....core.types import Principal
from ....core.edge.device_registry import EdgeDeviceRegistry

logger = structlog.get_logger(__name__)

# Create router for vault endpoints
router = APIRouter(prefix="/api/v1/vault", tags=["vault"])


# Pydantic models for API requests/responses
class StoreCredentialRequest(BaseModel):
    """Request model for storing a credential."""
    credential_id: str = Field(..., description="Unique identifier for the credential")
    credential_data: Dict[str, Any] = Field(..., description="The credential data to store")
    credential_type: CredentialType = Field(..., description="Type of credential")
    scope: CredentialScope = Field(..., description="Access scope for the credential")
    security_level: CredentialSecurityLevel = Field(..., description="Security classification level")
    tenant_id: Optional[str] = Field(None, description="Tenant ID for tenant-scoped credentials")
    motet_id: Optional[str] = Field(None, description="Motet ID for motet-scoped credentials")
    expires_at: Optional[datetime] = Field(None, description="Optional expiration time")
    tags: Optional[List[str]] = Field(None, description="Optional tags for categorization")
    description: str = Field("", description="Optional description")


class RetrieveCredentialRequest(BaseModel):
    """Request model for retrieving a credential."""
    credential_key: str = Field(..., description="Key identifying the credential to retrieve")
    tenant_id: Optional[str] = Field(None, description="Tenant ID for tenant-scoped credentials")
    motet_id: Optional[str] = Field(None, description="Motet ID for motet-scoped credentials")


class CredentialResponse(BaseModel):
    """Response model for credential operations."""
    success: bool = Field(..., description="Whether the operation was successful")
    credential_data: Optional[Dict[str, Any]] = Field(default=None, description="The credential data")
    error_message: Optional[str] = Field(default=None, description="Error message if operation failed")
    access_granted_at: Optional[datetime] = Field(default=None, description="When access was granted")
    expires_at: Optional[datetime] = Field(default=None, description="When the credential expires")


class CredentialListResponse(BaseModel):
    """Response model for listing credentials."""
    credentials: List[CredentialMetadata] = Field(..., description="List of accessible credentials")
    total_count: int = Field(..., description="Total number of credentials")


class DeleteCredentialRequest(BaseModel):
    """Request model for deleting a credential."""
    credential_id: str = Field(..., description="ID of the credential to delete")


class MCPEnvironmentRequest(BaseModel):
    """Request model for getting MCP environment variables."""
    mcp_server_id: str = Field(..., description="ID of the MCP server")
    tenant_id: Optional[str] = Field(None, description="Tenant ID for tenant-scoped credentials")
    motet_id: Optional[str] = Field(None, description="Motet ID for motet-scoped credentials")


class MCPEnvironmentResponse(BaseModel):
    """Response model for MCP environment variables."""
    mcp_server_id: str = Field(..., description="ID of the MCP server")
    environment_variables: Dict[str, str] = Field(..., description="Environment variables for the MCP server")
    success: bool = Field(..., description="Whether the operation was successful")
    error_message: Optional[str] = Field(default=None, description="Error message if operation failed")


class VaultHealthResponse(BaseModel):
    """Response model for vault health check."""
    status: str = Field(..., description="Health status ('healthy' or 'unhealthy')")
    redis_connected: bool = Field(..., description="Whether Redis is reachable")
    timestamp: str = Field(..., description="ISO 8601 timestamp of the health check")
    error: Optional[str] = Field(default=None, description="Error message if unhealthy")


class VaultStatsData(BaseModel):
    """Vault statistics data."""
    total_credentials: int = Field(..., description="Total number of stored credentials")
    active_credentials: int = Field(..., description="Number of non-expired credentials")
    expired_credentials: int = Field(..., description="Number of expired credentials")
    vault_status: str = Field(..., description="Overall vault health status")


class VaultStatsResponse(BaseModel):
    """Response model for vault statistics."""
    status: str = Field(..., description="Operation status ('success' or 'error')")
    stats: VaultStatsData = Field(..., description="Vault statistics")
    error: Optional[str] = Field(default=None, description="Error message if the operation failed")


class MCPServersResponse(BaseModel):
    """Response model for supported MCP servers."""
    supported_mcp_servers: List[Dict[str, Any]] = Field(..., description="List of MCP server credential mappings")
    total_count: int = Field(..., description="Number of supported MCP servers")


class VaultResolveRequest(BaseModel):
    """Request for local worker vault credential resolution."""
    credential_key: str = Field(..., description="Key identifying the credential to resolve")
    motet_id: Optional[str] = Field(None, description="Motet ID for scoped credentials")


class VaultResolveResponse(BaseModel):
    """Response for vault resolve: minimal payload for local workers."""
    ok: bool = Field(..., description="Whether the credential was resolved successfully")
    found: bool = Field(False, description="Whether the credential exists")
    credential_data: Optional[Dict[str, Any]] = Field(None, description="The resolved credential data")
    error_code: Optional[str] = Field(None, description="Error code if resolution failed")
    error_message: Optional[str] = Field(None, description="Error message if resolution failed")


async def _get_device_token_principal(request: Request) -> Principal:
    """Authenticate local worker device tokens for vault resolve (ADR-0095)."""
    auth_header = request.headers.get("authorization", "")
    token = ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    if not token:
        token = request.query_params.get("token", "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Device token required")
    session = EdgeDeviceRegistry().verify_token(token)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid or revoked device token")
    return Principal(
        id=session.principal_id,
        tenant_id=session.tenant_id,
        roles=["device"],
        claims={"auth_type": "edge_device_token", "device_id": session.device_id},
    )


# Vault service instance
_vault_service: Optional[DistributedVaultService] = None


def get_vault_service() -> DistributedVaultService:
    """Get the vault service instance."""
    global _vault_service
    
    if _vault_service is None:
        _vault_service = DistributedVaultService()
    
    return _vault_service


def _credential_not_expired(metadata: CredentialMetadata) -> bool:
    """True if metadata has no expiry or expires_at is in the future (UTC)."""
    exp = metadata.expires_at
    if exp is None:
        return True
    now_utc = datetime.now(timezone.utc)
    if exp.tzinfo is None:
        return exp > datetime.utcnow()
    return exp > now_utc


@router.post("/credentials", response_model=CredentialResponse)
async def store_credential(
    request: StoreCredentialRequest,
    principal: Principal = Depends(get_current_principal)
):
    """
    Store a credential in the vault.
    
    Requires authentication and appropriate permissions.
    """
    motet_id, tenant_id, principal_id = get_principal_context(principal)
    tenant_id = require_tenant_access(principal, request.tenant_id, fallback=tenant_id)
    motet_id_eff = request.motet_id or motet_id
    try:
        vault_service = get_vault_service()
        success = vault_service.store_credential(
            credential_id=request.credential_id,
            credential_data=request.credential_data,
            credential_type=request.credential_type,
            scope=request.scope,
            security_level=request.security_level,
            principal_id=principal_id,
            tenant_id=tenant_id,
            motet_id=motet_id_eff,
            expires_at=request.expires_at,
            tags=request.tags,
            description=request.description
        )
        
        if success:
            logger.info("Credential stored via API",
                       credential_id=request.credential_id,
                       principal_id=principal_id,
                       tenant_id=tenant_id)
            
            return CredentialResponse(
                success=True,
                access_granted_at=datetime.utcnow()
            )
        else:
            return CredentialResponse(
                success=False,
                error_message="Failed to store credential"
            )
    
    except Exception as e:
        logger.error("Error storing credential via API",
                    credential_id=request.credential_id,
                    principal_id=principal_id,
                    error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error: {str(e)}"
        )


@router.post("/credentials/retrieve", response_model=CredentialResponse)
async def retrieve_credential(
    request: RetrieveCredentialRequest,
    principal: Principal = Depends(get_current_principal)
):
    """
    Retrieve a credential from the vault.
    
    Requires authentication and appropriate permissions.
    """
    motet_id, tenant_id, principal_id = get_principal_context(principal)
    tenant_id = require_tenant_access(principal, request.tenant_id, fallback=tenant_id)
    motet_id_eff = request.motet_id or motet_id
    try:
        vault_service = get_vault_service()
        from ....core.security.vault_service import CredentialAccessRequest
        access_request = CredentialAccessRequest(
            principal_id=principal_id,
            tenant_id=tenant_id,
            motet_id=motet_id_eff,
            credential_key=request.credential_key
        )
        response = vault_service.retrieve_credential(access_request)
        if response.success:
            logger.info("Credential retrieved via API",
                       credential_key=request.credential_key,
                       principal_id=principal_id,
                       tenant_id=tenant_id)
        
        return CredentialResponse(
            success=response.success,
            credential_data=response.credential_data,
            error_message=response.error_message,
            access_granted_at=response.access_granted_at,
            expires_at=response.expires_at
        )
    
    except Exception as e:
        logger.error("Error retrieving credential via API",
                    credential_key=request.credential_key,
                    principal_id=principal_id,
                    error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error: {str(e)}"
        )


@router.get("/credentials")
async def list_credentials(
    tenant_id: Optional[str] = None,
    motet_id: Optional[str] = None,
    credential_type: Optional[CredentialType] = None,
    principal: Principal = Depends(get_current_principal),
):
    """
    List credentials accessible to the current principal.

    Requires admin. Unauthenticated ops.html fallbacks are retired.
    """
    require_admin_principal(principal, detail="Admin role required to list credentials")
    try:
        p_motet_id, p_tenant_id, p_principal_id = get_principal_context(principal)
        eff_tenant_id = require_tenant_access(principal, tenant_id, fallback=p_tenant_id)
        eff_motet_id = motet_id or p_motet_id
        vault_service = get_vault_service()
        credentials = vault_service.list_credentials(
            principal_id=p_principal_id,
            tenant_id=eff_tenant_id,
            motet_id=eff_motet_id,
            credential_type=credential_type,
            include_all=True,
        )
        logger.info("Credentials listed via API",
                   principal_id=p_principal_id,
                   tenant_id=p_tenant_id,
                   credential_count=len(credentials))
        
        # Return format compatible with ops.html
        return {
            "status": "success",
            "credentials": [
                {
                    "credential_id": cred.credential_id,
                    "credential_type": cred.credential_type.value if hasattr(cred.credential_type, 'value') else str(cred.credential_type),
                    "scope": cred.scope.value if hasattr(cred.scope, 'value') else str(cred.scope),
                    "security_level": cred.security_level.value if hasattr(cred.security_level, 'value') else str(cred.security_level),
                    "principal_id": cred.principal_id or "",
                    "tenant_id": cred.tenant_id or "",
                    "motet_id": cred.motet_id or "",
                    "description": cred.description or "",
                    "created_at": cred.created_at.isoformat() if hasattr(cred.created_at, 'isoformat') else str(cred.created_at),
                    "expires_at": cred.expires_at.isoformat() if cred.expires_at and hasattr(cred.expires_at, 'isoformat') else (str(cred.expires_at) if cred.expires_at else None),
                    "tags": cred.tags or []
                }
                for cred in credentials
            ],
            "total_count": len(credentials)
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error listing credentials via API", error=str(e))
        
        return {
            "status": "error",
            "error": str(e),
            "credentials": [],
            "total_count": 0
        }


@router.get("/credentials/{credential_id}")
async def get_credential(
    credential_id: str,
    principal: Principal = Depends(get_current_principal),
):
    """
    Get a single credential by ID.

    Requires authentication. Admins may see any credential; other principals
    only see credentials in their own scope.
    """
    try:
        _, p_tenant_id, p_principal_id = get_principal_context(principal)
        vault_service = get_vault_service()
        credentials = vault_service.list_credentials(
            principal_id=p_principal_id,
            tenant_id=p_tenant_id,
            include_all=is_admin_principal(principal),
        )
        
        # Find the credential by ID
        credential = next((c for c in credentials if c.credential_id == credential_id), None)
        
        if not credential:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Credential not found: {credential_id}"
            )
        
        return {
            "status": "success",
            "credential": {
                "credential_id": credential.credential_id,
                "credential_type": credential.credential_type.value if hasattr(credential.credential_type, 'value') else str(credential.credential_type),
                "scope": credential.scope.value if hasattr(credential.scope, 'value') else str(credential.scope),
                "security_level": credential.security_level.value if hasattr(credential.security_level, 'value') else str(credential.security_level),
                "principal_id": credential.principal_id or "",
                "tenant_id": credential.tenant_id or "",
                "motet_id": credential.motet_id or "",
                "description": credential.description or "",
                "created_at": credential.created_at.isoformat() if hasattr(credential.created_at, 'isoformat') else str(credential.created_at),
                "expires_at": credential.expires_at.isoformat() if credential.expires_at and hasattr(credential.expires_at, 'isoformat') else (str(credential.expires_at) if credential.expires_at else None),
                "tags": credential.tags or []
            }
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error getting credential via API", 
                    credential_id=credential_id,
                    error=str(e))
        
        return {
            "status": "error",
            "error": str(e)
        }


@router.delete("/credentials", response_model=CredentialResponse)
async def delete_credential(
    request: DeleteCredentialRequest,
    principal: Principal = Depends(get_current_principal)
):
    """
    Delete a credential from the vault.
    
    Requires authentication and ownership of the credential.
    """
    _, tenant_id, principal_id = get_principal_context(principal)
    try:
        vault_service = get_vault_service()
        success = vault_service.delete_credential(
            credential_id=request.credential_id,
            principal_id=principal_id,
            tenant_id=tenant_id,
        )
        if success:
            logger.info(
                "admin_audit",
                action="vault_credential_deleted",
                credential_id=request.credential_id,
                principal_id=principal_id,
                tenant_id=principal.tenant_id,
            )
            
            return CredentialResponse(success=True)
        else:
            return CredentialResponse(
                success=False,
                error_message="Failed to delete credential or insufficient permissions"
            )
    
    except Exception as e:
        logger.error("Error deleting credential via API",
                    credential_id=request.credential_id,
                    principal_id=principal_id,
                    error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error: {str(e)}"
        )


@router.post("/mcp/environment", response_model=MCPEnvironmentResponse)
async def get_mcp_environment_variables(
    request: MCPEnvironmentRequest,
    principal: Principal = Depends(get_current_principal)
):
    """
    Get environment variables for an MCP server with credentials from vault.
    
    Requires authentication and appropriate permissions.
    """
    _, tenant_id, principal_id = get_principal_context(principal)
    tenant_id = require_tenant_access(principal, request.tenant_id, fallback=tenant_id)
    try:
        from ....core.security.vault_mcp_integration import get_vault_mcp_integration
        from motet.core.commands.base import CommandContext
        context = CommandContext(
            task_id="api_request",
            conversation_id="",
            tenant_id=tenant_id,
            principal_id=principal_id
        )
        integration = get_vault_mcp_integration()
        env_vars = integration.get_mcp_environment_variables(
            mcp_server_id=request.mcp_server_id,
            context=context
        )
        logger.info("MCP environment variables retrieved via API",
                   mcp_server_id=request.mcp_server_id,
                   principal_id=principal_id,
                   tenant_id=tenant_id,
                   env_var_count=len(env_vars))
        
        return MCPEnvironmentResponse(
            mcp_server_id=request.mcp_server_id,
            environment_variables=env_vars,
            success=True
        )
    
    except Exception as e:
        logger.error("Error getting MCP environment variables via API",
                    mcp_server_id=request.mcp_server_id,
                    principal_id=principal_id,
                    error=str(e))
        return MCPEnvironmentResponse(
            mcp_server_id=request.mcp_server_id,
            environment_variables={},
            success=False,
            error_message=str(e)
        )


@router.get("/mcp/servers", response_model=MCPServersResponse)
async def get_supported_mcp_servers(principal: Principal = Depends(get_current_principal)):
    """
    Get list of MCP servers with registered credential mappings.
    
    Requires authentication.
    """
    _, _, principal_id = get_principal_context(principal)
    try:
        from ....core.security.vault_mcp_integration import get_vault_mcp_integration
        integration = get_vault_mcp_integration()
        servers = integration.get_supported_mcp_servers()
        # API model expects structured entries; integration returns server id strings.
        return {
            "supported_mcp_servers": [{"server_id": sid} for sid in servers],
            "total_count": len(servers),
        }
    except Exception as e:
        logger.error("Error getting supported MCP servers via API",
                    principal_id=principal_id,
                    error=str(e))
        
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error: {str(e)}"
        )


@router.get("/health", response_model=VaultHealthResponse)
async def vault_health_check():
    """
    Health check endpoint for the vault service.
    
    No authentication required.
    """
    try:
        vault_service = get_vault_service()
        
        # Simple health check - try to connect to Redis
        from ....core.distributed.redis_manager import redis_health_check
        redis_healthy = await redis_health_check("vault_service")
        
        return {
            "status": "healthy" if redis_healthy else "unhealthy",
            "redis_connected": redis_healthy,
            "timestamp": datetime.utcnow().isoformat()
        }
    
    except Exception as e:
        logger.error("Vault health check failed", error=str(e))
        
        return {
            "status": "unhealthy",
            "redis_connected": False,
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }


@router.get("/stats", response_model=VaultStatsResponse)
async def get_vault_stats(
    principal: Principal = Depends(get_current_principal),
):
    """
    Get vault statistics for the ops dashboard.

    Requires admin. Unauthenticated ops.html fallbacks are retired.
    """
    require_admin_principal(principal, detail="Admin role required to read vault stats")
    try:
        vault_service = get_vault_service()
        credentials = vault_service.list_credentials(
            principal_id=principal.id,
            tenant_id=None,
            motet_id=None,
            credential_type=None,
            include_all=True,
        )
        
        # Calculate stats
        total_credentials = len(credentials)
        active_credentials = sum(1 for cred in credentials if _credential_not_expired(cred))
        expired_credentials = total_credentials - active_credentials
        
        # Check vault health
        from ....core.distributed.redis_manager import redis_health_check
        redis_healthy = await redis_health_check("vault_service")
        vault_status = "healthy" if redis_healthy else "unhealthy"
        
        return {
            "status": "success",
            "stats": {
                "total_credentials": total_credentials,
                "active_credentials": active_credentials,
                "expired_credentials": expired_credentials,
                "vault_status": vault_status
            }
        }
    
    except Exception as e:
        logger.error("Error getting vault stats via API", error=str(e))
        
        return {
            "status": "error",
            "error": str(e),
            "stats": {
                "total_credentials": 0,
                "active_credentials": 0,
                "expired_credentials": 0,
                "vault_status": "error"
            }
        }


@router.get("/metrics")
async def get_vault_metrics(principal: Principal = Depends(get_current_principal)):
    """
    Get vault service metrics.
    
    Requires authentication.
    """
    _, _, principal_id = get_principal_context(principal)
    try:
        vault_client = get_vault_client()
        metrics = vault_client.get_metrics()
        return {
            "vault_client_metrics": metrics,
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error("Error getting vault metrics via API",
                    principal_id=principal_id,
                    error=str(e))
        
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error: {str(e)}"
        )


_ENCRYPTION_TENANT_PREFIX = "encryption:tenant:"


def _enforce_tenant_kek_scope(credential_key: str, tenant_id: Optional[str]) -> None:
    """Block cross-tenant KEK access from local worker devices.

    Tenant KEKs are stored with GLOBAL scope (so cloud workers — which are
    trusted — can access any tenant's key).  Local workers run on user hardware
    and must be restricted to their own tenant's KEK.  This guard runs before
    the vault service's own _check_authorization (which allows GLOBAL).
    """
    if not credential_key.startswith(_ENCRYPTION_TENANT_PREFIX):
        return
    requested_tenant = credential_key[len(_ENCRYPTION_TENANT_PREFIX):]
    _SYSTEM_TENANTS = {"discovery-tenant", "default", "default-tenant"}
    if requested_tenant in _SYSTEM_TENANTS:
        return
    if not tenant_id or requested_tenant != tenant_id:
        logger.warning(
            "vault_resolve_cross_tenant_kek_blocked",
            credential_key=credential_key,
            requested_tenant=requested_tenant,
            device_tenant=tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Device tenant '{tenant_id}' cannot access KEK for "
                f"tenant '{requested_tenant}'"
            ),
        )


@router.post(
    "/resolve",
    response_model=VaultResolveResponse,
    responses={
        200: {"description": "Credential resolved (check 'found' field)"},
        401: {"description": "Invalid or missing device token"},
        500: {"description": "Internal vault error"},
    },
)
async def resolve_credential(
    body: VaultResolveRequest,
    principal: Principal = Depends(_get_device_token_principal),
):
    """
    Resolve a vault credential for a local worker device.

    Local workers call this endpoint over HTTPS through the WireGuard tunnel.
    Authenticated with the device token issued at registration.
    """
    _, tenant_id, principal_id = get_principal_context(principal)

    _enforce_tenant_kek_scope(body.credential_key, tenant_id)

    try:
        vault_service = get_vault_service()
        from ....core.security.vault_service import CredentialAccessRequest

        access_request = CredentialAccessRequest(
            principal_id=principal_id,
            tenant_id=tenant_id,
            motet_id=body.motet_id,
            credential_key=body.credential_key,
        )
        response = vault_service.retrieve_credential(access_request)
        if response.success:
            logger.info(
                "vault_resolve_success",
                credential_key=body.credential_key,
                principal_id=principal_id,
            )
            return VaultResolveResponse(
                ok=True,
                found=response.credential_data is not None,
                credential_data=response.credential_data,
                error_code=None,
                error_message=None,
            )
        return VaultResolveResponse(
            ok=True,
            found=False,
            credential_data=None,
            error_code=None,
            error_message=response.error_message,
        )
    except Exception as e:
        logger.error(
            "vault_resolve_error",
            credential_key=body.credential_key,
            principal_id=principal_id,
            error=str(e),
            exc_info=True,
        )
        return VaultResolveResponse(
            ok=False,
            found=False,
            credential_data=None,
            error_code="internal_error",
            error_message=str(e),
        )


# Export the router
__all__ = ["router"]

