"""
Motet - OAuth Manager

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    OAuth Manager for the Motet distributed framework.
    Manages OAuth flows for MCP servers, including token storage, refresh,
    and validation. Uses Redis for thread-safe, distributed state management.

Dependencies:
    - typing: Type hints and annotations
    - structlog: Structured logging
    - aiohttp: Async HTTP client for OAuth token exchange
    - redis.asyncio: Async Redis client for state management
    - vault_client: Credential storage and retrieval

Usage:
    from motet.core.security.oauth_manager import get_oauth_manager
    
    oauth_manager = get_oauth_manager()
    auth_url, state = await oauth_manager.initiate_oauth(
        server_id="google_workspace",
        redirect_uri="https://example.com/callback",
        principal_id="user123",
        tenant_id="org456"
    )

Notes:
    - Thread-safe: Uses Redis for pending_states (distributed, auto-expires)
    - Multi-tenant: Supports per-user, per-tenant, and global credentials
    - Config-driven: OAuth provider config loaded from mcp_instance_manager.yaml
    - Token validation: Active validation via tokeninfo_url when configured
    - Integrates with distributed architecture and event system
"""


from typing import Dict, Any, Optional, Tuple
from datetime import datetime, timedelta
import secrets
import structlog
from urllib.parse import urlencode
import aiohttp
import json

from .vault_client import get_vault_client
from motet.core.commands.base import CommandContext
from ..types import Principal, Tenant
from ..distributed.redis_manager import get_redis_client

from .system_principals import (
    SYSTEM_PRINCIPAL_OAUTH_MANAGER as SYSTEM_PRINCIPAL_ID,
    SYSTEM_TENANT_ID,
    SYSTEM_MOTET_ID,
)

logger = structlog.get_logger(__name__)

def _get_oauth_config_from_yaml(server_id: str) -> Optional[Dict[str, Any]]:
    """
    Get OAuth configuration for a server from mcp_instance_manager.yaml (ADR-0057 Phase 4).
    
    Replaces hardcoded OAUTH_CONFIGS dict with YAML-driven config.
    
    Returns:
        OAuth config dict with keys: provider, auth_url, token_url, scopes
        or None if server not found or not configured for OAuth
    """
    from motet.core.security.vault_mcp_integration import get_service_auth_config
    
    auth_config = get_service_auth_config(server_id)
    if not auth_config or auth_config.get("type") != "oauth2":
        return None
    
    return {
        "provider": auth_config.get("provider", server_id),
        "auth_url": auth_config.get("auth_url"),
        "token_url": auth_config.get("token_url"),
        "scopes": auth_config.get("scopes", [])
    }


def _make_oauth_client_credentials_key(server_id: str) -> str:
    """
    Generate OAuth client credentials key in colon-separated format.
    
    Format: oauth:client_credentials:{server_id}
    Example: oauth:client_credentials:google_workspace
    """
    return f"oauth:client_credentials:{server_id}"


def _make_oauth_tokens_key(
    server_id: str,
    tenant_id: Optional[str] = None,
    motet_id: Optional[str] = None,
    principal_id: Optional[str] = None
) -> str:
    """
    Generate OAuth tokens key in colon-separated format (ADR-0058).
    
    Format priority (most specific first):
    1. oauth:tokens:{server_id}:{tenant_id}:{motet_id}:{principal_id} (USER scope - requires all three)
    2. oauth:tokens:{server_id}:{tenant_id}:{motet_id} (MOTET scope - requires tenant and motet)
    3. oauth:tokens:{server_id}:{tenant_id} (TENANT scope - requires tenant)
    4. oauth:tokens:{server_id}:global (GLOBAL scope - fallback)
    
    Normalizes empty strings to None to ensure consistent key generation.
    
    Args:
        server_id: MCP server identifier
        tenant_id: Optional tenant ID
        motet_id: Optional motet ID (required for USER and MOTET scope per ADR-0058)
        principal_id: Optional principal ID (required for USER scope per ADR-0058)
    
    Returns:
        Credential key in colon-separated format
    """
    # Normalize empty strings: use "default" for motet_id to match storage format
    # Empty strings are falsy and break key generation, but tokens are stored with "default"
    tenant_id = tenant_id if tenant_id else None
    motet_id = motet_id if motet_id else "default"  # Use "default" to match _get_context_for_principal behavior
    principal_id = principal_id if principal_id else None
    
    # ADR-0058: USER scope requires tenant_id:motet_id:principal_id
    if tenant_id and motet_id and principal_id:
        return f"oauth:tokens:{server_id}:{tenant_id}:{motet_id}:{principal_id}"
    # ADR-0058: MOTET scope requires tenant_id:motet_id
    elif tenant_id and motet_id:
        return f"oauth:tokens:{server_id}:{tenant_id}:{motet_id}"
    # ADR-0058: TENANT scope requires tenant_id
    elif tenant_id:
        return f"oauth:tokens:{server_id}:{tenant_id}"
    # GLOBAL scope (fallback)
    else:
        return f"oauth:tokens:{server_id}:global"


def _get_oauth_tokens_key_candidates(
    server_id: str,
    tenant_id: Optional[str] = None,
    motet_id: Optional[str] = None,
    principal_id: Optional[str] = None
) -> list[str]:
    """
    Get list of OAuth token key candidates in priority order (most specific first).
    
    Used for credential lookup with fallback logic. Uses ADR-0058 format only.
    
    Normalizes empty strings to None to ensure consistent key generation.
    
    Returns:
        List of credential keys to try in order (ADR-0058 format)
    """
    # Normalize empty strings: use "default" for motet_id to match storage format
    # Empty strings are falsy and break key generation, but tokens are stored with "default"
    tenant_id = tenant_id if tenant_id else None
    motet_id = motet_id if motet_id else "default"  # Use "default" to match _get_context_for_principal behavior
    principal_id = principal_id if principal_id else None
    
    candidates = []
    # ADR-0058: USER scope requires tenant_id:motet_id:principal_id
    if tenant_id and motet_id and principal_id:
        candidates.append(f"oauth:tokens:{server_id}:{tenant_id}:{motet_id}:{principal_id}")
    # ADR-0058: MOTET scope requires tenant_id:motet_id
    if tenant_id and motet_id:
        candidates.append(f"oauth:tokens:{server_id}:{tenant_id}:{motet_id}")
    # ADR-0058: TENANT scope requires tenant_id
    if tenant_id:
        candidates.append(f"oauth:tokens:{server_id}:{tenant_id}")
    # GLOBAL scope (fallback)
    candidates.append(f"oauth:tokens:{server_id}:global")
    return candidates


class OAuthManager:
    """Manages OAuth flows for MCP servers."""
    
    def __init__(self):
        self.vault_client = get_vault_client()
        self.redis_client = get_redis_client("oauth_manager")
        self.pending_states_ttl = 600  # 10 minutes TTL for OAuth states
        self.pending_states_key_prefix = "oauth:pending_state:"
    
    async def initiate_oauth(
        self,
        server_id: str,
        redirect_uri: str,
        principal_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        task_id: Optional[str] = None
    ) -> Tuple[str, str]:
        """
        Initiate OAuth flow for an MCP server.
        
        Args:
            server_id: MCP server identifier (e.g., "google_workspace")
            redirect_uri: Callback URL for OAuth provider
            principal_id: Principal ID for per-user credentials (required for multi-tenant)
            tenant_id: Tenant ID for multi-tenant credential isolation
            motet_id: Motet ID for per-motet credential isolation (ADR-0058)
        
        Returns:
            Tuple of (authorization_url, state)
        """
        principal_id = (principal_id or "").strip()
        tenant_id = (tenant_id or "").strip()
        if not principal_id:
            raise ValueError("principal_id is required for initiate_oauth")
        if not tenant_id:
            raise ValueError("tenant_id is required for initiate_oauth")

        config = _get_oauth_config_from_yaml(server_id)
        if not config:
            raise ValueError(f"No OAuth config for server: {server_id}. Please configure auth section in mcp_instance_manager.yaml")
        
        # Get client credentials from vault
        context = self._get_system_context()
        credential_key = _make_oauth_client_credentials_key(server_id)
        
        logger.debug("Looking up OAuth client credentials",
                    server_id=server_id,
                    credential_key=credential_key,
                    context_principal_id=context.principal_id,
                    context_tenant_id=context.tenant_id)
        
        client_creds = self.vault_client.get_credential(
            credential_key,
            context=context
        )
        
        if not client_creds:
            logger.error("OAuth client credentials not found",
                        server_id=server_id,
                        credential_key=credential_key,
                        context_principal_id=context.principal_id,
                        context_tenant_id=context.tenant_id)
            raise ValueError(
                f"No client credentials in vault for: {server_id}. "
                f"Please store credentials first. "
                f"Expected key: {credential_key} "
                f"(must be stored as global credential with empty principal_id)"
            )
        
        # Debug: Log which client credentials are being used
        logger.info("Retrieved OAuth client credentials",
                   server_id=server_id,
                   credential_key=credential_key,
                   client_id=client_creds.get("client_id", "")[:30] + "...",
                   client_type=client_creds.get("client_type", "unknown"))
        
        # Generate state for CSRF protection
        state = secrets.token_urlsafe(32)
        state_data = {
            "server_id": server_id,
            "redirect_uri": redirect_uri,
            "principal_id": principal_id,  # For per-principal credentials
            "tenant_id": tenant_id,  # For multi-tenant credential isolation
            "motet_id": motet_id,  # For per-motet credential isolation (ADR-0058)
            "conversation_id": conversation_id,  # For storing login success in conversation history
            "task_id": task_id,  # For conversation context
            "timestamp": datetime.utcnow().isoformat()
        }
        
        # Store state in Redis with TTL (thread-safe, distributed, auto-expires)
        state_key = f"{self.pending_states_key_prefix}{state}"
        await self.redis_client.setex(
            state_key,
            self.pending_states_ttl,
            json.dumps(state_data)
        )
        
        # Build authorization URL
        params = {
            "client_id": client_creds["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(config["scopes"]),
            "state": state,
            "access_type": "offline",  # Request refresh token
            "prompt": "consent"  # Force consent to get refresh token
        }
        
        auth_url = f"{config['auth_url']}?{urlencode(params)}"
        
        logger.info("OAuth flow initiated",
                   server_id=server_id,
                   state=state[:8],
                   principal_id=principal_id,
                   tenant_id=tenant_id)
        
        return auth_url, state
    
    async def complete_oauth(
        self,
        server_id: str,
        code: str,
        state: str,
        redirect_uri: str
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Complete OAuth flow by exchanging code for tokens.
        
        Args:
            server_id: MCP server identifier
            code: Authorization code from provider
            state: State parameter for CSRF validation
            redirect_uri: Callback URL (must match initiate_oauth)
        
        Returns:
            Tuple of (tokens, state_data) where:
            - tokens: Token dictionary with access_token, refresh_token, etc.
            - state_data: Original state data including principal_id and tenant_id
        """
        # Validate state from Redis (atomic get-and-delete prevents replay attacks)
        state_key = f"{self.pending_states_key_prefix}{state}"
        state_json = await self.redis_client.get(state_key)
        
        if not state_json:
            raise ValueError("Invalid or expired state parameter")
        
        # Parse state data
        try:
            state_data = json.loads(state_json)
        except (json.JSONDecodeError, TypeError) as e:
            logger.error("Failed to parse OAuth state data",
                        state=state[:8],
                        error=str(e))
            raise ValueError("Invalid state data format")
        
        # Verify server_id matches
        if state_data.get("server_id") != server_id:
            raise ValueError("Server ID mismatch")
        
        # Atomic delete (prevents replay attacks - state can only be used once)
        deleted = await self.redis_client.delete(state_key)
        if not deleted:
            # State was already deleted (race condition or replay attack)
            logger.warning("OAuth state already used or expired",
                          state=state[:8],
                          server_id=server_id)
            raise ValueError("State already used or expired")
        
        config = _get_oauth_config_from_yaml(server_id)
        if not config:
            raise ValueError(f"No OAuth config for server: {server_id}. Please configure auth section in mcp_instance_manager.yaml")
        
        # Get client credentials from vault
        context = self._get_system_context()
        credential_key = _make_oauth_client_credentials_key(server_id)
        client_creds = self.vault_client.get_credential(
            credential_key,
            context=context
        )
        
        if not client_creds:
            raise ValueError(
                f"No client credentials in vault for: {server_id}. "
                f"Expected key: {credential_key}"
            )

        # Debug: Log which client credentials are being used
        logger.info("Retrieved OAuth client credentials for callback",
                   server_id=server_id,
                   credential_key=credential_key,
                   client_id=client_creds.get("client_id", "")[:30] + "...",
                   client_type=client_creds.get("client_type", "unknown"))
        
        # Exchange code for tokens
        async with aiohttp.ClientSession() as session:
            async with session.post(
                config["token_url"],
                data={
                    "client_id": client_creds["client_id"],
                    "client_secret": client_creds["client_secret"],
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code"
                }
            ) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    raise ValueError(f"Token exchange failed: {error}")
                
                tokens = await resp.json()
        
        # Debug: Log what OAuth provider returned (sanitized)
        logger.info("OAuth token exchange response",
                   server_id=server_id,
                   has_access_token=bool(tokens.get("access_token")),
                   access_token_length=len(tokens.get("access_token", "")),
                   access_token_prefix=tokens.get("access_token", "")[:20] if tokens.get("access_token") else "NONE",
                   has_refresh_token=bool(tokens.get("refresh_token")),
                   token_type=tokens.get("token_type"),
                   expires_in=tokens.get("expires_in"),
                   scope_count=len(tokens.get("scope", "").split()))
        
        # Calculate expiry
        expires_in = tokens.get("expires_in", 3600)
        tokens["expires_at"] = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()
        tokens["scope"] = tokens.get("scope", " ".join(config["scopes"]))
        
        # Add client credentials for refresh
        tokens["client_id"] = client_creds["client_id"]
        tokens["client_secret"] = client_creds["client_secret"]
        tokens["token_uri"] = config["token_url"]

        principal_id = str(state_data.get("principal_id") or "").strip()
        tenant_id = str(state_data.get("tenant_id") or "").strip()
        motet_id = str(state_data.get("motet_id") or "").strip() or SYSTEM_MOTET_ID
        if not principal_id:
            raise ValueError("OAuth state missing principal_id")
        if not tenant_id:
            raise ValueError("OAuth state missing tenant_id")
        
        logger.info("OAuth flow completed",
                   server_id=server_id,
                   has_refresh_token=bool(tokens.get("refresh_token")),
                   principal_id=principal_id,
                   tenant_id=tenant_id)

        # Store tokens as the commit point for successful OAuth completion (ADR-0057/ADR-0058).
        # This ensures that any caller completing the OAuth flow results in persisted credentials
        # and a single mcp.auth_updated emission from store_tokens().
        await self.store_tokens(
            server_id=server_id,
            tokens=tokens,
            principal_id=principal_id,
            tenant_id=tenant_id,
            motet_id=motet_id,
            emit_auth_updated_event=True,
            event_source="oauth_callback",
        )
        
        return tokens, state_data
    
    async def store_tokens(
        self,
        server_id: str,
        tokens: Dict[str, Any],
        principal_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        emit_auth_updated_event: bool = False,
        event_source: str = "oauth_manager",
    ) -> None:
        """
        Store OAuth tokens in vault.
        
        Args:
            server_id: MCP server identifier
            tokens: Token dictionary to store
            principal_id: Principal ID for per-user credentials
            tenant_id: Tenant ID for multi-tenant credential isolation
            motet_id: Motet ID for per-motet credential isolation (ADR-0058)
            emit_auth_updated_event: If True, emit mcp.auth_updated after storing (ADR-0057)
            event_source: Event source identifier for emitted auth events
        """
        from ..security.vault_service import CredentialScope, CredentialSecurityLevel

        principal_id = (principal_id or "").strip()
        tenant_id = (tenant_id or "").strip()
        motet_id = (motet_id or "").strip() or SYSTEM_MOTET_ID
        if not principal_id:
            raise ValueError("principal_id is required for store_tokens")
        if not tenant_id:
            raise ValueError("tenant_id is required for store_tokens")
        
        # Build context with principal/tenant/motet for proper credential scoping
        context = self._get_context_for_principal(principal_id, tenant_id, motet_id)
        
        # Determine credential key and scope based on principal/tenant/motet (ADR-0058)
        credential_key = _make_oauth_tokens_key(server_id, tenant_id, motet_id, principal_id)
        
        # Principal-scoped credential for authenticated/synthetic principal flows.
        scope = CredentialScope.PRINCIPAL
        
        # Remove _needs_reauth flag from new tokens (successful auth clears this)
        tokens_to_store = tokens.copy()
        tokens_to_store.pop("_needs_reauth", None)
        
        # Store tokens
        self.vault_client.store_credential(
            credential_key=credential_key,
            credential_data=tokens_to_store,
            context=context,
            scope=scope,
            security_level=CredentialSecurityLevel.CONFIDENTIAL,
        )
        
        logger.info("OAuth tokens stored in vault",
                   server_id=server_id,
                   credential_key=credential_key,
                   principal_id=principal_id,
                   tenant_id=tenant_id,
                   motet_id=motet_id,
                   scope=scope.value,
                   has_access_token=bool(tokens_to_store.get("access_token")),
                   cleared_needs_reauth=("_needs_reauth" in tokens))

        # Emit mcp.auth_updated for system notification when requested (ADR-0057).
        # This is the "commit point" for OAuth tokens; callers decide when an update
        # should notify the rest of the system (e.g., OAuth callback, not token validation flags).
        if emit_auth_updated_event:
            try:
                from ..workers.events import global_bus
                from ..workers.observers import EventPriority

                scopes = tokens_to_store.get("scope", "")
                if isinstance(scopes, str):
                    scopes_list = scopes.split() if scopes else []
                else:
                    scopes_list = []

                global_bus.publish({
                    "kind": "mcp.auth_updated",
                    "source": event_source,
                    "priority": EventPriority.HIGH.value,
                    "data": {
                        "service_id": server_id,
                        "principal_id": principal_id,
                        "tenant_id": tenant_id,
                        "motet_id": motet_id,
                        "status": "authorized",
                        "scopes": scopes_list,
                        "expires_at": tokens_to_store.get("expires_at"),
                    }
                })
                logger.info(
                    "Emitted mcp.auth_updated event",
                    server_id=server_id,
                    principal_id=principal_id,
                    tenant_id=tenant_id,
                    motet_id=motet_id,
                    source=event_source,
                )
            except Exception as event_error:
                logger.warning(
                    "Failed to emit mcp.auth_updated event",
                    server_id=server_id,
                    error=str(event_error),
                    exc_info=True,
                )
    
    async def get_tokens(
        self,
        server_id: str,
        principal_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        motet_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Get OAuth tokens from vault.
        
        Looks up tokens in order of specificity (ADR-0058):
        1. Per-principal, per-tenant, per-motet (if all provided)
        2. Per-tenant, per-motet (if tenant_id and motet_id provided)
        3. Per-tenant (if tenant_id provided)
        4. Global fallback
        
        Args:
            server_id: MCP server identifier
            principal_id: Principal ID for per-user credentials
            tenant_id: Tenant ID for multi-tenant credential isolation
            motet_id: Motet ID for per-motet credential isolation (ADR-0058)
            
        Returns:
            Token dictionary if found, None otherwise
        """
        # Normalize empty strings to "default" to match storage format
        # Empty strings are falsy and prevent USER scope key generation
        # But tokens are stored with "default" when motet_id is empty
        normalized_tenant_id = tenant_id if tenant_id else None
        normalized_motet_id = motet_id if motet_id else "default"  # Use "default" instead of None
        normalized_principal_id = principal_id if principal_id else None
        log_principal_id = normalized_principal_id or SYSTEM_PRINCIPAL_ID
        log_tenant_id = normalized_tenant_id or SYSTEM_TENANT_ID
        log_motet_id = normalized_motet_id or SYSTEM_MOTET_ID
        
        context = self._get_context_for_principal(normalized_principal_id, normalized_tenant_id, normalized_motet_id)
        
        # Try keys in order of specificity (ADR-0058 format)
        keys_to_try = _get_oauth_tokens_key_candidates(server_id, normalized_tenant_id, normalized_motet_id, normalized_principal_id)
        
        logger.info("OAuth token lookup",
                   server_id=server_id,
                   principal_id=log_principal_id,
                   tenant_id=log_tenant_id,
                   motet_id=log_motet_id,
                   keys_to_try=keys_to_try)
        
        for credential_key in keys_to_try:
            tokens = self.vault_client.get_credential(
                credential_key=credential_key,
                context=context
            )
            if tokens:
                logger.info("OAuth tokens found",
                           server_id=server_id,
                           credential_key=credential_key,
                           principal_id=log_principal_id,
                           tenant_id=log_tenant_id,
                           motet_id=log_motet_id,
                           has_access_token=bool(tokens.get("access_token")))
                return tokens
            else:
                logger.debug("OAuth token not found for key",
                           server_id=server_id,
                           credential_key=credential_key)
        
        logger.warning("OAuth tokens not found for any key",
                      server_id=server_id,
                      principal_id=log_principal_id,
                      tenant_id=log_tenant_id,
                      motet_id=log_motet_id,
                      keys_tried=keys_to_try)
        return None
    
    async def revoke_credentials(
        self,
        server_id: str,
        principal_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        revoke_at_provider: bool = False
    ) -> Dict[str, Any]:
        """
        Revoke OAuth credentials (shared method for API and tools).
        
        This method:
        1. Deletes tokens from vault
        2. Optionally revokes at OAuth provider
        3. Emits mcp.auth_revoked event for system notification
        
        Args:
            server_id: MCP server identifier
            principal_id: Principal ID for per-user credentials
            tenant_id: Tenant ID for multi-tenant credential isolation
            revoke_at_provider: If True, also revokes token at OAuth provider
            
        Returns:
            Dict with:
            - success: bool - Whether revocation succeeded
            - provider_revoked: bool - Whether provider revocation succeeded (if attempted)
            - message: str - Human-readable message
        """
        from ..workers.events import global_bus
        from ..workers.observers import EventPriority
        
        # Check if credentials exist first (use get_tokens, not get_oauth_status)
        # We want to delete tokens even if they're expired/invalid
        tokens = await self.get_tokens(server_id, principal_id, tenant_id, motet_id)
        if not tokens:
            return {
                "success": False,
                "provider_revoked": False,
                "message": f"No OAuth credentials found for {server_id}"
            }
        
        # Delete tokens from vault
        deleted = await self.delete_tokens(server_id, principal_id, tenant_id, motet_id)
        
        if not deleted:
            return {
                "success": False,
                "provider_revoked": False,
                "message": f"Failed to delete OAuth tokens for {server_id}"
            }
        
        # Optionally revoke at provider
        provider_revoked = False
        if revoke_at_provider and tokens:
            try:
                await self._revoke_at_provider(server_id, tokens)
                provider_revoked = True
                logger.info("Token revoked at OAuth provider",
                           server_id=server_id)
            except Exception as e:
                logger.warning("Failed to revoke token at provider (local deletion succeeded)",
                             server_id=server_id,
                             error=str(e))
        
        # Emit mcp.auth_revoked event for system notification
        event_principal_id = (principal_id or "").strip() or SYSTEM_PRINCIPAL_ID
        event_tenant_id = (tenant_id or "").strip() or SYSTEM_TENANT_ID
        event_motet_id = (motet_id or "").strip() or SYSTEM_MOTET_ID
        try:
            global_bus.publish({
                "kind": "mcp.auth_revoked",
                "source": "oauth_manager",
                "priority": EventPriority.HIGH.value,
                "data": {
                    "service_id": server_id,
                    "principal_id": event_principal_id,
                    "tenant_id": event_tenant_id,
                    "motet_id": event_motet_id,
                    "revoked_at_provider": provider_revoked
                }
            })
            logger.info("Emitted mcp.auth_revoked event",
                       server_id=server_id,
                       principal_id=event_principal_id,
                       tenant_id=event_tenant_id,
                       motet_id=event_motet_id)
        except Exception as event_error:
            logger.warning("Failed to emit mcp.auth_revoked event",
                         server_id=server_id,
                         error=str(event_error),
                         exc_info=True)
        
        message = f"OAuth credentials for {server_id} revoked successfully"
        if revoke_at_provider:
            if provider_revoked:
                message += " (also revoked at provider)"
            else:
                message += " (local only - provider revocation failed)"
        
        logger.info("OAuth credentials revoked",
                   server_id=server_id,
                   principal_id=event_principal_id,
                   tenant_id=event_tenant_id,
                   motet_id=event_motet_id,
                   revoked_at_provider=provider_revoked)
        
        return {
            "success": True,
            "provider_revoked": provider_revoked,
            "message": message
        }
    
    async def _revoke_at_provider(self, server_id: str, tokens: Dict[str, Any]) -> None:
        """
        Revoke token at the OAuth provider using config from YAML.
        
        Args:
            server_id: MCP server identifier
            tokens: Token dictionary with access_token
        """
        import httpx
        
        access_token = tokens.get("access_token")
        if not access_token:
            return
        
        # Get revoke URL from YAML config
        from ..tools.mcp_motet.proxy.mcp_instance_manager import get_oauth_providers_from_config
        
        providers = get_oauth_providers_from_config()
        provider_config = providers.get(server_id)
        
        if not provider_config or not provider_config.get("revoke_url"):
            logger.warning("No revoke_url configured for provider",
                         server_id=server_id)
            return
        
        revoke_url = provider_config["revoke_url"]
        provider_type = provider_config.get("provider", server_id.lower())
        
        # Google OAuth uses POST with token in body
        if provider_type in ["google", "google_workspace"]:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    revoke_url,
                    data={"token": access_token}
                )
                if response.status_code not in [200, 204]:
                    raise Exception(f"Google revocation failed: {response.status_code}")
        
        # GitHub OAuth revocation requires client_id (future enhancement)
        elif provider_type == "github":
            logger.warning("GitHub revocation requires client_id, skipping provider revocation",
                         server_id=server_id)
            return
        
        # Generic revocation for other providers (POST with token)
        else:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    revoke_url,
                    data={"token": access_token}
                )
                if response.status_code not in [200, 204]:
                    logger.warning("Revocation returned non-success status",
                                 server_id=server_id,
                                 status_code=response.status_code)
    
    async def delete_tokens(
        self,
        server_id: str,
        principal_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        motet_id: Optional[str] = None
    ) -> bool:
        """
        Delete OAuth tokens from vault (low-level method).
        
        This is the core deletion logic. For full revocation with events,
        use revoke_credentials() instead.
        
        Deletes tokens for the most specific key that matches (ADR-0058):
        1. Per-principal, per-tenant, per-motet (if all provided)
        2. Per-tenant, per-motet (if tenant_id and motet_id provided)
        3. Per-tenant (if tenant_id provided)
        4. Global fallback
        
        Args:
            server_id: MCP server identifier
            principal_id: Principal ID for per-user credentials
            tenant_id: Tenant ID for multi-tenant credential isolation
            motet_id: Motet ID for per-motet credential isolation (ADR-0058)
            
        Returns:
            True if tokens were deleted, False if not found
        """
        context = self._get_context_for_principal(principal_id, tenant_id, motet_id)
        
        # Determine credential key based on provided identifiers (ADR-0058 format)
        credential_key = _make_oauth_tokens_key(server_id, tenant_id, motet_id, principal_id)
        
        try:
            success = self.vault_client.delete_credential(
                credential_key=credential_key,
                context=context
            )
            
            logger.info("OAuth tokens deleted from vault",
                       server_id=server_id,
                       credential_key=credential_key,
                       principal_id=(principal_id or "").strip() or SYSTEM_PRINCIPAL_ID,
                       tenant_id=(tenant_id or "").strip() or SYSTEM_TENANT_ID,
                       motet_id=(motet_id or "").strip() or SYSTEM_MOTET_ID,
                       success=success)
            
            return success
        except Exception as e:
            logger.error("Failed to delete OAuth tokens",
                        server_id=server_id,
                        credential_key=credential_key,
                        error=str(e))
            return False
    
    async def _test_token_validity(
        self,
        server_id: str,
        tokens: Dict[str, Any]
    ) -> tuple[bool, Optional[Dict[str, Any]]]:
        """
        Test if OAuth tokens are actually valid by making an API call.
        
        Uses tokeninfo_url from YAML config if available. Falls back to
        _needs_reauth flag if no tokeninfo_url is configured.
        
        Args:
            server_id: MCP server identifier
            tokens: Token dictionary with access_token
        
        Returns:
            Tuple of (is_valid, token_info) where token_info contains validated scopes, expiry, etc.
        """
        # Get tokeninfo_url from YAML config
        from .vault_mcp_integration import get_service_auth_config
        
        auth_config = get_service_auth_config(server_id)
        tokeninfo_url = auth_config.get("tokeninfo_url") if auth_config else None
        
        if not tokeninfo_url:
            # No tokeninfo endpoint configured - fall back to _needs_reauth flag
            is_valid = not tokens.get("_needs_reauth", False)
            logger.debug("No tokeninfo_url configured, using _needs_reauth flag",
                        server_id=server_id,
                        is_valid=is_valid)
            return is_valid, None
        
        # Validate token using provider's tokeninfo endpoint
        try:
            async with aiohttp.ClientSession() as session:
                access_token = tokens.get('access_token', '')
                
                if not access_token:
                    logger.warning("No access_token in tokens for validation",
                                 server_id=server_id)
                    return False, None
                
                logger.info("Testing token validity",
                           server_id=server_id,
                           tokeninfo_url=tokeninfo_url,
                           access_token_length=len(access_token),
                           access_token_prefix=access_token[:20] if access_token else "NONE")
                
                # Build URL - support both query param and path-based endpoints
                # Google: https://.../tokeninfo?access_token=...
                # Others might use: https://.../tokeninfo/{token} or POST with body
                if "?" in tokeninfo_url:
                    # URL already has query params
                    validation_url = f"{tokeninfo_url}&access_token={access_token}"
                else:
                    # Add access_token as query param
                    validation_url = f"{tokeninfo_url}?access_token={access_token}"
                
                async with session.get(
                    validation_url,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        token_info = await resp.json()
                        
                        # Extract scopes - handle different response formats
                        scope_str = token_info.get("scope", "")
                        if isinstance(scope_str, str):
                            scopes = scope_str.split()
                        else:
                            scopes = scope_str if isinstance(scope_str, list) else []
                        
                        logger.info("Token validation successful",
                                   server_id=server_id,
                                   status=resp.status,
                                   scope_count=len(scopes),
                                   expires_in=token_info.get("expires_in"))
                        return True, token_info
                    
                    # Non-200 response
                    response_text = await resp.text()
                    logger.warning("Token validation failed",
                                 server_id=server_id,
                                 status=resp.status,
                                 error_response=response_text[:200])
                    return False, None
        except Exception as e:
            logger.error("Token validation error",
                       server_id=server_id,
                       tokeninfo_url=tokeninfo_url,
                       error=str(e),
                       exc_info=True)
            return False, None
    
    async def get_oauth_status(
        self,
        server_id: str,
        principal_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        motet_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get OAuth status for an MCP server with active validation.
        
        Tests tokens with the actual API to ensure they're valid,
        rather than just trusting Redis flags.
        
        Args:
            server_id: MCP server identifier
            principal_id: Principal ID for per-user credentials
            tenant_id: Tenant ID for multi-tenant credential isolation
        
        Returns:
            Status dictionary with authentication info
        """
        # Get tokens using the fallback lookup logic (includes ADR-0058 format)
        tokens = await self.get_tokens(server_id, principal_id, tenant_id, motet_id)
        
        if not tokens:
            return {
                "configured": False,
                "authenticated": False,
                "server_id": server_id,
                "principal_id": (principal_id or "").strip() or SYSTEM_PRINCIPAL_ID,
                "tenant_id": (tenant_id or "").strip() or SYSTEM_TENANT_ID,
                "motet_id": (motet_id or "").strip() or SYSTEM_MOTET_ID,
            }
        
        expires_at = tokens.get("expires_at")
        if expires_at:
            expires_at_dt = datetime.fromisoformat(expires_at)
            is_expired = datetime.utcnow() > expires_at_dt
            expires_in_seconds = (expires_at_dt - datetime.utcnow()).total_seconds()
        else:
            is_expired = None
            expires_in_seconds = None
        
        # ACTIVE VALIDATION: Test tokens with real API call
        is_valid, token_info = await self._test_token_validity(server_id, tokens)
        
        # If tokens are invalid, mark them as needing reauth
        if not is_valid and not tokens.get("_needs_reauth"):
            logger.warning("Token validation failed - marking as needing reauth",
                         server_id=server_id,
                         principal_id=(principal_id or "").strip() or SYSTEM_PRINCIPAL_ID,
                         tenant_id=(tenant_id or "").strip() or SYSTEM_TENANT_ID,
                         motet_id=(motet_id or "").strip() or SYSTEM_MOTET_ID)
            
            # Mark tokens as needing reauth by re-storing with flag
            tokens_with_flag = tokens.copy()
            tokens_with_flag["_needs_reauth"] = True
            
            # Store updated tokens with flag (uses same principal/tenant/motet scoping)
            await self.store_tokens(
                server_id=server_id,
                tokens=tokens_with_flag,
                principal_id=principal_id,
                tenant_id=tenant_id,
                motet_id=motet_id
            )
            
            logger.info("Marked tokens as needing reauth",
                       server_id=server_id,
                       principal_id=(principal_id or "").strip() or SYSTEM_PRINCIPAL_ID,
                       tenant_id=(tenant_id or "").strip() or SYSTEM_TENANT_ID,
                       motet_id=(motet_id or "").strip() or SYSTEM_MOTET_ID)
        
        authenticated = is_valid
        needs_reauth = not is_valid
        
        # Use validated scopes from tokeninfo if available, otherwise fall back to stored scopes
        if token_info and "scope" in token_info:
            validated_scopes = token_info["scope"].split()
        else:
            validated_scopes = tokens.get("scope", "").split()
        
        # If we have tokeninfo, use its expiry information
        if token_info and "expires_in" in token_info:
            expires_in_seconds = int(token_info["expires_in"])
            is_expired = False
        
        return {
            "configured": True,
            "authenticated": authenticated,
            "needs_reauth": needs_reauth,
            "has_refresh_token": bool(tokens.get("refresh_token")),
            "expires_at": expires_at,
            "is_expired": is_expired,
            "expires_in_seconds": expires_in_seconds,
            "scopes": validated_scopes,
            "validated": token_info is not None,  # New field to indicate if scopes are validated
            "server_id": server_id,
            "principal_id": (principal_id or "").strip() or SYSTEM_PRINCIPAL_ID,
            "tenant_id": (tenant_id or "").strip() or SYSTEM_TENANT_ID,
            "motet_id": (motet_id or "").strip() or SYSTEM_MOTET_ID,
        }
    
    async def refresh_tokens(
        self,
        server_id: str,
        principal_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        motet_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Refresh OAuth tokens.
        
        Args:
            server_id: MCP server identifier
            principal_id: Principal ID for per-user credentials
            tenant_id: Tenant ID for multi-tenant credential isolation
            motet_id: Motet ID for per-motet credential isolation (ADR-0058)
        
        Returns:
            Refreshed token dictionary
        """
        # Get tokens using the fallback lookup logic (includes ADR-0058 format)
        tokens = await self.get_tokens(server_id, principal_id, tenant_id, motet_id)
        
        if not tokens or not tokens.get("refresh_token"):
            raise ValueError(f"No refresh token available for {server_id}")
        
        config = _get_oauth_config_from_yaml(server_id)
        if not config:
            raise ValueError(f"No OAuth config for server: {server_id}. Please configure auth section in mcp_instance_manager.yaml")
        
        # Refresh tokens
        async with aiohttp.ClientSession() as session:
            async with session.post(
                tokens["token_uri"],
                data={
                    "client_id": tokens["client_id"],
                    "client_secret": tokens["client_secret"],
                    "refresh_token": tokens["refresh_token"],
                    "grant_type": "refresh_token"
                }
            ) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    raise ValueError(f"Token refresh failed: {error}")
                
                new_tokens = await resp.json()
        
        # Update tokens (preserve fields not returned)
        if "refresh_token" not in new_tokens:
            new_tokens["refresh_token"] = tokens["refresh_token"]
        
        new_tokens["client_id"] = tokens["client_id"]
        new_tokens["client_secret"] = tokens["client_secret"]
        new_tokens["token_uri"] = tokens["token_uri"]
        
        expires_in = new_tokens.get("expires_in", 3600)
        new_tokens["expires_at"] = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()
        
        # Store updated tokens with same principal/tenant/motet scoping.
        # This is a real credential update (access token rotation), so emit mcp.auth_updated
        # to trigger instance refresh/restart (ADR-0057).
        await self.store_tokens(
            server_id=server_id,
            tokens=new_tokens,
            principal_id=principal_id,
            tenant_id=tenant_id,
            motet_id=motet_id,
            emit_auth_updated_event=True,
            event_source="oauth_refresh",
        )
        
        logger.info("OAuth tokens refreshed",
                   server_id=server_id,
                   principal_id=(principal_id or "").strip() or SYSTEM_PRINCIPAL_ID,
                   tenant_id=(tenant_id or "").strip() or SYSTEM_TENANT_ID,
                   motet_id=(motet_id or "").strip() or SYSTEM_MOTET_ID)
        
        return new_tokens
    
    def _get_context_for_principal(
        self,
        principal_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        motet_id: Optional[str] = None
    ) -> CommandContext:
        """
        Get command context for a specific principal/tenant/motet.
        
        Args:
            principal_id: Principal ID (user ID)
            tenant_id: Tenant ID
            motet_id: Motet ID (ADR-0058)
            
        Returns:
            CommandContext with proper principal/tenant/motet scoping
        """
        return CommandContext(
            task_id="oauth_manager",
            principal_id=principal_id or "",
            tenant_id=tenant_id or SYSTEM_TENANT_ID,
            motet_id=motet_id or SYSTEM_MOTET_ID
        )
    
    def _get_system_context(self) -> CommandContext:
        """Get system context for vault operations."""
        return CommandContext(
            task_id="oauth_manager",
            principal_id="",  # Empty for global scope
            tenant_id=SYSTEM_TENANT_ID,
            motet_id=SYSTEM_MOTET_ID
        )


# Singleton instance
_oauth_manager: Optional[OAuthManager] = None


def get_oauth_manager() -> OAuthManager:
    """Get the global OAuth manager instance."""
    global _oauth_manager
    if _oauth_manager is None:
        _oauth_manager = OAuthManager()
    return _oauth_manager

