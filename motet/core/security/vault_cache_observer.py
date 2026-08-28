"""
Motet - Vault Cache Observer

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Event-driven vault cache invalidation observer for the Motet.
    Listens for mcp.auth_updated and mcp.auth_revoked events and clears
    the vault client's in-memory cache to ensure fresh credentials are
    fetched from Redis on subsequent lookups.

Dependencies:
    - structlog: Structured logging
    - Event system and observer pattern
    - Vault client for cache access

Usage:
    from motet.core.security.vault_cache_observer import register_vault_cache_observer
    
    # Register during API startup
    register_vault_cache_observer()

Notes:
    - Ensures vault client cache is invalidated when OAuth tokens change
    - Prevents stale cached credentials from being used after auth events
    - Part of MCP OAuth Prompt Flow for Missing Authorization
"""

import structlog
from typing import Optional

from ..workers import Observer, EventFilter, Event, register_event_observer
from .oauth_manager import _get_oauth_tokens_key_candidates

logger = structlog.get_logger(__name__)


class VaultCacheObserver(Observer):
    """
    Observer that clears the vault client's in-memory cache when auth events occur.
    
    This ensures that fresh credentials are fetched from Redis after:
    - User completes OAuth authorization (mcp.auth_updated)
    - User revokes/disconnects OAuth credentials (mcp.auth_revoked)
    
    Without this observer, the vault client might serve stale cached credentials
    for up to 5 minutes (the default cache TTL).
    """
    
    def __init__(self):
        """Initialize the vault cache observer."""
        super().__init__(name="vault_cache")
        logger.info("🗑️ VaultCacheObserver initialized")
    
    def get_event_filter(self) -> EventFilter:
        """
        Filter for mcp.auth_updated and mcp.auth_revoked events.
        
        Listens for events from:
        - oauth_callback: OAuth API callback endpoint
        - oauth_revoke: OAuth API revoke endpoint
        - oauth_logout: Built-in tool for disconnecting OAuth services
        """
        return EventFilter(
            event_types={"mcp.auth_updated", "mcp.auth_revoked"},
            sources={"oauth_callback", "oauth_revoke", "oauth_logout"},
            tags=set(),
        )
    
    def on_event(self, event: Event) -> None:
        """
        Handle auth events by clearing the vault cache.
        
        Args:
            event: Event containing auth update/revoke details
        """
        try:
            event_kind = event.event_type
            data = event.data if hasattr(event, 'data') else {}
            service_id = data.get('service_id')
            principal_id = data.get('principal_id')
            tenant_id = data.get('tenant_id')
            motet_id = data.get('motet_id')  # ADR-0058: Extract motet_id from event
            
            if not service_id:
                logger.warning(f"⚠️ {event_kind} event missing service_id")
                return
            
            logger.info(
                f"🗑️ VaultCacheObserver: Clearing cache for {event_kind}",
                service_id=service_id,
                principal_id=principal_id,
                tenant_id=tenant_id,
                motet_id=motet_id
            )
            
            # Clear the vault client cache (ADR-0058 format)
            self._clear_vault_cache(service_id, principal_id, tenant_id, motet_id)
            
        except Exception as e:
            logger.error(
                f"❌ VaultCacheObserver error: {e}",
                exc_info=True
            )
    
    def _clear_vault_cache(
        self,
        service_id: str,
        principal_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        motet_id: Optional[str] = None
    ) -> None:
        """
        Clear the vault client's in-memory cache for OAuth credentials.
        
        Args:
            service_id: MCP service identifier
            principal_id: User who completed authorization
            tenant_id: Tenant context
            motet_id: Motet context (ADR-0058)
        """
        try:
            from .vault_client import get_vault_client
            
            vault_client = get_vault_client()
            
            # Build the credential key patterns that might be cached
            # These match the patterns used in oauth_manager.py (ADR-0058 format)
            keys_to_clear = _get_oauth_tokens_key_candidates(service_id, tenant_id, motet_id, principal_id)
            
            # Clear each potential cache key
            cleared_count = 0
            for credential_key in keys_to_clear:
                # Build cache keys for both with and without principal_id prefix
                cache_keys_to_try = [
                    vault_client._make_cache_key(principal_id or "", credential_key),
                    vault_client._make_cache_key("", credential_key),
                ]
                
                for cache_key in cache_keys_to_try:
                    if cache_key in vault_client._local_cache:
                        del vault_client._local_cache[cache_key]
                        cleared_count += 1
                    if cache_key in vault_client._cache_timestamps:
                        del vault_client._cache_timestamps[cache_key]
            
            if cleared_count > 0:
                logger.info(
                    f"🗑️ Cleared {cleared_count} vault cache entries",
                    service_id=service_id,
                    principal_id=principal_id,
                    tenant_id=tenant_id
                )
            else:
                logger.debug(
                    f"ℹ️ No vault cache entries to clear",
                    service_id=service_id,
                    principal_id=principal_id,
                    tenant_id=tenant_id
                )
                
        except Exception as e:
            logger.warning(
                f"⚠️ Failed to clear vault cache: {e}",
                service_id=service_id,
                principal_id=principal_id,
                tenant_id=tenant_id
            )


# Singleton instance
_vault_cache_observer: Optional[VaultCacheObserver] = None


def register_vault_cache_observer() -> VaultCacheObserver:
    """
    Register the vault cache observer with the event system.
    
    This should be called during API startup to enable event-driven
    cache invalidation after OAuth events.
    
    Returns:
        The registered observer
    """
    global _vault_cache_observer
    
    if _vault_cache_observer is None:
        _vault_cache_observer = VaultCacheObserver()
        register_event_observer(_vault_cache_observer)
        logger.info("✅ VaultCacheObserver registered with event system")
    
    return _vault_cache_observer

