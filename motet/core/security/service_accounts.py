"""
Motet - Service Account Token Management

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Service account token management for CLI and automation use cases.
    Provides secure token generation, verification, and revocation for long-lived
    authentication tokens used in CI/CD pipelines and automated workflows.

    Tokens also carry OpenAI-compatible facade policy: an optional
    execution mode binding, a model allowlist, and optional force_thinking for
    clients that never send reasoning opt-in. Clients such as Cursor can only
    supply a base URL, an API key, and a model string, so facade authorization is
    bound to the token rather than to request headers.

    Service account tokens are self-contained identifiers (format: sa_*) that
    are stored in Redis with metadata (name, roles, tenant_id, expiration).
    They provide an alternative to JWT tokens for automation scenarios where
    token refresh is not practical.

Dependencies:
    - redis: Redis client for token storage
    - secrets: Cryptographically secure random token generation
    - datetime: Token expiration management
    - pydantic: Data validation and configuration models
    - typing: Type hints and annotations

Usage:
    from motet.core.security.service_accounts import ServiceAccountManager
    from motet.core.distributed.redis_manager import get_sync_redis_client
    
    redis_client = get_sync_redis_client("service_accounts")
    sa_manager = ServiceAccountManager(redis_client)
    
    # Create service account
    token = sa_manager.create_service_account(
        name="ci-pipeline",
        tenant_id="acme-corp",
        motet_id="production",
        roles=["admin", "ci"],
        created_by="alice@acme.com",
        expires_days=365
    )
    
    # Verify token
    token_meta = sa_manager.verify_service_account(token)
    if token_meta:
        print(f"Authenticated as {token_meta.principal_id}")

Notes:
    - Service account tokens are stored in Redis with TTL matching expiration
    - Tokens can be revoked individually or by name pattern
    - Token format: sa_{timestamp}_{random}_{name} for easy identification
    - Tokens are self-contained (no external dependencies)
    - Create writes ``motet:auth:service_account:{token}`` → tenant so verify
      can ``GET`` then ``HGETALL`` the tenant hash without a keyspace SCAN
    - Supports tenant isolation and role-based access control
    - Includes comprehensive error handling and logging
    - facade_mode / allowed_models / force_thinking / agent_id are consumed by
      motet.interfaces.api.openai_compat; absent values fall back to
      Config defaults (allowlist deny-all; force_thinking off; agent_id empty)
"""

from __future__ import annotations

import secrets
import json
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, field_serializer

from motet.core.distributed.tenant_keys import (
    delete_candidate_keys,
    family_scan_patterns,
    is_reserved_tenant_id,
    product_key,
    tenant_key,
)

import structlog

logger = structlog.get_logger(__name__)


class ServiceAccountToken(BaseModel):
    """Service account token metadata."""
    
    id: str = Field(..., description="Token ID (format: sa_*)")
    name: str = Field(..., description="Human-readable service account name")
    principal_id: str = Field(..., description="Principal ID for this service account")
    tenant_id: str = Field(..., description="Tenant ID for multi-tenant isolation")
    motet_id: Optional[str] = Field(None, description="Motet/environment identifier")
    roles: List[str] = Field(default_factory=list, description="Roles assigned to this service account")
    created_at: datetime = Field(..., description="Token creation timestamp")
    expires_at: datetime = Field(..., description="Token expiration timestamp")
    created_by: str = Field(..., description="Principal who created this token")
    last_used_at: Optional[datetime] = Field(None, description="Last time token was used")
    facade_mode: Optional[str] = Field(
        None,
        description=(
            "OpenAI-compatible facade execution mode for this token: passthrough, "
            "hosted_tools, or agent. None falls back to the configured default."        ),
    )
    allowed_models: List[str] = Field(
        default_factory=list,
        description=(
            "Facade model allowlist as 'provider/model' ids. Empty falls back "
            "to the configured default allowlist, which is deny-all unless explicitly set."
        ),
    )
    force_thinking: Optional[bool] = Field(
        None,
        description=(
            "When true, the OpenAI facade enables thinking for CAP_REASONING models even "
            "without client opt-in. None falls back to MOTET_OPENAI_COMPAT_FORCE_THINKING."
        ),
    )
    force_thinking_effort: Optional[str] = Field(
        None,
        description=(
            "Default reasoning effort when force_thinking applies and the client omits "
            "effort. None falls back to MOTET_OPENAI_COMPAT_FORCE_THINKING_EFFORT."
        ),
    )
    agent_id: Optional[str] = Field(
        None,
        description=(
            "Default Motet agent id for OpenAI facade agent mode when the client omits "
            "motet_agent_id. None falls back to MOTET_OPENAI_COMPAT_DEFAULT_AGENT_ID."
        ),
    )
    
    @field_serializer("created_at", "expires_at", "last_used_at")
    def serialize_datetime(self, value: Optional[datetime], _info) -> Optional[str]:
        """Serialize datetime fields to ISO format strings."""
        if value is None:
            return None
        return value.isoformat()


class ServiceAccountManager:
    """Manage service account tokens for CLI/automation."""
    
    def __init__(self, redis_client):
        """
        Initialize service account manager.
        
        Args:
            redis_client: Synchronous Redis client instance
        """
        self.redis = redis_client
        self._prefix = "auth:service_account:"
        self._revoked_prefix = product_key("auth:revoked_tokens")

    def _locator_key(self, token: str) -> str:
        return product_key(f"{self._prefix}{token}")

    def _locator_tenant(self, token: str) -> Optional[str]:
        raw = self.redis.get(self._locator_key(token))
        if isinstance(raw, (bytes, bytearray)):
            text = raw.decode("utf-8")
        elif isinstance(raw, str):
            text = raw
        else:
            return None
        text = text.strip()
        if not text or text in ("None", "null"):
            return None
        return text

    def _data_key(self, token: str, tenant_id: Optional[str] = None) -> Optional[str]:
        logical = f"{self._prefix}{token}"
        resolved = tenant_id or self._locator_tenant(token)
        if not resolved:
            return None
        return tenant_key(resolved, logical)

    def _tenant_from_stored_key(self, stored_key: str) -> Optional[str]:
        tenant, sep, rest = stored_key.partition(":")
        if sep and rest.startswith(self._prefix) and not is_reserved_tenant_id(tenant):
            return tenant
        return None

    def _token_id_from_key(self, stored_key: str) -> Optional[str]:
        """Extract the token id from a collapsed ``{tenant}:auth:service_account:`` key."""
        marker = self._prefix
        if stored_key.startswith(marker):
            return stored_key[len(marker) :] or None
        tenant, sep, rest = stored_key.partition(":")
        if not sep or is_reserved_tenant_id(tenant) or not rest.startswith(marker):
            return None
        return rest[len(marker) :] or None
    
    def create_service_account(
        self,
        name: str,
        tenant_id: str,
        motet_id: Optional[str],
        roles: List[str],
        created_by: str,
        expires_days: int = 365,
        facade_mode: Optional[str] = None,
        allowed_models: Optional[List[str]] = None,
        force_thinking: Optional[bool] = None,
        force_thinking_effort: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> str:
        """
        Create a new service account token.
        
        Args:
            name: Human-readable service account name (e.g., "ci-pipeline")
            tenant_id: Tenant ID for multi-tenant isolation
            motet_id: Motet/environment identifier (e.g., "production")
            roles: List of roles assigned to this service account
            created_by: Principal ID who created this token
            expires_days: Token expiration in days (default: 365)
            facade_mode: Optional OpenAI facade mode binding (ADR-0125 §5c)
            allowed_models: Optional facade model allowlist as "provider/model" ids (ADR-0125 §11a)
            force_thinking: Optional facade force-thinking policy (ADR-0125)
            force_thinking_effort: Optional default effort when force_thinking applies
            agent_id: Optional default Motet agent id for facade agent mode
            
        Returns:
            Service account token string (format: sa_*) - store securely, shown only once!
            
        Raises:
            ValueError: If name or tenant_id is empty
        """
        if not name or not name.strip():
            raise ValueError("Service account name cannot be empty")
        if not tenant_id or not tenant_id.strip():
            raise ValueError("Tenant ID cannot be empty")
        if not motet_id or not str(motet_id).strip():
            raise ValueError("Motet ID cannot be empty")
        
        tenant_id = tenant_id.strip()
        motet_id = str(motet_id).strip()
        
        # Generate secure random token
        random_part = secrets.token_urlsafe(32)
        timestamp = datetime.utcnow().strftime("%Y%m%d")
        token_id = f"sa_{timestamp}_{random_part}_{name.replace(' ', '-').lower()}"
        
        # Create metadata
        now = datetime.utcnow()
        token_meta = ServiceAccountToken(
            id=token_id,
            name=name,
            principal_id=f"service-account:{name}",
            tenant_id=tenant_id,
            motet_id=motet_id,
            roles=roles,
            created_at=now,
            expires_at=now + timedelta(days=expires_days),
            created_by=created_by,
            last_used_at=None,
            facade_mode=facade_mode,
            allowed_models=list(allowed_models or []),
            force_thinking=force_thinking,
            force_thinking_effort=force_thinking_effort,
            agent_id=agent_id,
        )
        
        # Store metadata in Redis
        redis_key = tenant_key(tenant_id, f"{self._prefix}{token_id}")
        token_data = token_meta.model_dump(mode="json")
        
        # Convert datetime objects to ISO strings for Redis storage
        token_data["created_at"] = token_meta.created_at.isoformat()
        token_data["expires_at"] = token_meta.expires_at.isoformat()
        if token_meta.last_used_at:
            token_data["last_used_at"] = token_meta.last_used_at.isoformat()
        
        # Store as hash
        self.redis.hset(redis_key, mapping={
            k: json.dumps(v) if isinstance(v, (list, dict)) else str(v)
            for k, v in token_data.items()
        })
        
        # Set expiration TTL (add 1 day grace period for cleanup)
        ttl_seconds = int((token_meta.expires_at - now).total_seconds()) + 86400
        self.redis.expire(redis_key, ttl_seconds)
        self.redis.set(self._locator_key(token_id), tenant_id)
        self.redis.expire(self._locator_key(token_id), ttl_seconds)
        
        logger.info(
            "Service account created",
            token_id=token_id,
            name=name,
            tenant_id=tenant_id,
            motet_id=motet_id,
            expires_at=token_meta.expires_at.isoformat(),
            created_by=created_by
        )
        
        return token_id
    
    def verify_service_account(
        self, token: str, tenant_id: Optional[str] = None
    ) -> Optional[ServiceAccountToken]:
        """
        Verify service account token and return metadata.
        
        Args:
            token: Service account token string (format: sa_*)
            
        Returns:
            ServiceAccountToken metadata if valid, None otherwise
        """
        if not token or not token.startswith("sa_"):
            return None
        
        # Check if revoked
        if self.redis.sismember(self._revoked_prefix, token):
            logger.warning("Service account token revoked", token_id=token)
            return None
        
        redis_key = self._data_key(token, tenant_id)
        if not redis_key:
            logger.warning("Service account token not found", token_id=token)
            return None
        token_data = self.redis.hgetall(redis_key)
        
        if not token_data:
            logger.warning("Service account token not found", token_id=token)
            return None
        
        try:
            # Parse token data
            parsed_data = {}
            for k, v in token_data.items():
                if k in ["created_at", "expires_at", "last_used_at"]:
                    # Handle None values (stored as string "None" in Redis)
                    if v and v != "None" and v != "":
                        parsed_data[k] = datetime.fromisoformat(v)
                    else:
                        parsed_data[k] = None
                elif k in ("roles", "allowed_models"):
                    parsed_data[k] = json.loads(v) if v else []
                elif k == "force_thinking":
                    if v in (None, "", "None"):
                        parsed_data[k] = None
                    elif isinstance(v, bool):
                        parsed_data[k] = v
                    else:
                        text = str(v).strip().lower()
                        if text in ("1", "true", "yes", "on"):
                            parsed_data[k] = True
                        elif text in ("0", "false", "no", "off"):
                            parsed_data[k] = False
                        else:
                            parsed_data[k] = None
                elif k in ("motet_id", "facade_mode", "force_thinking_effort", "agent_id"):
                    parsed_data[k] = v if v and v != "None" else None
                else:
                    parsed_data[k] = v
            
            token_meta = ServiceAccountToken(**parsed_data)
            
            # Check expiration
            if datetime.utcnow() > token_meta.expires_at:
                logger.warning("Service account token expired", token_id=token, expires_at=token_meta.expires_at.isoformat())
                return None
            
            # Update last used timestamp
            now = datetime.utcnow()
            self.redis.hset(redis_key, "last_used_at", now.isoformat())
            token_meta.last_used_at = now
            
            logger.debug("Service account token verified", token_id=token, name=token_meta.name)
            return token_meta
            
        except Exception as e:
            logger.error("Failed to parse service account token", token_id=token, error=str(e), exc_info=True)
            return None
    
    def revoke_service_account(self, token: str) -> bool:
        """
        Revoke a service account token.
        
        Args:
            token: Service account token string (format: sa_*)
            
        Returns:
            True if token was revoked, False if token not found
        """
        if not token or not token.startswith("sa_"):
            return False
        
        # Get token metadata to determine TTL
        token_meta = self.verify_service_account(token)
        if not token_meta:
            return False
        
        # Add to revocation list (with TTL matching token expiration)
        ttl = int((token_meta.expires_at - datetime.utcnow()).total_seconds())
        if ttl > 0:
            self.redis.sadd(self._revoked_prefix, token)
            self.redis.expire(self._revoked_prefix, ttl)
        
        delete_candidate_keys(
            self.redis,
            (
                tenant_key(token_meta.tenant_id, f"{self._prefix}{token}"),
                self._locator_key(token),
            ),
        )
        
        logger.info("Service account token revoked", token_id=token, name=token_meta.name)
        return True
    
    def list_service_accounts(
        self,
        tenant_id: Optional[str] = None,
        motet_id: Optional[str] = None
    ) -> List[ServiceAccountToken]:
        """
        List all service accounts (optionally filtered by tenant).
        
        Args:
            tenant_id: Optional tenant ID filter
            motet_id: Optional motet/environment filter
            
        Returns:
            List of ServiceAccountToken metadata
        """
        accounts = []
        seen: set[str] = set()
        for pattern in family_scan_patterns(self._prefix):
            for key in self.redis.scan_iter(match=pattern):
                decoded = key.decode("utf-8") if isinstance(key, (bytes, bytearray)) else str(key)
                token_id = self._token_id_from_key(decoded)
                if not token_id or token_id in seen:
                    continue
                seen.add(token_id)
                token_meta = self.verify_service_account(
                    token_id, tenant_id=self._tenant_from_stored_key(decoded)
                )
                if token_meta:
                    tenant_match = tenant_id is None or token_meta.tenant_id == tenant_id
                    motet_match = motet_id is None or token_meta.motet_id == motet_id
                    if tenant_match and motet_match:
                        accounts.append(token_meta)
        
        return accounts


__all__ = ["ServiceAccountManager", "ServiceAccountToken"]

