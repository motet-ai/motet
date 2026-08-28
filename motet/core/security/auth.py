"""
Motet - Authentication Security

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-29

Description:
    Authentication security module for the Motet distributed framework.
    Provides comprehensive authentication capabilities including API key validation,
    JWT token handling with Keycloak support, service account tokens, and OAuth integration.
    Includes principal management, token caching, and distributed security coordination.

Dependencies:
    - fastapi: HTTP exception handling and request processing
    - typing: Type hints and annotations
    - Principal types and security definitions
    - redis: Service account token storage
    - jwt: JWT token verification

Usage:
    from motet.core.security.auth import require_api_key, extract_principal

    # Require API key
    require_api_key(config, request.headers.get("X-API-Key"))

    # Extract principal (supports JWT, service accounts, headers)
    principal = extract_principal(config, request)

Notes:
    - Provides comprehensive authentication capabilities
    - Includes API key validation and JWT token handling with signature verification
    - Supports Keycloak JWKS for public key discovery
    - Supports service account tokens for CLI/automation
    - Includes token caching and distributed security coordination
    - Supports comprehensive error handling and logging
    - Integrates with FastAPI and security system
    - Includes comprehensive observability and monitoring
"""

from __future__ import annotations

from typing import Optional, Any, Dict, List
from urllib.parse import urlparse
import hashlib
import os

from fastapi import HTTPException, Request
from ..types import Principal
from .tenant_mapping import resolve_tenant_id
from .system_principals import is_reserved_system_principal_id
from .ratelimit import RateLimiter

import structlog

logger = structlog.get_logger(__name__)

# Default claim keys for tenant extraction (used by both extract_principal and extract_principal_from_claims)
# These are checked in order; the first present value is used
DEFAULT_TENANT_CLAIMS = "tenant_id,tid,org_id,org,tenant"
DEFAULT_MOTET_CLAIMS = "motet_id,motet,environment,env,deployment"
AUTH_CACHE_ATTR = "_motet_auth_cache"
LOCAL_DEVELOPMENT_ENVIRONMENTS = {"local", "development", "dev", "test", "testing"}


def _resolve_motet_id(cfg) -> str:
    """Return the motet/environment identifier from config or env, defaulting to 'default'."""
    return getattr(cfg, "motet_id", None) or os.getenv("MOTET_MOTET_ID", "default")


def _is_local_development_environment(cfg) -> bool:
    environment = str(
        getattr(cfg, "deployment_environment", "development") or "development"
    ).strip().lower()
    return environment in LOCAL_DEVELOPMENT_ENVIRONMENTS


def validate_insecure_principal_header_policy(cfg) -> None:
    """Fail closed when insecure principal headers are enabled outside local/test."""
    if not getattr(cfg, "allow_insecure_principal_headers", False):
        return

    environment = str(
        getattr(cfg, "deployment_environment", "development") or "development"
    ).strip().lower()

    if _is_local_development_environment(cfg):
        logger.warning(
            "Insecure principal headers enabled for local environment",
            environment=environment,
        )
        return

    if getattr(cfg, "allow_insecure_principal_headers_in_non_dev", False):
        logger.warning(
            "Insecure principal headers explicitly enabled in non-development environment",
            environment=environment,
        )
        return

    logger.error(
        "Refusing insecure principal headers outside local development",
        environment=environment,
    )
    raise RuntimeError(
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS is only allowed in local/test "
        "environments unless MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS_IN_NON_DEV=true"
    )


def _get_cached_auth_result(request: Request) -> Optional[Dict[str, Any]]:
    try:
        cached = getattr(request.state, AUTH_CACHE_ATTR, None)
    except Exception:
        return None
    return cached if isinstance(cached, dict) and cached.get("computed") else None


def _set_cached_auth_result(
    request: Request,
    *,
    principal: Optional[Principal],
    failure: Optional[HTTPException],
) -> None:
    try:
        setattr(
            request.state,
            AUTH_CACHE_ATTR,
            {
                "computed": True,
                "principal": principal,
                "failure": failure,
            },
        )
    except Exception as e:
        logger.warning("auth_cache_state_update_failed", error=str(e))


def _get_client_ip(request: Request) -> str:
    try:
        forwarded_for = request.headers.get("X-Forwarded-For", "").strip()
        if forwarded_for:
            return forwarded_for.split(",", 1)[0].strip()
    except Exception as e:
        logger.warning("client_ip_forwarded_for_read_failed", error=str(e))

    try:
        if request.client and request.client.host:
            return str(request.client.host)
    except Exception as e:
        logger.warning("client_ip_client_host_read_failed", error=str(e))

    return "unknown"


def _token_fingerprint(token: str) -> str:
    if not token:
        return "missing"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _get_auth_failure_rate_limiter(cfg, request: Request) -> RateLimiter:
    try:
        cached = getattr(request.app.state, "_auth_failure_rate_limiter", None)
        if cached is not None:
            return cached
    except Exception as e:
        logger.warning("auth_rate_limiter_cache_read_failed", error=str(e))

    limiter = RateLimiter(
        backend=getattr(cfg, "rate_limit_backend", "memory"),
        redis_url=getattr(cfg, "redis_url", None),
        limit_per_minute=getattr(cfg, "auth_failure_limit_per_minute", None),
        window_seconds=int(getattr(cfg, "auth_failure_window_seconds", 60) or 60),
    )
    try:
        setattr(request.app.state, "_auth_failure_rate_limiter", limiter)
    except Exception as e:
        logger.warning("auth_rate_limiter_cache_write_failed", error=str(e))
    return limiter


def _build_auth_failure_exception(
    cfg,
    request: Request,
    *,
    auth_type: str,
    token: str,
    detail: str,
    reason: str,
) -> HTTPException:
    client_ip = _get_client_ip(request)
    token_id = _token_fingerprint(token)
    key = f"auth-failure:{auth_type}:{client_ip}:{token_id}"
    window_seconds = int(getattr(cfg, "auth_failure_window_seconds", 60) or 60)

    try:
        limiter = _get_auth_failure_rate_limiter(cfg, request)
        limiter.check(key)
    except HTTPException:
        logger.warning(
            "Authentication throttled",
            auth_type=auth_type,
            client_ip=client_ip,
            token_fingerprint=token_id,
            reason=reason,
        )
        return HTTPException(
            status_code=429,
            detail="too many failed authentication attempts",
            headers={"Retry-After": str(window_seconds)},
        )

    logger.warning(
        "Authentication failed",
        auth_type=auth_type,
        client_ip=client_ip,
        token_fingerprint=token_id,
        reason=reason,
    )
    return HTTPException(status_code=401, detail=detail)


def _reject_reserved_system_principal(
    principal_id: str,
    *,
    source: str,
) -> None:
    """Reject user-authenticated principals that spoof reserved system namespace."""
    if not is_reserved_system_principal_id(principal_id):
        return
    logger.warning(
        "Rejected reserved system principal in user auth path",
        principal_id=principal_id,
        source=source,
    )
    raise HTTPException(
        status_code=403,
        detail="Reserved principal namespace is not allowed for user-authenticated requests",
    )


def _build_host_override_headers(cfg, target_url: str) -> Optional[Dict[str, str]]:
    """
    Some IdPs require the Host header to match the public URL even when we
    connect via an internal hostname (e.g., docker network alias).
    """
    override = getattr(cfg, "jwt_host_override", None)
    if override:
        return {"Host": override}

    public_url = getattr(cfg, "keycloak_public_url", None)
    if not public_url:
        return None

    try:
        target_host = urlparse(target_url).hostname
        public_host = urlparse(public_url).hostname
    except ValueError:
        return None

    if not target_host or not public_host or target_host == public_host:
        return None

    return {"Host": public_host}


def require_api_key(cfg, header_value: Optional[str]) -> None:
    if cfg.api_key and header_value != cfg.api_key:
        raise HTTPException(status_code=401, detail="invalid api key")


class _JWKSCache:
    def __init__(self) -> None:
        self._data: dict[str, Any] | None = None
        self._loaded: float = 0.0

    def get(self) -> Optional[dict[str, Any]]:
        return self._data

    def set(self, jwks: dict[str, Any], ts: float) -> None:
        self._data = jwks
        self._loaded = ts

    @property
    def loaded(self) -> float:
        return self._loaded


def _get_request_jwks_cache(request: Request) -> _JWKSCache:
    """Retrieve or create the per-app JWKS cache stored on request.app.state."""
    try:
        cached = getattr(request.app.state, "_jwks_cache_obj", None)
        if cached is not None:
            return cached
    except Exception as e:
        logger.warning("jwks_cache_read_failed", error=str(e))

    cache = _JWKSCache()
    try:
        setattr(request.app.state, "_jwks_cache_obj", cache)
    except Exception as e:
        logger.warning("jwks_cache_write_failed", error=str(e))
    return cache


def require_jwt_if_configured(cfg, request: Request, *, cache: Optional[_JWKSCache] = None) -> None:
    """Gate that raises HTTPException when JWT is configured but the token is missing or invalid."""
    if not (getattr(cfg, "jwt_public_key_pem", None) or getattr(cfg, "jwt_jwks_url", None)):
        return
    try:
        import jwt as _jwt  # type: ignore  # noqa: F401
    except ImportError:
        raise HTTPException(status_code=500, detail="jwt support not available")
    cached = _get_cached_auth_result(request)
    if cached:
        failure = cached.get("failure")
        principal = cached.get("principal")
        if failure is not None:
            raise failure
        if principal is not None:
            return

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        if getattr(cfg, "allow_insecure_principal_headers", False):
            return
        failure = HTTPException(status_code=401, detail="missing bearer token")
        _set_cached_auth_result(request, principal=None, failure=failure)
        raise failure
    token = auth.split(" ", 1)[1]

    jwks_cache = cache or _get_request_jwks_cache(request)
    claims = _verify_jwt_token(cfg, token, jwks_cache)
    if claims is not None:
        return

    failure = _build_auth_failure_exception(
        cfg,
        request,
        auth_type="jwt",
        token=token,
        detail="invalid token",
        reason="jwt_verification_failed",
    )
    _set_cached_auth_result(request, principal=None, failure=failure)
    raise failure


def _first_present(d: Dict[str, Any], keys: List[str]) -> Optional[Any]:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _to_list(val: Any) -> List[str]:
    if val is None:
        return []
    if isinstance(val, list):
        return [str(v) for v in val if v is not None]
    s = str(val)
    if "," in s:
        return [p.strip() for p in s.split(",") if p.strip()]
    if " " in s:
        return [p.strip() for p in s.split(" ") if p.strip()]
    return [s]


def _normalize_org_slug(value: Any) -> Optional[str]:
    """
    Normalize organization identifiers emitted by Keycloak's Organizations feature.
    Accepts strings, lists, and nested dictionaries and returns a lowercase slug.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        for item in value:
            slug = _normalize_org_slug(item)
            if slug:
                return slug
        return None
    if isinstance(value, dict):
        for key in ("slug", "name", "id", "identifier", "tenant", "tenant_id", "org", "org_id"):
            if key in value:
                slug = _normalize_org_slug(value[key])
                if slug:
                    return slug
        # Handle Keycloak organization mapper which returns { "<slug>": {...} }
        for key, nested in value.items():
            slug = _normalize_org_slug(key)
            if slug:
                return slug
            slug = _normalize_org_slug(nested)
            if slug:
                return slug
        return None
    slug = str(value).strip()
    if not slug:
        return None
    slug = slug.replace("\\", "/").strip("/")
    if slug.startswith("orgs/"):
        slug = slug[len("orgs/"):]
    slug = slug.replace("/", "-").replace(" ", "-")
    slug = slug.lower()
    return slug or None


def _extract_tenant_candidate(cfg, claims: Dict[str, Any], tenant_keys: List[str]) -> tuple[Optional[str], Optional[str]]:
    """
    Determine the raw tenant identifier by checking standard tenant claims first,
    then falling back to Keycloak organization metadata and group hierarchy.
    Returns (candidate_value, origin_hint).
    """
    candidate = _first_present(claims, tenant_keys)
    origin = "tenant_claim" if candidate else None
    if candidate:
        # Keycloak (or other IdPs) may send the full organization object in a tenant claim;
        # normalize dict/list to a slug so we never use str(dict) as tenant_id in Redis keys.
        normalized = _normalize_org_slug(candidate)
        if normalized:
            return normalized, origin
        return str(candidate), origin
    
    org_claims: List[str] = []
    custom_org_claim = (getattr(cfg, "jwt_organization_claim", "") or "").strip()
    if custom_org_claim:
        org_claims.append(custom_org_claim)
    fallback_org_claims = [
        "organization",
        "organization_name",
        "organization_slug",
        "organization_id",
        "organization_domain",
        "organization_path",
        "org_slug",
        "org_domain",
    ]
    for key in fallback_org_claims:
        if key not in org_claims:
            org_claims.append(key)
    
    for key in org_claims:
        candidate = _normalize_org_slug(claims.get(key))
        if candidate:
            origin = f"organization_claim:{key}"
            return candidate, origin
    
    for key in ("orgs", "org_hierarchy"):
        candidate = _normalize_org_slug(claims.get(key))
        if candidate:
            origin = f"group_path:{key}"
            return candidate, origin
    
    return None, None


def _extract_motet_candidate(cfg, claims: Dict[str, Any], motet_keys: List[str]) -> tuple[Optional[str], Optional[str]]:
    """
    Determine the motet identifier by checking configured motet claim keys.
    Returns (candidate_value, origin_hint).
    
    Args:
        cfg: Configuration object
        claims: JWT claims dictionary
        motet_keys: List of claim keys to check for motet_id
        
    Returns:
        Tuple of (motet_id value or None, origin hint or None)
    """
    candidate = _first_present(claims, motet_keys)
    origin = "motet_claim" if candidate else None
    if candidate:
        # Normalize motet_id (lowercase, strip whitespace)
        motet_id = str(candidate).strip().lower()
        if motet_id:
            return motet_id, origin
    
    return None, None


def _verify_jwt_token(cfg, token: str, cache: Optional[_JWKSCache] = None) -> Optional[Dict[str, Any]]:
    """
    Verify JWT token signature and return claims.
    
    Args:
        cfg: Configuration object
        token: JWT token string
        cache: Optional JWKS cache
        
    Returns:
        Verified JWT claims if valid, None otherwise
    """
    try:
        import jwt  # type: ignore
        from jwt import InvalidTokenError  # type: ignore
    except Exception:
        logger.warning("JWT library not available")
        return None
    
    try:
        if getattr(cfg, "jwt_jwks_url", None):
            # JWKS-based verification (Keycloak, Auth0, etc.)
            import time as _t
            import requests  # type: ignore
            
            # Get or create cache
            if cache is None:
                cache = _JWKSCache()
            
            # Fetch JWKS if cache expired
            now = _t.time()
            ttl = int(getattr(cfg, "jwt_jwks_cache_ttl_seconds", 300))
            if not cache.get() or now - cache.loaded > ttl:
                try:
                    headers = _build_host_override_headers(cfg, cfg.jwt_jwks_url)
                    resp = requests.get(cfg.jwt_jwks_url, timeout=3, headers=headers or None)
                    resp.raise_for_status()
                    jwks = resp.json()
                    cache.set(jwks, now)
                    logger.debug("JWKS cache refreshed", url=cfg.jwt_jwks_url)
                except Exception as e:
                    logger.error("Failed to fetch JWKS", url=cfg.jwt_jwks_url, error=str(e))
                    # Use cached JWKS if available, even if expired
                    if not cache.get():
                        return None
            
            jwks = cache.get() or {}
            
            # Get key ID from token header
            unverified_header = jwt.get_unverified_header(token)
            kid = unverified_header.get("kid")
            
            if not kid:
                logger.warning("JWT token missing kid in header")
                return None
            
            from jwt.algorithms import RSAAlgorithm

            # Find matching key in JWKS
            key = None
            for k in jwks.get("keys", []):
                if k.get("kid") == kid:
                    try:
                        key = RSAAlgorithm.from_jwk(k)
                        break
                    except Exception as e:
                        logger.warning("Failed to parse JWK", kid=kid, error=str(e))
                        continue
            
            if not key:
                logger.warning("JWKS key not found", kid=kid)
                return None
            
            # Verify and decode token
            algs = [a.strip() for a in (getattr(cfg, "jwt_alg_allowlist", "RS256") or "RS256").split(",") if a.strip()]
            leeway = int(getattr(cfg, "jwt_leeway_seconds", 0) or 0)
            
            # Build options for jwt.decode
            options = {}
            if getattr(cfg, "jwt_issuer", None):
                options["issuer"] = cfg.jwt_issuer
            if getattr(cfg, "jwt_audience", None):
                options["audience"] = cfg.jwt_audience
            
            try:
                claims = jwt.decode(token, key=key, algorithms=algs, leeway=leeway, **options)  # type: ignore[arg-type]
            except TypeError:
                # Fallback for older PyJWT versions
                decode_kwargs = {"algorithms": algs}
                if getattr(cfg, "jwt_issuer", None):
                    decode_kwargs["issuer"] = cfg.jwt_issuer
                if getattr(cfg, "jwt_audience", None):
                    decode_kwargs["audience"] = cfg.jwt_audience
                claims = jwt.decode(token, key=key, **decode_kwargs)  # type: ignore[arg-type]
            except InvalidTokenError as e:
                logger.debug("JWT decode failed", error=str(e), issuer=getattr(cfg, "jwt_issuer", None), audience=getattr(cfg, "jwt_audience", None))
                # If audience validation fails, try without it (some public clients may not include 'aud')
                if getattr(cfg, "jwt_audience", None) and "audience" in str(e).lower():
                    logger.debug("Retrying JWT decode without audience validation")
                    options_no_aud = {k: v for k, v in options.items() if k != "audience"}
                    try:
                        claims = jwt.decode(token, key=key, algorithms=algs, leeway=leeway, **options_no_aud)  # type: ignore[arg-type]
                    except Exception:
                        return None
                else:
                    return None
            
            return dict(claims) if isinstance(claims, dict) else None
            
        elif getattr(cfg, "jwt_public_key_pem", None):
            # Static public key verification
            algs = [a.strip() for a in (getattr(cfg, "jwt_alg_allowlist", "RS256,HS256") or "RS256,HS256").split(",") if a.strip()]
            leeway = int(getattr(cfg, "jwt_leeway_seconds", 0) or 0)
            
            try:
                claims = jwt.decode(token, cfg.jwt_public_key_pem, algorithms=algs, leeway=leeway)  # type: ignore[arg-type]
            except TypeError:
                claims = jwt.decode(token, cfg.jwt_public_key_pem, algorithms=algs)  # type: ignore[arg-type]
            
            return dict(claims) if isinstance(claims, dict) else None
        else:
            # JWT not configured
            return None
            
    except InvalidTokenError as e:
        logger.debug("Invalid JWT token", error=str(e))
        return None
    except Exception as e:
        logger.warning("JWT verification failed", error=str(e), exc_info=True)
        return None


def extract_principal(cfg, request: Request) -> Optional[Principal]:
    """
    Extract a Principal from a verified JWT, service account token, or headers.
    
    Priority order:
    1. Service account token (sa_*) - for CLI/automation
    2. JWT token (Bearer <token>) - verified with signature
    3. Headers (X-Principal-Id, X-Tenant-Id) - if allow_insecure_principal_headers=True
    
    Args:
        cfg: Configuration object
        request: FastAPI Request object
        
    Returns:
        Principal if authenticated, None otherwise
    """
    cached = _get_cached_auth_result(request)
    if cached:
        return cached.get("principal")

    try:
        auth = request.headers.get("Authorization", "")

        if auth.startswith("Bearer "):
            token = auth.removeprefix("Bearer ").strip()

            # 1. Check if service account token
            if token.startswith("sa_"):
                try:
                    from .service_accounts import ServiceAccountManager
                    from ..distributed.redis_manager import get_sync_redis_client

                    redis_client = get_sync_redis_client("service_accounts")
                    sa_manager = ServiceAccountManager(redis_client)
                    sa_token = sa_manager.verify_service_account(token)

                    if sa_token:
                        # Service accounts may have motet_id in their metadata
                        motet_id = getattr(sa_token, "motet_id", None) or _resolve_motet_id(cfg)
                        logger.debug("Service account authenticated", token_fingerprint=_token_fingerprint(token), name=sa_token.name, motet_id=motet_id)
                        # Facade policy (ADR-0125 §4) travels on the principal so the
                        # OpenAI-compatible facade does not re-verify the token per request.
                        principal = Principal(
                            id=sa_token.principal_id,
                            tenant_id=sa_token.tenant_id,
                            motet_id=str(motet_id) if motet_id else None,
                            roles=sa_token.roles,
                            claims={
                                "type": "service_account",
                                "name": sa_token.name,
                                "token_id": sa_token.id,
                                "facade_mode": getattr(sa_token, "facade_mode", None),
                                "allowed_models": list(getattr(sa_token, "allowed_models", None) or []),
                                "force_thinking": getattr(sa_token, "force_thinking", None),
                                "force_thinking_effort": getattr(
                                    sa_token, "force_thinking_effort", None
                                ),
                                "agent_id": getattr(sa_token, "agent_id", None),
                            },
                        )
                        _set_cached_auth_result(request, principal=principal, failure=None)
                        return principal

                    failure = _build_auth_failure_exception(
                        cfg,
                        request,
                        auth_type="service_account",
                        token=token,
                        detail="invalid service account token",
                        reason="service_account_invalid_or_expired",
                    )
                    _set_cached_auth_result(request, principal=None, failure=failure)
                    return None
                except HTTPException:
                    raise
                except Exception as e:
                    logger.error("Service account verification failed", error=str(e), exc_info=True)
                    failure = _build_auth_failure_exception(
                        cfg,
                        request,
                        auth_type="service_account",
                        token=token,
                        detail="invalid service account token",
                        reason=f"service_account_verification_error:{type(e).__name__}",
                    )
                    _set_cached_auth_result(request, principal=None, failure=failure)
                    return None

            # 2. Verify as JWT (with signature verification)
            claims = _verify_jwt_token(cfg, token, _get_request_jwks_cache(request))

            if claims:
                sub_key = getattr(cfg, "jwt_sub_claim", "sub") or "sub"
                roles_key = getattr(cfg, "jwt_roles_claim", "roles") or "roles"
                tenant_keys = [k.strip() for k in (getattr(cfg, "jwt_tenant_claims", DEFAULT_TENANT_CLAIMS) or DEFAULT_TENANT_CLAIMS).split(",") if k.strip()]
                motet_keys = [k.strip() for k in (getattr(cfg, "jwt_motet_claims", DEFAULT_MOTET_CLAIMS) or DEFAULT_MOTET_CLAIMS).split(",") if k.strip()]

                pid = str(claims.get(sub_key) or "")
                if not pid:
                    logger.warning("JWT missing required sub claim", sub_claim=sub_key, available_claims=list(claims.keys()))
                    failure = _build_auth_failure_exception(
                        cfg,
                        request,
                        auth_type="jwt",
                        token=token,
                        detail="invalid token: missing subject claim",
                        reason="jwt_missing_subject_claim",
                    )
                    _set_cached_auth_result(request, principal=None, failure=failure)
                    return None
                _reject_reserved_system_principal(pid, source="jwt")
                roles = _to_list(claims.get(roles_key) or claims.get("role") or claims.get("scope"))
                tenant_id, tenant_origin = _extract_tenant_candidate(cfg, claims, tenant_keys)
                motet_id, motet_origin = _extract_motet_candidate(cfg, claims, motet_keys)

                if not motet_id:
                    motet_id = _resolve_motet_id(cfg)
                    motet_origin = "environment_fallback"

                # Log what we found before mapping
                logger.info(
                    "JWT tenant and motet claim extraction",
                    principal_id=pid,
                    raw_tenant_value=tenant_id,
                    tenant_keys_checked=tenant_keys,
                    raw_motet_value=motet_id,
                    motet_keys_checked=motet_keys,
                    available_claims=list(claims.keys()),
                    motet_tenant_claim=claims.get("motet_tenant"),
                    resource_owner=claims.get("resourceOwner"),
                    organization_claim=getattr(cfg, "jwt_organization_claim", None),
                    tenant_origin=tenant_origin,
                    motet_origin=motet_origin
                )

                tenant_id = resolve_tenant_id(cfg, tenant_id, claims)
                if tenant_origin:
                    claims["tenant_origin"] = tenant_origin
                if motet_origin:
                    claims["motet_origin"] = motet_origin

                # Reject if tenant_id is missing (unless in dev mode with insecure headers allowed)
                if not tenant_id and not getattr(cfg, "allow_insecure_principal_headers", False):
                    logger.warning(
                        "Authenticated identity missing tenant claim",
                        principal_id=pid,
                        tenant_keys_checked=tenant_keys,
                        available_claims=list(claims.keys())
                    )
                    raise HTTPException(
                        status_code=403,
                        detail="Authenticated identity missing tenant claim"
                    )

                logger.info("JWT authenticated", principal_id=pid, tenant_id=tenant_id, motet_id=motet_id, tenant_keys_checked=tenant_keys, motet_keys_checked=motet_keys)
                principal = Principal(
                    id=pid or "",
                    roles=roles,
                    tenant_id=str(tenant_id) if tenant_id is not None else None,
                    motet_id=str(motet_id) if motet_id is not None else None,
                    claims=claims
                )
                _set_cached_auth_result(request, principal=principal, failure=None)
                return principal

            if getattr(cfg, "jwt_public_key_pem", None) or getattr(cfg, "jwt_jwks_url", None):
                failure = _build_auth_failure_exception(
                    cfg,
                    request,
                    auth_type="jwt",
                    token=token,
                    detail="invalid token",
                    reason="jwt_verification_failed",
                )
                _set_cached_auth_result(request, principal=None, failure=failure)
                return None

            logger.debug("JWT verification failed or not configured")
    
        # 3. X-API-Key: when configured, a matching key authenticates as a synthetic principal
        api_key_header = request.headers.get("X-API-Key") or request.headers.get("x-api-key")
        if getattr(cfg, "api_key", None) and api_key_header == cfg.api_key:
            motet_id = _resolve_motet_id(cfg)
            logger.debug("API key authentication", motet_id=motet_id)
            principal = Principal(
                id="api_key",
                roles=[],
                tenant_id="default",
                motet_id=str(motet_id) if motet_id else None,
                claims={"type": "api_key"}
            )
            _set_cached_auth_result(request, principal=principal, failure=None)
            return principal

        # 4. Fallback to headers for dev/test
        # Policy validation happens once at startup in create_app(); no need to
        # re-check (and re-log warnings) on every request.
        if getattr(cfg, "allow_insecure_principal_headers", False):
            pid = request.headers.get("X-Principal-Id")
            roles = _to_list(request.headers.get("X-Roles"))
            tenant_id = request.headers.get("X-Tenant-Id")
            motet_id = request.headers.get("X-Motet-Id") or _resolve_motet_id(cfg)
            if pid or tenant_id:
                _reject_reserved_system_principal(str(pid or ""), source="header_dev_mode")
                logger.debug("Using header-based authentication (dev mode)", principal_id=pid, tenant_id=tenant_id, motet_id=motet_id)
                principal = Principal(
                    id=str(pid or ""),
                    roles=roles,
                    tenant_id=str(tenant_id) if tenant_id else None,
                    motet_id=str(motet_id) if motet_id else None,
                    claims={"type": "header", "source": "dev_mode"}
                )
                _set_cached_auth_result(request, principal=principal, failure=None)
                return principal

        _set_cached_auth_result(request, principal=None, failure=None)
        return None
    except HTTPException as exc:
        _set_cached_auth_result(request, principal=None, failure=exc)
        raise


def extract_principal_from_claims(claims: Dict[str, Any], cfg=None) -> Principal:
    """
    Extract a Principal from JWT claims without requiring a request context.
    
    This is useful for OAuth popup flows where we have decoded JWT claims
    but no full request context. Uses the same tenant and motet extraction 
    logic as the main auth system.
    
    Args:
        claims: Decoded JWT claims dictionary
        cfg: Optional Config object (will use default if not provided)
        
    Returns:
        Principal object with id, tenant_id, motet_id, and roles extracted from claims
        
    Example:
        import jwt
        decoded = jwt.decode(token, options={"verify_signature": False})
        principal = extract_principal_from_claims(decoded)
    """
    if cfg is None:
        from ..config import Config
        cfg = Config()
    
    # Extract principal_id (sub claim)
    sub_claim = getattr(cfg, "jwt_sub_claim", "sub") or "sub"
    principal_id = str(claims.get(sub_claim) or claims.get("client_id", "unknown"))
    if not principal_id:
        raise ValueError("Unable to extract principal_id from claims")
    if is_reserved_system_principal_id(principal_id):
        raise ValueError(
            "Reserved principal namespace is not allowed for user-authenticated claims"
        )
    
    # Extract tenant_id using the standard logic
    tenant_claims_str = getattr(cfg, "jwt_tenant_claims", DEFAULT_TENANT_CLAIMS) or DEFAULT_TENANT_CLAIMS
    tenant_keys = [k.strip() for k in tenant_claims_str.split(",") if k.strip()]
    tenant_id, tenant_origin = _extract_tenant_candidate(cfg, claims, tenant_keys)
    
    # Apply tenant ID mapping if configured
    if tenant_id:
        tenant_id = resolve_tenant_id(cfg, tenant_id, claims)
    
    # Fallback to azp (authorized party) if no tenant found
    if not tenant_id:
        tenant_id = claims.get("azp", "default")
    
    # Extract motet_id using the standard logic
    motet_claims_str = getattr(cfg, "jwt_motet_claims", DEFAULT_MOTET_CLAIMS) or DEFAULT_MOTET_CLAIMS
    motet_keys = [k.strip() for k in motet_claims_str.split(",") if k.strip()]
    motet_id, motet_origin = _extract_motet_candidate(cfg, claims, motet_keys)
    
    if not motet_id:
        motet_id = _resolve_motet_id(cfg)
    
    # Extract roles
    roles_claim = getattr(cfg, "jwt_roles_claim", "roles") or "roles"
    roles = claims.get(roles_claim, [])
    if isinstance(roles, str):
        roles = [r.strip() for r in roles.split(",") if r.strip()]
    
    return Principal(
        id=principal_id,
        tenant_id=str(tenant_id),
        motet_id=str(motet_id) if motet_id else None,
        roles=list(roles) if roles else [],
        claims=claims
    )


__all__ = [
    "require_api_key",
    "require_jwt_if_configured",
    "_JWKSCache",
    "extract_principal",
    "extract_principal_from_claims",
    "validate_insecure_principal_header_policy",
]


