"""
Motet - Shared API Authentication

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-15

Description:
    Shared authentication utilities for all API endpoints. Provides a unified
    get_current_principal() function used across all APIs for consistent
    authentication handling, plus can_access_all_tenants() so every API applies
    the same definition of cross-tenant (global scope) visibility.
    require_tenant_access / require_motet_access enforce the issue #214
    invariant: a caller-supplied tenant_id or motet_id is a name, not
    permission.

Dependencies:
    - fastapi: Web framework for REST API
    - motet.core.security: Authentication and principal extraction
    - motet.core.config: Configuration management
    - motet.core.types: Principal type definition

Usage:
    from motet.interfaces.api.shared.auth import (
        can_access_all_tenants,
        get_current_principal,
        is_admin_principal,
        require_admin_principal,
        require_can_access_all_tenants,
        require_motet_access,
        require_tenant_access,
    )

    @router.get("/endpoint")
    async def my_endpoint(principal: Principal = Depends(get_current_principal)):
        if can_access_all_tenants(principal):
            ...  # serve the whole catalog
        # Use principal for authenticated operations
        pass

Notes:
    - Config is created inside the function to avoid Pydantic v2 namespace inspection
      issues with pydantic-settings BaseSettings private attributes
    - Raises HTTPException 401 if authentication is required but not provided
    - Used by all API endpoints for consistent authentication
"""

from typing import Optional

from fastapi import HTTPException, Request, status
import structlog

from motet.core.security import extract_principal
from motet.core.types import Principal
from motet.core.config import Config

logger = structlog.get_logger(__name__)

ADMIN_ROLES = frozenset({"admin", "motet-admin"})
GLOBAL_SCOPE_PRINCIPAL_IDS = frozenset({"ops_dashboard"})


def is_admin_principal(principal: Principal) -> bool:
    """Report whether a principal has an admin role or the ops_dashboard id.

    Unlike ``can_access_all_tenants``, this does not treat
    ``tenant_scope=global`` as admin. Use it for destructive or
    credential-listing operations that ADR-0066 gates on admin.
    """
    if principal.id in GLOBAL_SCOPE_PRINCIPAL_IDS:
        return True
    return bool(set(principal.roles or []) & ADMIN_ROLES)


def can_access_all_tenants(principal: Principal) -> bool:
    """Report whether a principal may read data across every tenant.

    Cross-tenant visibility is granted by an admin role, by the built-in
    ops_dashboard principal, or by the ``tenant_scope=global`` claim that
    tenant mapping sets for ids in MOTET_TENANT_GLOBAL_IDS.

    Args:
        principal: Authenticated principal to evaluate

    Returns:
        True when the caller may see the whole catalog, False when the caller
        is confined to its own tenant
    """
    if is_admin_principal(principal):
        return True
    return (principal.claims or {}).get("tenant_scope") == "global"


def require_admin_principal(
    principal: Principal,
    detail: str = "Admin role required",
) -> None:
    """Raise HTTP 403 unless the principal is an admin."""
    if not is_admin_principal(principal):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def require_can_access_all_tenants(
    principal: Principal,
    detail: str = "Not authorized to access all tenants",
) -> None:
    """Raise HTTP 403 unless the principal may see every tenant."""
    if not can_access_all_tenants(principal):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def require_tenant_access(
    principal: Principal,
    requested_tenant_id: Optional[str],
    *,
    fallback: Optional[str] = None,
) -> str:
    """Return the tenant the caller may query, or raise HTTP 403.

    Copy this helper onto every new endpoint that accepts ``tenant_id``.
    The HTTP API is the tenant security boundary; workers trust the
    tenant stamped on the command. Invariant (issue #214):

    1. Identity comes from the principal, not the body or query.
    2. A request ``tenant_id`` / ``motet_id`` / ``conversation_id`` is a
       name, not permission.
    3. Cross-tenant access requires ``can_access_all_tenants``; otherwise
       raise 403. Do not silently substitute the caller's tenant.

    An omitted or matching tenant is allowed and resolves to the
    caller's tenant. A different explicit tenant requires global scope.

    Args:
        principal: Authenticated principal
        requested_tenant_id: Caller-supplied tenant id, if any
        fallback: Tenant to use when the request omits one (defaults to
            ``principal.tenant_id`` or ``default``)

    Returns:
        The tenant id the request is authorized to use
    """
    resolved_fallback = fallback or principal.tenant_id or "default"
    if not requested_tenant_id or requested_tenant_id == resolved_fallback:
        return requested_tenant_id or resolved_fallback
    if not can_access_all_tenants(principal):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access another tenant",
        )
    return requested_tenant_id


def require_motet_access(
    principal: Principal,
    requested_motet_id: Optional[str],
    *,
    fallback: Optional[str] = None,
) -> str:
    """Return the motet the caller may query, or raise HTTP 403.

    Same invariant as ``require_tenant_access``: a request ``motet_id``
    is a name, not permission. Omitted or matching values resolve to the
    principal's motet. A different explicit motet requires
    ``can_access_all_tenants``.

    Args:
        principal: Authenticated principal
        requested_motet_id: Caller-supplied motet id, if any
        fallback: Motet to use when the request omits one (defaults to
            ``principal.motet_id`` or ``default``)

    Returns:
        The motet id the request is authorized to use
    """
    resolved_fallback = fallback or principal.motet_id or "default"
    if not requested_motet_id or requested_motet_id == resolved_fallback:
        return requested_motet_id or resolved_fallback
    if not can_access_all_tenants(principal):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access another motet",
        )
    return requested_motet_id


async def get_current_principal(
    request: Request
) -> Principal:
    """Get the current principal from the request.
    
    Raises HTTPException 401 if no principal is found.
    
    Args:
        request: FastAPI Request object (injected by FastAPI)
        
    Returns:
        Principal object with authenticated user information
        
    Raises:
        HTTPException: 401 if authentication is required but not provided
    """
    # Create Config instance directly (reads from environment variables)
    # This avoids using Depends(Config) which triggers Pydantic v2 namespace inspection
    # that sees Config's private attributes from pydantic-settings BaseSettings
    cfg = Config()
    principal = extract_principal(cfg, request)
    if not principal:
        # If JWT auth is configured, provide a more specific 401 error when the token
        # is missing or invalid (e.g., issuer/audience mismatch, JWKS fetch failure).
        if getattr(cfg, "jwt_public_key_pem", None) or getattr(cfg, "jwt_jwks_url", None):
            try:
                from motet.core.security.auth import require_jwt_if_configured

                cache = None
                try:
                    if hasattr(request, "app") and hasattr(request.app.state, "_jwks_cache_obj"):
                        cache = request.app.state._jwks_cache_obj
                except Exception:
                    cache = None

                require_jwt_if_configured(cfg, request, cache=cache)
            except HTTPException:
                raise
            except Exception as e:
                logger.error("principal_extraction_failed", error=str(e), error_type=type(e).__name__, exc_info=True)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication required"
                )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required"
        )
    if not principal.id:
        logger.warning("principal_missing_sub_claim")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Authenticated identity missing subject claim"
        )
    if not principal.tenant_id:
        if getattr(cfg, "allow_insecure_principal_headers", False):
            logger.warning("principal_missing_tenant_claim_dev_mode", principal_id=principal.id)
        else:
            logger.error("principal_missing_tenant_claim", principal_id=principal.id)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Authenticated identity missing tenant claim"
            )
    return principal

