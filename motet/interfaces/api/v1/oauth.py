"""
Motet - OAuth API

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    OAuth API for the Motet distributed framework.
    Provides REST API endpoints for OAuth authentication flows for MCP servers
    and other external service integrations.

    Enhanced callback with postMessage for popup communication,
    auto-close behavior, and mcp.auth_updated event emission.

Dependencies:
    - fastapi: Web framework for REST API
    - fastapi.responses: HTMLResponse for OAuth callback pages
    - motet.core.security.oauth_manager: OAuth flow management
    - motet.core.workers.events: Event bus for auth_updated events

Usage:
    from motet.interfaces.api.v1.oauth import router
    
    # Include in FastAPI app
    app.include_router(router)

Notes:
    - Provides OAuth flows for MCP servers (e.g., google_workspace, github)
    - Supports future OAuth integrations for Motet services (e.g., slack, microsoft_teams)
    - OAuth tokens are stored securely in the vault
    - Part of Phase 2: API Organization and URL Standardization
    - Part of OAuth Proxy Service for MCP Server Authentication
    - Part of MCP OAuth Prompt Flow for Missing Authorization
"""

from typing import Dict, Any, Optional, List
from pathlib import Path
from fastapi import APIRouter, HTTPException, Header, Request, Query, Depends, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
import structlog

from ..shared.auth import get_current_principal
from ....core.types import Principal
from ....core.security.system_principals import (
    SYSTEM_PRINCIPAL_OAUTH_API,
    SYSTEM_TENANT_ID,
    SYSTEM_MOTET_ID,
)

logger = structlog.get_logger(__name__)

# Initialize templates
_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent.parent / "templates"))

router = APIRouter(prefix="/api/v1/oauth", tags=["oauth"])


def _store_oauth_login_memory(
    conversation_content: str,
    conversation_id: str,
    task_id: Optional[str],
    principal_id: Optional[str],
    tenant_id: str,
    motet_id: str,
    provider: str,
    display_name: str
) -> None:
    """
    Store OAuth login success as conversation memory (background task).
    
    This ensures prepare_context includes the login success in conversation history,
    so the LLM knows the user is logged in even if login happened via popup.
    """
    try:
        import time
        from uuid import uuid4
        from ....core.memory.constants import CONVERSATION_SCOPE_TAG_PREFIX
        from motet.core.commands.builtin.memory import memory_store, MemoryStoreData
        from ....core.workers import global_invoker
        from ....core.workers.observers import EventPriority
        
        # Create memory store command with context parameters
        # The decorated function returns a DistributedCommand instance
        store_cmd = memory_store(
            data=MemoryStoreData(
                content=conversation_content,
                type="conversation_turn",
                tags=[f"{CONVERSATION_SCOPE_TAG_PREFIX}{conversation_id}", "oauth_login", provider],
                metadata={
                    "task_id": task_id,
                    "conversation_id": conversation_id,
                    "timestamp": time.time(),
                    "oauth_provider": provider,
                    "oauth_display_name": display_name
                }
            ),
            task_id=task_id or f"oauth-{uuid4().hex[:12]}",
            conversation_id=conversation_id,
            principal_id=principal_id,
            tenant_id=tenant_id,
            motet_id=motet_id,
            priority=EventPriority.NORMAL.value if hasattr(EventPriority.NORMAL, 'value') else EventPriority.NORMAL
        )
        
        # Execute command synchronously (background task runs in thread pool)
        result = global_invoker.execute_command(store_cmd)
        
        # Check if memory was stored successfully
        # Decorated commands return status: "completed" for success, or status: "error" for failure
        result_status = result.get("status") if isinstance(result, dict) else None
        result_error = result.get("error") if isinstance(result, dict) else None
        memory_stored = (
            isinstance(result, dict) and 
            (result_status == "success" or result_status == "completed") and 
            result_error is None
        )
        
        logger.info(
            "OAuth login success memory stored",
            provider=provider,
            conversation_id=conversation_id,
            task_id=task_id,
            principal_id=principal_id,
            tenant_id=tenant_id,
            motet_id=motet_id,
            memory_stored=memory_stored,
            result_status=result_status,
            result_error=result_error
        )
    except Exception as e:
        # Don't fail the OAuth callback if memory storage fails
        logger.warning(
            "Failed to store OAuth login success in conversation memory",
            provider=provider,
            conversation_id=conversation_id,
            task_id=task_id,
            principal_id=principal_id,
            tenant_id=tenant_id,
            motet_id=motet_id,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True
        )


class OAuthInitiateResponse(BaseModel):
    """Response model for OAuth initiation."""
    authorization_url: str = Field(..., description="URL to visit for OAuth authorization", json_schema_extra={"example": "https://accounts.google.com/o/oauth2/v2/auth?..."})
    state: str = Field(..., description="CSRF protection state parameter", json_schema_extra={"example": "abc123..."})
    instructions: str = Field(..., description="Instructions for completing OAuth flow", json_schema_extra={"example": "Visit the URL above to authorize..."})


class OAuthStatusResponse(BaseModel):
    """Response model for OAuth status."""
    provider: str = Field(..., description="OAuth provider identifier", json_schema_extra={"example": "mcp/google_workspace"})
    server_id: str = Field(..., description="Server ID (backward compatibility)", json_schema_extra={"example": "google_workspace"})
    configured: bool = Field(..., description="Whether provider is configured", json_schema_extra={"example": True})
    authenticated: bool = Field(..., description="Whether provider is authenticated", json_schema_extra={"example": True})
    expires_at: Optional[str] = Field(None, description="Token expiration timestamp", json_schema_extra={"example": "2025-11-15T10:00:00Z"})
    is_expired: Optional[bool] = Field(None, description="Whether token is expired", json_schema_extra={"example": False})
    needs_reauth: Optional[bool] = Field(None, description="Whether re-authentication is needed", json_schema_extra={"example": False})
    scopes: Optional[List[str]] = Field(None, description="OAuth scopes", json_schema_extra={"example": ["https://www.googleapis.com/auth/calendar.readonly"]})
    has_refresh_token: Optional[bool] = Field(None, description="Whether refresh token is available", json_schema_extra={"example": True})


class OAuthRefreshResponse(BaseModel):
    """Response model for OAuth token refresh."""
    success: bool = Field(..., description="Whether refresh was successful", json_schema_extra={"example": True})
    expires_at: Optional[str] = Field(None, description="New token expiration timestamp", json_schema_extra={"example": "2025-11-15T10:00:00Z"})
    message: str = Field(..., description="Refresh result message", json_schema_extra={"example": "Tokens refreshed successfully"})


class OAuthRevokeResponse(BaseModel):
    """Response model for OAuth credential revocation."""
    success: bool = Field(..., description="Whether revocation was successful", json_schema_extra={"example": True})
    provider: str = Field(..., description="Provider that was revoked", json_schema_extra={"example": "google_workspace"})
    message: str = Field(..., description="Revocation result message", json_schema_extra={"example": "OAuth credentials revoked successfully"})


class OAuthProviderInfo(BaseModel):
    """Information about a single OAuth provider."""
    provider: str = Field(..., description="Provider identifier", json_schema_extra={"example": "google_workspace"})
    display_name: str = Field(..., description="User-friendly display name", json_schema_extra={"example": "Google Workspace"})
    description: Optional[str] = Field(None, description="Provider description", json_schema_extra={"example": "Access Gmail, Drive, Calendar, Docs, and other Google services"})
    auth_type: str = Field(..., description="Authentication type", json_schema_extra={"example": "oauth2"})
    configured: bool = Field(..., description="Whether provider is configured in system", json_schema_extra={"example": True})
    authenticated: bool = Field(..., description="Whether user has valid credentials", json_schema_extra={"example": True})
    expires_at: Optional[str] = Field(None, description="Token expiration timestamp", json_schema_extra={"example": "2025-11-15T10:00:00Z"})
    is_expired: Optional[bool] = Field(None, description="Whether token is expired", json_schema_extra={"example": False})
    needs_reauth: Optional[bool] = Field(None, description="Whether re-authentication is needed", json_schema_extra={"example": False})
    scopes: Optional[List[str]] = Field(None, description="Configured OAuth scopes")
    initiate_url: str = Field(..., description="URL to initiate OAuth flow", json_schema_extra={"example": "/api/v1/oauth/google_workspace/initiate"})


class OAuthProvidersListResponse(BaseModel):
    """Response model for listing all OAuth providers."""
    providers: List[OAuthProviderInfo] = Field(..., description="List of OAuth providers and their status")
    total: int = Field(..., description="Total number of providers", json_schema_extra={"example": 3})
    authenticated_count: int = Field(..., description="Number of authenticated providers", json_schema_extra={"example": 2})


# ============================================
# OAuth Provider List Endpoint
# ============================================

@router.get(
    "/providers",
    summary="List all OAuth providers",
    description="Get a list of all configured OAuth providers and their current authentication status. "
                "Useful for showing which services are connected/disconnected in the UI.",
    response_model=OAuthProvidersListResponse,
    response_description="List of OAuth providers with status"
)
async def list_oauth_providers(
    principal: Principal = Depends(get_current_principal)
) -> OAuthProvidersListResponse:
    """
    List all configured OAuth providers and their authentication status.
    
    This endpoint returns:
    - All OAuth providers configured in the system (from mcp_instance_manager.yaml)
    - Whether each provider has valid credentials stored
    - Token expiration and status information
    - URLs to initiate OAuth for each provider
    
    Useful for:
    - UI to show connected/disconnected services
    - Admin dashboards to view OAuth status
    - Testing to verify which services need authorization
    
    Args:
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        OAuthProvidersListResponse with list of providers and their status
    """
    from ....core.security.oauth_manager import get_oauth_manager
    from ....core.security.vault_mcp_integration import get_service_auth_config
    
    try:
        oauth_manager = get_oauth_manager()
        providers_info = []
        authenticated_count = 0
        
        # Get all configured OAuth providers from MCP config
        # These are defined in mcp_instance_manager.yaml with auth sections
        from motet.core.tools.mcp_motet.proxy.mcp_instance_manager import get_oauth_providers_from_config
        known_oauth_providers = get_oauth_providers_from_config()
        
        for provider_id, provider_config in known_oauth_providers.items():
            # Get auth config for this provider
            auth_config = get_service_auth_config(provider_id)
            if not auth_config:
                auth_config = provider_config  # Use known config as fallback
            
            # Check authentication status for this principal/tenant
            try:
                status = await oauth_manager.get_oauth_status(
                    server_id=provider_id,
                    principal_id=principal.id,
                    tenant_id=principal.tenant_id,
                    motet_id=principal.motet_id
                )
                authenticated = status.get("authenticated", False)
                expires_at = status.get("expires_at")
                is_expired = status.get("is_expired", False)
                needs_reauth = status.get("needs_reauth", False)
                scopes = status.get("scopes")
            except Exception:
                authenticated = False
                expires_at = None
                is_expired = None
                needs_reauth = None
                scopes = None
            
            if authenticated:
                authenticated_count += 1
            
            provider_info = OAuthProviderInfo(
                provider=provider_id,
                display_name=auth_config.get("display_name", _get_provider_display_name(provider_id)),
                description=auth_config.get("description"),
                auth_type=auth_config.get("type", "oauth2"),
                configured=True,
                authenticated=authenticated,
                expires_at=expires_at,
                is_expired=is_expired,
                needs_reauth=needs_reauth,
                scopes=scopes or auth_config.get("scopes"),
                initiate_url=f"/api/v1/oauth/{provider_id}/initiate"
            )
            providers_info.append(provider_info)
        
        return OAuthProvidersListResponse(
            providers=providers_info,
            total=len(providers_info),
            authenticated_count=authenticated_count
        )
        
    except Exception as e:
        logger.error("Failed to list OAuth providers",
                    error=str(e),
                    exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list providers: {str(e)}")




# ============================================
# OAuth Initiate Endpoints
# ============================================
# Two separate endpoints for different use cases:
#
# GET  /{provider}/initiate - Browser/Popup Flow (ADR-0057)
#   - Used by chat UI popup when user clicks "Authorize" button
#   - Redirects directly to OAuth provider (302)
#   - Safe because: authentication required, no sensitive data in URL,
#     user must still consent at OAuth provider
#
# POST /{provider}/initiate - API/Programmatic Flow
#   - Used by backend services, CLI tools, or custom integrations
#   - Returns JSON with authorization URL for client to handle
#   - More RESTful for programmatic access
# ============================================


@router.get(
    "/{provider}/initiate",
    summary="Initiate OAuth flow (browser/popup)",
    description="Start OAuth flow via browser popup. Redirects directly to OAuth provider. "
                "Used by chat UI when user clicks 'Authorize' button. "
                "Accepts auth via query params since popups can't inherit headers.",
    responses={
        302: {"description": "Redirect to OAuth provider authorization page"}
    }
)
async def oauth_initiate_browser(
    provider: str,
    request: Request,
    # Auth can come from query params (for popup) or headers (for direct access)
    token: Optional[str] = Query(None, description="JWT or service account token (for popup flow)"),
    api_key: Optional[str] = Query(None, description="API key (for popup flow)"),
    principal_id: Optional[str] = Query(None, description="Principal ID (dev mode)"),
    tenant_id: Optional[str] = Query(
        None,
        description=(
            "Tenant ID for insecure-header simulation only. Used solely with "
            "principal_id when MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS=true. "
            "Cannot override tenant_id from a real JWT or service-account token."
        ),
    ),
    # Conversation context for storing login success in conversation history
    conversation_id: Optional[str] = Query(None, description="Conversation ID to store login success message"),
    task_id: Optional[str] = Query(None, description="Task ID for conversation context")
):
    """
    Initiate OAuth flow for browser/popup use case.
    
    This endpoint is designed for the chat UI popup flow:
    1. User clicks "Authorize" button in chat UI
    2. Popup opens this URL via window.open() with auth params
    3. User is redirected to OAuth provider consent screen
    4. After consent, redirected back to /callback
    5. Callback page posts message to opener and closes
    
    Authentication for popup flow:
    Since window.open() creates a new browser context that can't inherit
    Authorization headers from the parent page, auth credentials must be
    passed as query parameters:
    - token: JWT or service account bearer token
    - api_key: API key authentication
    - principal_id + tenant_id: Dev mode header simulation
    
    Security considerations:
    - Auth tokens in URL are logged but short-lived and single-use for redirect
    - OAuth state parameter prevents CSRF during callback
    - User must explicitly consent at OAuth provider
    - Tokens should be short-lived; consider using session cookies for production
    
    Args:
        provider: Provider identifier (e.g., "google_workspace", "github")
        request: FastAPI request for building callback URI
        token: JWT or service account token (query param for popup)
        api_key: API key (query param for popup)
        principal_id: Principal ID for dev mode
        tenant_id: Tenant ID for dev mode
        
    Returns:
        302 redirect to OAuth provider authorization URL
    """
    from fastapi.responses import RedirectResponse
    from ....core.security.oauth_manager import get_oauth_manager
    from ....core.config import Config
    
    # Extract principal from query params or headers
    # Priority: token > api_key > dev mode headers > query params
    # Query tenant_id is ignored once a JWT / service-account principal is
    # resolved — it cannot override a real token's tenant (issue #214).
    principal = None
    
    # Try to get principal from standard headers first
    try:
        principal = await get_current_principal(request)
    except HTTPException:
        pass  # No auth in headers, try query params
    
    # If no principal from headers, try query params
    if not principal:
        cfg = Config()
        
        if token:
            # Validate JWT/service account token
            # For now, trust the token and extract claims
            # In production, this should validate the token properly
            try:
                import jwt as pyjwt
                from ....core.security.auth import extract_principal_from_claims
                
                # Try to decode without verification to get principal_id
                # The actual OAuth provider will validate permissions
                decoded = pyjwt.decode(token, options={"verify_signature": False})
                
                # Use the standard auth helper to extract principal from claims
                # This ensures consistent tenant extraction logic (organization claim, etc.)
                principal = extract_principal_from_claims(decoded, cfg)
                
                logger.info("OAuth popup auth via token", 
                           principal_id=principal.id, 
                           tenant_id=principal.tenant_id,
                           org_claim=decoded.get("organization"))
            except Exception as e:
                logger.warning("Failed to decode token for OAuth popup", error=str(e))
        
        elif api_key:
            # API key auth - create minimal principal
            # In production, validate API key against database
            principal = Principal(
                id=f"api-key-user",
                tenant_id="default",
                roles=[]
            )
            logger.info("OAuth popup auth via API key")
        
        elif principal_id:
            if not cfg.allow_insecure_principal_headers:
                raise HTTPException(
                    status_code=401,
                    detail="principal_id query param requires MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS=true"
                )
            principal = Principal(
                id=principal_id,
                tenant_id=tenant_id or "default",
                roles=[]
            )
            logger.info("OAuth popup auth via dev mode query params", principal_id=principal.id)
    
    if not principal:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Pass token, api_key, or principal_id query params for popup flow."
        )
    
    base_url = str(request.base_url).rstrip('/')
    redirect_uri = f"{base_url}/api/v1/oauth/{provider}/callback"
    
    try:
        oauth_manager = get_oauth_manager()
        auth_url, state = await oauth_manager.initiate_oauth(
            server_id=provider,
            redirect_uri=redirect_uri,
            principal_id=principal.id,
            tenant_id=principal.tenant_id,
            motet_id=principal.motet_id,
            conversation_id=conversation_id,
            task_id=task_id
        )
        
        logger.info("OAuth initiate (browser) - redirecting to provider",
                   provider=provider,
                   principal_id=principal.id,
                   tenant_id=principal.tenant_id,
                   conversation_id=conversation_id)
        return RedirectResponse(url=auth_url, status_code=302)
        
    except ValueError as e:
        logger.warning("OAuth initiation failed - provider not configured",
                     provider=provider,
                     error=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("OAuth initiation failed",
                    provider=provider,
                    error=str(e),
                    exc_info=True)
        raise HTTPException(status_code=500, detail=f"OAuth initiation failed: {str(e)}")


@router.post(
    "/{provider}/initiate",
    summary="Initiate OAuth flow (API/programmatic)",
    description="Start OAuth flow via API. Returns authorization URL as JSON for client to handle. "
                "Used by backend services, CLI tools, or custom integrations.",
    response_model=OAuthInitiateResponse,
    response_description="OAuth authorization URL and state"
)
async def oauth_initiate_api(
    provider: str,
    request: Request,
    principal: Principal = Depends(get_current_principal)
) -> OAuthInitiateResponse:
    """
    Initiate OAuth flow for API/programmatic use case.
    
    This endpoint is designed for programmatic OAuth flows:
    - Backend services that need to orchestrate OAuth
    - CLI tools that will open the URL in user's browser
    - Custom integrations that handle the redirect themselves
    
    The client receives the authorization URL and is responsible for:
    1. Directing the user to the URL (open browser, display link, etc.)
    2. Handling the callback (if using custom redirect_uri)
    
    Args:
        provider: Provider identifier (e.g., "google_workspace", "github")
        request: FastAPI request for building callback URI
        principal: Authenticated user/service (required)
        
    Returns:
        OAuthInitiateResponse with authorization_url, state, and instructions
    """
    from ....core.security.oauth_manager import get_oauth_manager
    
    base_url = str(request.base_url).rstrip('/')
    redirect_uri = f"{base_url}/api/v1/oauth/{provider}/callback"
    
    try:
        oauth_manager = get_oauth_manager()
        auth_url, state = await oauth_manager.initiate_oauth(
            server_id=provider,
            redirect_uri=redirect_uri,
            principal_id=principal.id,
            tenant_id=principal.tenant_id,
            motet_id=principal.motet_id
        )
        
        logger.info("OAuth initiate (API) - returning authorization URL",
                   provider=provider,
                   principal_id=principal.id,
                   tenant_id=principal.tenant_id)
        
        return OAuthInitiateResponse(
            authorization_url=auth_url,
            state=state,
            instructions=f"Visit the URL above to authorize {provider}. You will be redirected back to this server upon completion."
        )
        
    except ValueError as e:
        logger.warning("OAuth initiation failed - provider not configured",
                     provider=provider,
                     error=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("OAuth initiation failed",
                    provider=provider,
                    error=str(e),
                    exc_info=True)
        raise HTTPException(status_code=500, detail=f"OAuth initiation failed: {str(e)}")


@router.get(
    "/{provider}/callback",
    summary="OAuth callback",
    description="Handle OAuth callback from provider after user authorization",
    response_description="HTML page showing success or error"
)
async def oauth_callback(
    request: Request,
    provider: str,
    code: str = Query(..., description="Authorization code from OAuth provider"),
    state: str = Query(..., description="CSRF protection state parameter"),
    background_tasks: BackgroundTasks = BackgroundTasks()
) -> HTMLResponse:
    """
    OAuth callback endpoint - handles authorization code from provider.
    
    This endpoint is called by the OAuth provider after user grants permissions.
    It exchanges the authorization code for access tokens and stores them securely
    in the vault.
    
    Note: This endpoint does not require authentication as it's called by the
    external OAuth provider. CSRF protection is handled via the state parameter.
    
    Args:
        provider: Provider identifier (e.g., "mcp/google_workspace", "slack")
        code: Authorization code from OAuth provider
        state: State parameter for CSRF validation
        request: FastAPI request object for building redirect URI
        
    Returns:
        HTMLResponse with success or error page
        
    Raises:
        HTTPException: 400 if callback fails
    """
    from ....core.security.oauth_manager import get_oauth_manager
    
    # Build redirect URI (must match initiate)
    base_url = str(request.base_url).rstrip('/') if request else "http://localhost:8000"
    redirect_uri = f"{base_url}/api/v1/oauth/{provider}/callback"
    
    try:
        oauth_manager = get_oauth_manager()
        
        # Exchange code for tokens - returns (tokens, state_data) tuple
        tokens, state_data = await oauth_manager.complete_oauth(
            server_id=provider,  # OAuthManager uses server_id, but we're calling it provider now
            code=code,
            state=state,
            redirect_uri=redirect_uri
        )
        
        # Extract principal/tenant/motet from state data for per-user token storage
        principal_id = state_data.get("principal_id")
        tenant_id = state_data.get("tenant_id")
        motet_id = state_data.get("motet_id")
        conversation_id = state_data.get("conversation_id")
        task_id = state_data.get("task_id")
        
        # Log state_data extraction for debugging
        logger.debug("OAuth callback: Extracted state_data",
                    principal_id=principal_id,
                    tenant_id=tenant_id,
                    motet_id=motet_id,
                    conversation_id=conversation_id,
                    task_id=task_id)
        
        # Ensure we have at least default values for memory storage
        # Note: principal_id can be None (global tokens), but tenant_id and motet_id should have defaults
        if not tenant_id:
            tenant_id = SYSTEM_TENANT_ID
        if not motet_id:
            motet_id = SYSTEM_MOTET_ID
        
        # Tokens are stored inside OAuthManager.complete_oauth() (commit point) which also emits
        # mcp.auth_updated (ADR-0057). We keep this log for request-level visibility.
        logger.info(
            "OAuth tokens stored with user scoping",
            provider=provider,
            principal_id=principal_id or SYSTEM_PRINCIPAL_OAUTH_API,
            tenant_id=tenant_id or SYSTEM_TENANT_ID,
            motet_id=motet_id or SYSTEM_MOTET_ID,
            conversation_id=conversation_id,
        )
        
        # Get display name for UI (ADR-0057)
        display_name = _get_provider_display_name(provider)
        
        # Store conversation memory if conversation_id is present
        # This ensures prepare_context includes the login success in conversation history
        if conversation_id:
            # Create a conversation turn memory indicating successful login
            login_message = f"User successfully logged in to {display_name}. OAuth credentials have been stored and are ready to use."
            conversation_content = f"User: Login to {display_name}\nAssistant: {login_message}"
            
            # Store via background task using distributed command
            background_tasks.add_task(
                _store_oauth_login_memory,
                conversation_content=conversation_content,
                conversation_id=conversation_id,
                task_id=task_id,
                principal_id=principal_id,
                tenant_id=tenant_id or SYSTEM_TENANT_ID,
                motet_id=motet_id or SYSTEM_MOTET_ID,
                provider=provider,
                display_name=display_name
            )
            
            logger.info(
                "OAuth login success memory queued for storage",
                provider=provider,
                conversation_id=conversation_id,
                task_id=task_id,
                principal_id=principal_id,
                tenant_id=tenant_id or SYSTEM_TENANT_ID,
                motet_id=motet_id or SYSTEM_MOTET_ID
            )
        
        # Return success page with postMessage for popup communication (ADR-0057)
        html_content = _templates.get_template("oauth_success.html").render(
            request=request,
            provider=provider,
            display_name=display_name
        )
        return HTMLResponse(content=html_content, background=background_tasks)
        
    except Exception as e:
        logger.error("OAuth callback failed",
                    provider=provider,
                    error=str(e),
                    exc_info=True)
        
        # Return error page with postMessage for popup communication (ADR-0057)
        # Escape error for safe HTML/JS embedding
        safe_error = str(e).replace("'", "\\'").replace('"', '\\"').replace('\n', ' ')
        html_content = _templates.get_template("oauth_error.html").render(
            request=request,
            provider=provider,
            error=safe_error
        )
        return HTMLResponse(content=html_content, status_code=400)


@router.get(
    "/{provider}/status",
    summary="Get OAuth status",
    description="Get OAuth authentication status for a provider",
    response_model=OAuthStatusResponse,
    response_description="OAuth status information"
)
async def oauth_status(
    provider: str,
    principal: Principal = Depends(get_current_principal)
) -> OAuthStatusResponse:
    """
    Get OAuth status for a provider.
    
    Returns whether the provider is configured and authenticated, including
    token expiration information if available.
    
    Args:
        provider: Provider identifier (e.g., "mcp/google_workspace", "slack")
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        OAuthStatusResponse with status information
        
    Raises:
        HTTPException: 400 if status check fails
    """
    from ....core.security.oauth_manager import get_oauth_manager
    
    try:
        oauth_manager = get_oauth_manager()
        status = await oauth_manager.get_oauth_status(
            server_id=provider,
            principal_id=principal.id,
            tenant_id=principal.tenant_id,
            motet_id=principal.motet_id
        )
        
        # Ensure server_id is always set (use provider if not in status)
        server_id = status.get("server_id") or provider
        
        return OAuthStatusResponse(
            provider=provider,
            server_id=server_id,  # Include server_id for backward compatibility
            configured=status.get("configured", False),
            authenticated=status.get("authenticated", False),
            expires_at=status.get("expires_at"),
            is_expired=status.get("is_expired"),
            needs_reauth=status.get("needs_reauth"),
            scopes=status.get("scopes"),
            has_refresh_token=status.get("has_refresh_token")
        )
    except Exception as e:
        logger.error("OAuth status check failed",
                    provider=provider,
                    principal_id=principal.id,
                    tenant_id=principal.tenant_id,
                    error=str(e),
                    exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post(
    "/{provider}/refresh",
    summary="Refresh OAuth tokens",
    description="Manually refresh OAuth tokens for a provider",
    response_model=OAuthRefreshResponse,
    response_description="Token refresh result"
)
async def oauth_refresh(
    provider: str,
    principal: Principal = Depends(get_current_principal)
) -> OAuthRefreshResponse:
    """
    Manually refresh OAuth tokens for a provider.
    
    Normally tokens are refreshed automatically, but this endpoint can be used
    to force a refresh. Useful for troubleshooting or when automatic refresh fails.
    
    Args:
        provider: Provider identifier (e.g., "mcp/google_workspace", "slack")
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        OAuthRefreshResponse with refresh result
        
    Raises:
        HTTPException: 400 if refresh fails
    """
    from ....core.security.oauth_manager import get_oauth_manager
    
    try:
        oauth_manager = get_oauth_manager()
        tokens = await oauth_manager.refresh_tokens(
            server_id=provider,
            principal_id=principal.id,
            tenant_id=principal.tenant_id,
            motet_id=principal.motet_id
        )
        
        return OAuthRefreshResponse(
            success=True,
            expires_at=tokens.get("expires_at"),
            message="Tokens refreshed successfully"
        )
    except Exception as e:
        logger.error("OAuth token refresh failed",
                    provider=provider,
                    principal_id=principal.id,
                    tenant_id=principal.tenant_id,
                    error=str(e),
                    exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.delete(
    "/{provider}/revoke",
    summary="Revoke OAuth credentials",
    description="Revoke/disconnect OAuth credentials for a provider. "
                "This removes the stored tokens from the vault and optionally "
                "revokes them with the OAuth provider.",
    response_model=OAuthRevokeResponse,
    response_description="Revocation result"
)
async def oauth_revoke(
    provider: str,
    revoke_at_provider: bool = Query(
        default=False,
        description="Whether to also revoke the token at the OAuth provider (e.g., Google). "
                    "If False, only removes from local vault storage."
    ),
    principal: Principal = Depends(get_current_principal)
) -> OAuthRevokeResponse:
    """
    Revoke/disconnect OAuth credentials for a provider.
    
    This endpoint is useful for:
    - Testing the OAuth flow from scratch
    - Disconnecting a service the user no longer wants connected
    - Switching to a different account
    - Security purposes (e.g., compromised token)
    
    Args:
        provider: Provider identifier (e.g., "google_workspace", "slack")
        revoke_at_provider: If True, also calls the provider's revoke endpoint
                           to invalidate the token at the source
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        OAuthRevokeResponse with revocation result
        
    Raises:
        HTTPException: 400 if revocation fails, 404 if no credentials found
    """
    from ....core.security.oauth_manager import get_oauth_manager
    
    try:
        oauth_manager = get_oauth_manager()
        
        # Use shared revoke_credentials method (handles deletion, provider revocation, and events)
        result = await oauth_manager.revoke_credentials(
            server_id=provider,
            principal_id=principal.id,
            tenant_id=principal.tenant_id,
            motet_id=principal.motet_id,
            revoke_at_provider=revoke_at_provider
        )
        
        if not result.get("success"):
            if "No OAuth credentials found" in result.get("message", ""):
                raise HTTPException(
                    status_code=404,
                    detail=result["message"]
                )
            raise HTTPException(
                status_code=400,
                detail=result["message"]
            )
        
        return OAuthRevokeResponse(
            success=True,
            provider=provider,
            message=result["message"]
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("OAuth credential revocation failed",
                    provider=provider,
                    error=str(e),
                    exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))




# ============================================
# Helper Functions for OAuth Callback (ADR-0057)
# ============================================

# Provider display names for user-friendly UI
def _get_provider_display_name(provider: str) -> str:
    """
    Get user-friendly display name for a provider from YAML config.
    
    Falls back to formatted provider name if not found in config.
    """
    from motet.core.tools.mcp_motet.proxy.mcp_instance_manager import get_oauth_providers_from_config
    
    providers = get_oauth_providers_from_config()
    provider_config = providers.get(provider)
    
    if provider_config and provider_config.get("display_name"):
        return provider_config["display_name"]
    
    # Fallback to formatted name
    return provider.replace("_", " ").title()



