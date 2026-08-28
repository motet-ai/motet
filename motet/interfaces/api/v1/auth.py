"""
Motet - User Authentication API

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    User authentication API for OAuth flows with Keycloak.
    Provides endpoints for initiating OAuth login and handling callbacks.
    Separate from MCP OAuth (which is in oauth.py).

Dependencies:
    - fastapi: Web framework for REST API
    - motet.core.config: Configuration
    - motet.core.security.auth: Principal extraction
    - secrets: CSRF protection

Usage:
    from motet.interfaces.api.v1.auth import router
    
    # Include in FastAPI app
    app.include_router(router)

Notes:
    - Uses Keycloak for OAuth provider
    - Implements PKCE flow for security
    - Stores OAuth state in Redis for CSRF protection
    - Stores refresh tokens in Redis keyed by principal + JWT tenant
    - Login, callback, and check use the sync Redis client so ASGI tests
      are not bound to a previous event loop
    - Refresh and logout look up that tenant key, then the logical and
      ``motet:`` names. No keyspace SCAN.
    - Part of Phase 3: Demo Chat JWT + API Updates
"""

import os
import secrets
import hashlib
import base64
from pathlib import Path
from typing import Optional, Dict, Any
from urllib.parse import urlencode
from fastapi import APIRouter, HTTPException, Query, Request, Depends, status
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
import structlog
import httpx

from ..shared.auth import get_current_principal
from ....core.types import Principal
from ....core.config import Config
from ....core.distributed.redis_manager import get_redis_client, get_sync_redis_client
from ....core.distributed.tenant_keys import (
    delete_candidate_keys,
    first_existing_key,
    maybe_tenant_key,
    product_key,
)
from ....core.security.auth import extract_principal_from_claims

logger = structlog.get_logger(__name__)

# Initialize templates
_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent.parent / "templates"))

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _refresh_token_logical(principal_id: str) -> str:
    return f"auth:refresh_token:{principal_id}"


def _refresh_token_key(principal_id: str, tenant_id: Optional[str] = None) -> str:
    return maybe_tenant_key(tenant_id, _refresh_token_logical(principal_id))


def _refresh_token_lookup_keys(principal_id: str, tenant_id: Optional[str] = None) -> list[str]:
    logical = _refresh_token_logical(principal_id)
    keys: list[str] = []
    if tenant_id:
        keys.append(_refresh_token_key(principal_id, tenant_id))
    keys.append(logical)
    keys.append(product_key(logical))
    return keys


def _request_base_url(request: Optional[Request]) -> str:
    """
    Build the public base URL for this request.
    When behind a reverse proxy (e.g. nginx), use X-Forwarded-Proto and X-Forwarded-Host
    so the callback URL matches what the browser sees (e.g. https://...) and Keycloak's
    valid redirect URIs.
    """
    if not request:
        return "http://localhost:8000"
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").strip().lower()
    raw_host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").strip()
    # Strip default port so https://host:443 and http://host:80 match Keycloak patterns
    if raw_host and ":" in raw_host:
        host_part, _, port_part = raw_host.rpartition(":")
        if (forwarded_proto == "https" and port_part == "443") or (
            forwarded_proto == "http" and port_part == "80"
        ):
            raw_host = host_part
    host = raw_host
    if forwarded_proto and host:
        return f"{forwarded_proto}://{host}".rstrip("/")
    return str(request.base_url).rstrip("/")


def _generate_pkce_pair() -> tuple[str, str]:
    """Generate PKCE code verifier and challenge."""
    # Generate random code verifier (43-128 characters)
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('utf-8').rstrip('=')
    
    # Generate code challenge (SHA256 hash of verifier)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode('utf-8')).digest()
    ).decode('utf-8').rstrip('=')
    
    return code_verifier, code_challenge


def _resolve_keycloak_endpoints(cfg: Config) -> Dict[str, str]:
    """
    Derive browser-facing and internal Keycloak endpoints from configuration.
    
    Returns dict with keys:
        realm: Keycloak realm name
        issuer: Configured issuer (used for validation)
        browser_realm_base: Base URL for browser redirects (e.g., http://localhost:8080/realms/motet)
        internal_realm_base: Base URL for service-to-service calls (e.g., http://keycloak:8080/realms/motet)
    """
    issuer = (cfg.jwt_issuer or "http://localhost:8080/realms/motet").rstrip('/')
    if "/realms/" not in issuer:
        raise HTTPException(
            status_code=500,
            detail="JWT issuer must include /realms/<name> when using Keycloak."
        )
    
    issuer_base, realm_segment = issuer.split("/realms/", 1)
    realm = realm_segment.split("/")[0].strip()
    if not realm:
        raise HTTPException(
            status_code=500,
            detail="Unable to determine Keycloak realm name from JWT issuer."
        )
    
    browser_base = (getattr(cfg, "keycloak_public_url", None) or issuer_base).rstrip('/')
    jwks_url = cfg.jwt_jwks_url or ""
    if "/realms/" in jwks_url:
        internal_base = jwks_url.split("/realms/")[0]
    else:
        internal_base = issuer_base
    internal_base = internal_base.rstrip('/')
    
    return {
        "realm": realm,
        "issuer": issuer,
        "browser_realm_base": f"{browser_base}/realms/{realm}",
        "internal_realm_base": f"{internal_base}/realms/{realm}"
    }


@router.get("/login")
async def initiate_login(
    request: Request,
    redirect_uri: Optional[str] = Query(None, description="Redirect URI after login"),
) -> RedirectResponse:
    """
    Initiate OAuth login flow with Keycloak.
    
    Redirects user to the Keycloak authorization endpoint with PKCE.
    After authentication, Keycloak redirects to /api/v1/auth/callback.
    
    Args:
        redirect_uri: Optional redirect URI after successful login (defaults to Chat Explorer)
        request: FastAPI request object
        
    Returns:
        RedirectResponse to Keycloak authorization endpoint
    """
    try:
        cfg = Config()
        if not cfg.jwt_jwks_url:
            raise HTTPException(
                status_code=503,
                detail="JWT authentication not configured. Please set MOTET_JWT_JWKS_URL.",
            )
        keycloak = _resolve_keycloak_endpoints(cfg)
        client_id = getattr(cfg, "keycloak_client_id", None) or "motet-ai-stack"
        base_url = _request_base_url(request)
        callback_uri = f"{base_url}/api/v1/auth/callback"
        logger.info(
            "OAuth callback URL",
            base_url=base_url,
            callback_uri=callback_uri,
            x_forwarded_proto=request.headers.get("x-forwarded-proto") if request else None,
            host=request.headers.get("host") if request else None,
        )
        code_verifier, code_challenge = _generate_pkce_pair()
        state = secrets.token_urlsafe(32)
        sync_redis = get_sync_redis_client("auth")
        state_key = product_key(f"auth:oauth_state:{state}")
        sync_redis.setex(state_key, 600, code_verifier)
        if redirect_uri:
            redirect_key = product_key(f"auth:oauth_redirect:{state}")
            sync_redis.setex(redirect_key, 600, redirect_uri)
        auth_url = f"{keycloak['browser_realm_base']}/protocol/openid-connect/auth"
        params = {
            "client_id": client_id,
            "redirect_uri": callback_uri,
            "response_type": "code",
            "scope": "openid profile email organization",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        auth_url_with_params = f"{auth_url}?{urlencode(params)}"
        logger.info("Initiating OAuth login", keycloak_base=keycloak["browser_realm_base"], client_id=client_id)
        return RedirectResponse(url=auth_url_with_params)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Login initiation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Login initiation failed: {e!s}. Check Redis (MOTET_REDIS_URL) and Keycloak config (MOTET_JWT_*, keycloak_public_url).",
        ) from e


@router.get("/callback")
async def oauth_callback(
    request: Request,
    code: str = Query(..., description="Authorization code from Keycloak"),
    state: str = Query(..., description="CSRF protection state parameter"),
) -> HTMLResponse:
    """
    Handle OAuth callback from Keycloak.
    
    Exchanges authorization code for JWT token and returns it to the frontend
    via JavaScript so the Chat Explorer UI can store it securely.
    """
    cfg = Config()
    
    # Validate state (sync client: ASGI tests must not reuse a closed event loop)
    sync_redis = get_sync_redis_client("auth")
    state_key = product_key(f"auth:oauth_state:{state}")
    code_verifier = sync_redis.get(state_key)
    
    if not code_verifier:
        logger.warning("Invalid or expired OAuth state", state=state)
        html_content = _templates.get_template("auth_error.html").render(
            request=request,
            error_message="Invalid or expired authentication state. Please try logging in again.",
            error_details=None,
            close_delay=3000
        )
        return HTMLResponse(content=html_content, status_code=400)
    
    # Decode code_verifier if it's bytes
    if isinstance(code_verifier, bytes):
        code_verifier_str = code_verifier.decode('utf-8')
    else:
        code_verifier_str = code_verifier
    
    # Delete state (one-time use)
    sync_redis.delete(state_key)
    
    redirect_uri = "/chat-explorer"
    redirect_key = product_key(f"auth:oauth_redirect:{state}")
    stored = sync_redis.get(redirect_key)
    if stored:
        sync_redis.delete(redirect_key)
        redirect_uri = stored.decode("utf-8") if isinstance(stored, bytes) else stored
    
    keycloak = _resolve_keycloak_endpoints(cfg)
    client_id = getattr(cfg, "keycloak_client_id", None) or "motet-ai-stack"
    
    # Build callback URL (must match what was sent to Keycloak in /login)
    base_url = _request_base_url(request)
    callback_uri = f"{base_url}/api/v1/auth/callback"
    
    token_url = f"{keycloak['internal_realm_base']}/protocol/openid-connect/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    
    try:
        async with httpx.AsyncClient() as client:
            token_response = await client.post(
                token_url,
                data={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": code,
                    "redirect_uri": callback_uri,
                    "code_verifier": code_verifier_str
                },
                headers=headers,
                timeout=10.0
            )
            token_response.raise_for_status()
            token_data = token_response.json()
    except httpx.HTTPStatusError as e:
        logger.error("Token exchange failed", status_code=e.response.status_code, error=e.response.text)
        html_content = _templates.get_template("auth_error.html").render(
            request=request,
            error_message="Failed to exchange authorization code for token. Please try again.",
            error_details=e.response.text,
            close_delay=5000
        )
        return HTMLResponse(content=html_content, status_code=400)
    except Exception as e:
        logger.error("Token exchange error", error=str(e), exc_info=True)
        html_content = _templates.get_template("auth_error.html").render(
            request=request,
            error_message=f"An error occurred during authentication: {str(e)}",
            error_details=None,
            close_delay=5000
        )
        return HTMLResponse(content=html_content, status_code=500)
    
    # Extract JWT tokens (prefer access token for API auth, keep ID token for UI claims)
    access_token = token_data.get("access_token")
    id_token = token_data.get("id_token")
    token_type = token_data.get("token_type")

    jwt_token = access_token or id_token
    if not jwt_token or "." not in jwt_token:
        logger.warning(
            "No usable JWT token in Keycloak response",
            has_access_token=bool(access_token),
            has_id_token=bool(id_token),
            token_type=token_type,
        )
    
    if not jwt_token:
        logger.error("No usable JWT token in response", token_data_keys=list(token_data.keys()))
        html_content = _templates.get_template("auth_error.html").render(
            request=request,
            error_message="No valid token received from Keycloak.",
            error_details=None,
            close_delay=3000
        )
        return HTMLResponse(content=html_content, status_code=400)
    
    # Store refresh token in Redis (keyed by principal ID from JWT)
    # This allows us to refresh tokens later without exposing refresh token to frontend
    refresh_token = token_data.get("refresh_token")
    if refresh_token:
        try:
            import jwt  # type: ignore
            # Decode JWT to get principal ID (sub claim)
            decoded = jwt.decode(jwt_token, options={"verify_signature": False})  # type: ignore
            try:
                principal = extract_principal_from_claims(decoded, cfg)
                principal_id = principal.id
                tenant_id = principal.tenant_id or ""
            except Exception:
                principal_id = decoded.get("sub") or decoded.get("preferred_username") or "unknown"
                tenant_id = (
                    decoded.get("tid")
                    or decoded.get("tenant_id")
                    or decoded.get("tenant")
                    or decoded.get("org")
                    or ""
                )
            
            # Store refresh token in Redis with TTL matching Keycloak refresh token expiry
            # Default Keycloak refresh token expiry is typically 30 days, but we'll use 7 days as safe default
            refresh_token_ttl = 7 * 24 * 60 * 60  # 7 days in seconds
            refresh_token_key = _refresh_token_key(str(principal_id), str(tenant_id) if tenant_id else None)
            
            # Store refresh token data (including expires_in if provided)
            refresh_token_data = {
                "refresh_token": refresh_token,
                "expires_in": token_data.get("expires_in", 3600),  # Access token expiry
                "refresh_expires_in": token_data.get("refresh_expires_in", refresh_token_ttl)  # Refresh token expiry
            }
            import json
            sync_redis.setex(
                refresh_token_key,
                refresh_token_ttl,
                json.dumps(refresh_token_data)
            )
            
            logger.info("Stored refresh token in Redis", principal_id=principal_id[:8] + "...")
        except Exception as e:
            logger.warning("Failed to store refresh token", error=str(e), exc_info=True)
            # Continue even if refresh token storage fails - user can still use current token
    
    # Return HTML page that stores token and redirects
    html_content = _templates.get_template("auth_success.html").render(
        request=request,
        jwt_token=jwt_token,
        access_token=access_token,
        id_token=id_token,
        redirect_uri=redirect_uri,
    )
    
    logger.info("OAuth callback successful", redirect_uri=redirect_uri)
    
    return HTMLResponse(content=html_content)


@router.get("/check")
async def auth_check() -> Dict[str, Any]:
    """
    Preflight check for auth/login. No auth required.
    Returns status of JWT config, Keycloak resolution, and Redis connectivity
    so CLI/users can see why login might fail (500).
    """
    result: Dict[str, Any] = {
        "login_ready": False,
        "jwt_configured": False,
        "keycloak_resolved": False,
        "redis_ok": False,
        "errors": [],
    }
    cfg = Config()
    result["jwt_configured"] = bool(cfg.jwt_jwks_url)
    if not cfg.jwt_jwks_url:
        result["errors"].append("MOTET_JWT_JWKS_URL is not set (required for OAuth login)")
    else:
        try:
            _resolve_keycloak_endpoints(cfg)
            result["keycloak_resolved"] = True
        except HTTPException as e:
            result["errors"].append(f"Keycloak config: {e.detail}")
        except Exception as e:
            result["errors"].append(f"Keycloak config: {e!s}")
    try:
        sync_redis = get_sync_redis_client("auth")
        test_key = product_key("auth:check:ping")
        sync_redis.setex(test_key, 10, "ok")
        val = sync_redis.get(test_key)
        sync_redis.delete(test_key)
        result["redis_ok"] = val in ("ok", b"ok")
        if not result["redis_ok"]:
            result["errors"].append("Redis setex/get failed (check MOTET_REDIS_URL and that Redis is running)")
    except Exception as e:
        result["errors"].append(f"Redis: {e!s}. Is Redis running? MOTET_REDIS_URL default: redis://localhost:6379/0")
    result["login_ready"] = result["jwt_configured"] and result["keycloak_resolved"] and result["redis_ok"]
    return result


@router.get("/cli-success")
async def cli_success_page(request: Request) -> HTMLResponse:
    """
    Page shown after OAuth when redirect_uri targets CLI success.
    Token is passed in URL fragment (#token=...) so it is not sent to the server.
    Page JS reads the fragment and shows the motet-cli auth store-token command.
    """
    html_content = _templates.get_template("auth_cli_success.html").render(request=request)
    return HTMLResponse(content=html_content)


@router.get("/debug/claims", response_model=None)
async def debug_claims(
    request: Request,
    principal: Principal = Depends(get_current_principal),
):
    """
    Debug endpoint to inspect JWT claims (requires MOTET_DEBUG_MODE=true and valid authentication).
    Useful for troubleshooting tenant claim extraction in development environments.
    """
    if not os.getenv("MOTET_DEBUG_MODE", "false").lower() == "true":
        raise HTTPException(status_code=403, detail="Debug mode not enabled. Set MOTET_DEBUG_MODE=true.")

    try:
        import jwt  # type: ignore
    except ImportError:
        raise HTTPException(status_code=500, detail="PyJWT not installed")
    
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=400, detail="No JWT token in Authorization header.")
    
    token = auth.removeprefix("Bearer ").strip()
    
    try:
        decoded = jwt.decode(token, options={"verify_signature": False})  # type: ignore
        
        cfg = Config()
        principal_info = {
            "id": principal.id,
            "tenant_id": principal.tenant_id,
            "roles": principal.roles,
        }
        
        tenant_keys = [k.strip() for k in (getattr(cfg, "jwt_tenant_claims", "tid,org,tenant,tenant_id,org_id,organization") or "tid").split(",") if k.strip()]
        org_claim = (getattr(cfg, "jwt_organization_claim", "") or "").strip()
        if org_claim and org_claim not in tenant_keys:
            tenant_keys.append(org_claim)
        
        tenant_claim_values = {}
        for key in tenant_keys:
            if key in decoded:
                tenant_claim_values[key] = decoded[key]
        
        tenant_candidate = None
        tenant_origin = None
        try:
            from ....core.security.auth import _extract_tenant_candidate  # type: ignore
            tenant_candidate, tenant_origin = _extract_tenant_candidate(cfg, decoded, tenant_keys)  # type: ignore[attr-defined]
        except Exception as e:
            tenant_origin = f"error: {e}"
        
        motet_keys = [k.strip() for k in (getattr(cfg, "jwt_motet_claims", "motet_id,motet,environment,env,deployment") or "motet_id").split(",") if k.strip()]
        motet_claim_values = {}
        for key in motet_keys:
            if key in decoded:
                motet_claim_values[key] = decoded[key]
        
        return {
            "all_claim_keys": list(decoded.keys()),
            "principal_extraction": principal_info,
            "tenant_claim_values": tenant_claim_values,
            "tenant_keys_checked": tenant_keys,
            "tenant_candidate": tenant_candidate,
            "tenant_candidate_origin": tenant_origin,
            "motet_claim_values": motet_claim_values,
            "motet_keys_checked": motet_keys,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("debug_claims failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to decode JWT claims.")


async def _get_identity_for_refresh(request: Request) -> tuple[str, Optional[str]]:
    """
    Return (principal_id, tenant_id) for the refresh endpoint.

    Tries full JWT validation first; if that fails (e.g. expired token),
    decodes the JWT without verifying exp and uses the same tenant claim
    path as login so the Redis key matches the callback write.
    """
    try:
        principal = await get_current_principal(request)
        return principal.id, (principal.tenant_id or None)
    except HTTPException:
        pass
    auth_header = request.headers.get("Authorization") or ""
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please provide a valid JWT token."
        )
    token = auth_header.removeprefix("Bearer ").strip()
    if not token or "." not in token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please provide a valid JWT token."
        )
    try:
        import jwt  # type: ignore
        cfg = Config()
        decoded = jwt.decode(
            token,
            options={"verify_signature": False, "verify_exp": False},
        )
        try:
            principal = extract_principal_from_claims(decoded, cfg)
            return principal.id, (principal.tenant_id or None)
        except Exception:
            sub_claim = getattr(cfg, "jwt_sub_claim", None) or "sub"
            principal_id = decoded.get(sub_claim) or decoded.get("sub")
            if not principal_id:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token missing subject. Please log in again."
                )
            return str(principal_id), None
    except HTTPException:
        raise
    except Exception as e:
        logger.debug("Failed to decode JWT for refresh", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please provide a valid JWT token."
        )


@router.post("/refresh")
async def refresh_token(
    request: Request
) -> Dict[str, Any]:
    """
    Refresh JWT token using stored refresh token.
    
    Uses the refresh token stored during OAuth callback to get new tokens from Keycloak.
    The refresh token is stored in Redis keyed by principal ID from the current JWT.
    Accepts an expired access token so the client can refresh after the popup completes.
    
    Returns:
        Dict with new JWT tokens (access_token/id_token)
        
    Raises:
        HTTPException: 401 if authentication fails or refresh token not found
        HTTPException: 400 if refresh fails
    """
    cfg = Config()
    
    principal_id, tenant_id = await _get_identity_for_refresh(request)
    
    # Look up refresh token from Redis (tenant key, then logical / motet:)
    redis_client = get_redis_client("auth")
    sync_redis = get_sync_redis_client("auth")
    refresh_token_key = first_existing_key(
        sync_redis, _refresh_token_lookup_keys(principal_id, tenant_id)
    ) or _refresh_token_key(principal_id, tenant_id)
    refresh_token_data_bytes = await redis_client.get(refresh_token_key)
    
    if not refresh_token_data_bytes:
        logger.warning("Refresh token not found", principal_id=principal_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token not found. Please log in again."
        )
    
    # Decode refresh token data
    import json
    if isinstance(refresh_token_data_bytes, bytes):
        refresh_token_data_str = refresh_token_data_bytes.decode('utf-8')
    else:
        refresh_token_data_str = refresh_token_data_bytes
    
    try:
        refresh_token_data = json.loads(refresh_token_data_str)
    except Exception as e:
        logger.error("Failed to parse refresh token data", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve refresh token."
        )
    
    refresh_token = refresh_token_data.get("refresh_token")
    if not refresh_token:
        logger.error("Refresh token missing from stored data", principal_id=principal_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token invalid. Please log in again."
        )
    
    # Exchange refresh token with Keycloak
    keycloak = _resolve_keycloak_endpoints(cfg)
    client_id = getattr(cfg, "keycloak_client_id", None) or "motet-ai-stack"
    token_url = f"{keycloak['internal_realm_base']}/protocol/openid-connect/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    
    try:
        async with httpx.AsyncClient() as client:
            token_response = await client.post(
                token_url,
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": refresh_token
                },
                headers=headers,
                timeout=10.0
            )
            token_response.raise_for_status()
            new_token_data = token_response.json()
    except httpx.HTTPStatusError as e:
        logger.error("Token refresh failed", status_code=e.response.status_code, error=e.response.text)
        # Clear invalid refresh token from Redis
        await redis_client.delete(refresh_token_key)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Token refresh failed: {e.response.text}"
        )
    except Exception as e:
        logger.error("Token refresh error", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Token refresh error: {str(e)}"
        )
    
    # Extract new JWT token (prefer access token for API auth, keep ID token for UI claims)
    new_access_token = new_token_data.get("access_token")
    new_id_token = new_token_data.get("id_token")
    new_jwt_token = new_access_token or new_id_token
    
    if not new_jwt_token:
        logger.error("No usable JWT token in refresh response", token_data_keys=list(new_token_data.keys()))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No valid token received from Keycloak."
        )
    
    # Update refresh token in Redis if a new one was provided (token rotation)
    new_refresh_token = new_token_data.get("refresh_token")
    if new_refresh_token:
        # Update stored refresh token data
        new_refresh_token_data = {
            "refresh_token": new_refresh_token,
            "expires_in": new_token_data.get("expires_in", 3600),
            "refresh_expires_in": new_token_data.get("refresh_expires_in", 7 * 24 * 60 * 60)
        }
        refresh_token_ttl = new_refresh_token_data.get("refresh_expires_in", 7 * 24 * 60 * 60)
        await redis_client.setex(
            refresh_token_key,
            refresh_token_ttl,
            json.dumps(new_refresh_token_data)
        )
        logger.info("Updated refresh token in Redis", principal_id=principal_id[:8] + "...")
    
    logger.info("Token refresh successful", principal_id=principal_id[:8] + "...")
    
    return {
        "access_token": new_access_token,
        "id_token": new_id_token,
        "token": new_jwt_token,  # For backward compatibility
        "expires_in": new_token_data.get("expires_in", 3600),
        "token_type": new_token_data.get("token_type", "Bearer")
    }


@router.get("/identity-provider-logout")
async def identity_provider_logout_url(
    request: Request,
    post_logout_redirect_uri: Optional[str] = Query(
        None,
        description="URI to redirect to after IdP logout (e.g. app origin); must match client's post logout redirect URIs in the identity provider.",
    ),
    id_token_hint: Optional[str] = Query(
        None,
        description="ID token from the current session; required by Keycloak (and some other IdPs) when using post_logout_redirect_uri. Client should pass this before clearing tokens.",
    ),
) -> Dict[str, Any]:
    """
    Return identity provider end-session URL for full SSO logout.
    
    When using an IdP (e.g. Keycloak), the app's logout only clears the app's tokens;
    the IdP session (cookie) remains, so the next login redirects back without prompting.
    Redirecting the browser to the URL returned here logs the user out of the IdP
    and then optionally back to post_logout_redirect_uri.
    
    Keycloak requires id_token_hint when post_logout_redirect_uri is used; the client
    should pass the current id_token (e.g. from localStorage) before clearing it.
    
    Currently implemented for Keycloak (OIDC end-session endpoint).
    
    Returns:
        {"url": "<idp logout url>"} when JWT/IdP is configured, else {"url": null}.
    """
    cfg = Config()
    if not cfg.jwt_jwks_url or "/realms/" not in (cfg.jwt_issuer or ""):
        return {"url": None}
    try:
        keycloak = _resolve_keycloak_endpoints(cfg)
    except HTTPException:
        return {"url": None}
    base = _request_base_url(request)
    redirect_uri = (post_logout_redirect_uri or base).strip()
    if not redirect_uri:
        redirect_uri = base
    logout_path = f"{keycloak['browser_realm_base']}/protocol/openid-connect/logout"
    params: Dict[str, str] = {}
    if redirect_uri:
        params["post_logout_redirect_uri"] = redirect_uri
    if id_token_hint:
        params["id_token_hint"] = id_token_hint
    url = f"{logout_path}?{urlencode(params)}" if params else logout_path
    return {"url": url}


@router.get("/logout")
async def logout(
    principal: Principal = Depends(get_current_principal)
) -> Dict[str, Any]:
    """
    Logout endpoint - clears refresh token from server.
    
    Clears the refresh token stored in Redis for this principal.
    Client should also clear JWT token from localStorage and then redirect to
    IdP end-session (GET /api/v1/auth/identity-provider-logout) for full SSO logout.
    
    Returns:
        Success message
    """
    logger.info("User logout", principal_id=principal.id)

    # Clear refresh token from Redis (sync client avoids async redis bound to another event loop in tests/ASGI)
    sync_redis = get_sync_redis_client("auth")
    delete_candidate_keys(
        sync_redis,
        _refresh_token_lookup_keys(principal.id, getattr(principal, "tenant_id", None)),
    )

    return {
        "status": "success",
        "message": "Logged out successfully. Please clear your JWT token from localStorage."
    }


# Export the router
__all__ = ["router"]

