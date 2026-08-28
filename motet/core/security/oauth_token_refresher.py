"""
Motet - Oauth Token Refresher

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Oauth Token Refresher for the Motet distributed framework.

Dependencies:
    - typing: Type hints and annotations
    - Base interfaces and implementations

Usage:
    from motet.core.security.oauth_token_refresher import OauthTokenRefresher

Notes:
    - Provides core functionality
    - Integrates with distributed architecture
"""


import asyncio
import os
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
import structlog

from .oauth_manager import get_oauth_manager, _make_oauth_tokens_key
from motet.core.tools.mcp_motet.proxy.mcp_instance_manager import get_oauth_providers_from_config

logger = structlog.get_logger(__name__)

# Refresh tokens when they have less than this many seconds left
from .system_principals import (
    SYSTEM_PRINCIPAL_OAUTH_REFRESHER as SYSTEM_PRINCIPAL_ID,
    SYSTEM_TENANT_ID,
    SYSTEM_MOTET_ID,
)

REFRESH_THRESHOLD_SECONDS = 300  # 5 minutes

# How often to check for tokens that need refresh
CHECK_INTERVAL_SECONDS = 60  # 1 minute


class OAuthTokenRefresher:
    """Background task that automatically refreshes OAuth tokens."""
    
    def __init__(self):
        self.oauth_manager = get_oauth_manager()
        self.running = False
        self._task: Optional[asyncio.Task] = None
    
    async def start(self):
        """Start the background refresh task."""
        if self.running:
            logger.warning("OAuth token refresher already running")
            return
        
        self.running = True
        self._task = asyncio.create_task(self._refresh_loop())
        logger.info("OAuth token refresher started",
                   check_interval=CHECK_INTERVAL_SECONDS,
                   refresh_threshold=REFRESH_THRESHOLD_SECONDS)
    
    async def stop(self):
        """Stop the background refresh task."""
        if not self.running:
            return
        
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        
        logger.info("OAuth token refresher stopped")
    
    async def _refresh_loop(self):
        """Main refresh loop that runs continuously."""
        logger.info("OAuth token refresh loop started")
        
        while self.running:
            try:
                await self._check_and_refresh_tokens()
            except Exception as e:
                logger.error("Error in token refresh loop",
                           error=str(e),
                           error_type=type(e).__name__,
                           exc_info=True)
            
            # Wait before next check
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
    
    async def _check_and_refresh_tokens(self):
        """Check all configured servers and refresh tokens if needed."""
        # Get OAuth providers from YAML config (ADR-0057 Phase 4)
        oauth_providers = get_oauth_providers_from_config()
        server_ids = list(oauth_providers.keys())
        
        for server_id in server_ids:
            try:
                await self._check_server_tokens(server_id)
            except Exception as e:
                logger.error("Error checking tokens for server",
                           server_id=server_id,
                           error=str(e),
                           exc_info=True)
    
    async def _check_server_tokens(self, server_id: str):
        """
        Check if tokens for a server need refresh and refresh if needed.
        
        Args:
            server_id: MCP server identifier (e.g., "google_workspace")
        """
        # Get status (token refresher operates on global tokens - no user context)
        # Pass None for principal_id, tenant_id, motet_id to check global tokens
        status = await self.oauth_manager.get_oauth_status(
            server_id=server_id,
            principal_id=SYSTEM_PRINCIPAL_ID,
            tenant_id=SYSTEM_TENANT_ID,
            motet_id=SYSTEM_MOTET_ID,
        )
        
        # Skip if not configured or authenticated
        if not status.get("configured") or not status.get("authenticated"):
            return
        
        # Skip if no refresh token
        if not status.get("has_refresh_token"):
            logger.debug("No refresh token available for server",
                        server_id=server_id)
            return
        
        # Check if expired or expiring soon
        expires_at = status.get("expires_at")
        if not expires_at:
            logger.warning("No expiry time for token",
                          server_id=server_id)
            return
        
        expires_at_dt = datetime.fromisoformat(expires_at)
        time_until_expiry = (expires_at_dt - datetime.utcnow()).total_seconds()
        
        # Refresh if expired or expiring soon
        if time_until_expiry <= REFRESH_THRESHOLD_SECONDS:
            logger.info("Refreshing OAuth tokens",
                       server_id=server_id,
                       time_until_expiry=time_until_expiry,
                       threshold=REFRESH_THRESHOLD_SECONDS)
            
            try:
                # Token refresher operates on global tokens (no user context)
                await self.oauth_manager.refresh_tokens(
                    server_id=server_id,
                    principal_id=SYSTEM_PRINCIPAL_ID,
                    tenant_id=SYSTEM_TENANT_ID,
                    motet_id=SYSTEM_MOTET_ID,
                )
                logger.info("OAuth tokens refreshed successfully",
                           server_id=server_id)
            except Exception as e:
                logger.error("Failed to refresh OAuth tokens - marking for reauth",
                           server_id=server_id,
                           error=str(e),
                           exc_info=True)
                
                # Mark credential as needing reauth
                await self._mark_needs_reauth(server_id)
        else:
            logger.debug("Token still valid",
                        server_id=server_id,
                        time_until_expiry=time_until_expiry)
    
    async def _mark_needs_reauth(self, server_id: str) -> None:
        """
        Mark tokens as needing reauth when refresh fails.
        
        Args:
            server_id: MCP server identifier
        """
        from motet.core.security.vault_client import get_vault_client
        from motet.core.commands.base import CommandContext
        
        # Get current tokens using the same explicit system identity context.
        context = CommandContext(
            task_id="token_refresher",
            tenant_id=SYSTEM_TENANT_ID,
            principal_id=SYSTEM_PRINCIPAL_ID,
            motet_id=SYSTEM_MOTET_ID,
            conversation_id="",
        )
        
        vault_client = get_vault_client()
        
        # Determine credential key (ADR-0058 format) for the explicit system principal.
        credential_key = _make_oauth_tokens_key(
            server_id=server_id,
            tenant_id=SYSTEM_TENANT_ID,
            motet_id=SYSTEM_MOTET_ID,
            principal_id=SYSTEM_PRINCIPAL_ID,
        )
        
        tokens = vault_client.get_credential(credential_key, context=context)
        
        if tokens:
            # Mark as needing reauth
            tokens["_needs_reauth"] = True
            
            # Store back to vault
            vault_client.store_credential(
                credential_key=credential_key,
                credential_data=tokens,
                context=context,
            )
            
            logger.info("Marked credential as needing reauth",
                       server_id=server_id,
                       credential_key=credential_key)


# Global instance
_token_refresher: Optional[OAuthTokenRefresher] = None


def get_token_refresher() -> OAuthTokenRefresher:
    """Get the global token refresher instance."""
    global _token_refresher
    if _token_refresher is None:
        _token_refresher = OAuthTokenRefresher()
    return _token_refresher


async def start_token_refresher():
    """Start the global token refresher."""
    if os.getenv("MOTET_VAULT_ENABLED", "true").lower() != "true":
        logger.info("oauth_token_refresher_skipped_vault_disabled")
        return
    try:
        refresher = get_token_refresher()
    except Exception as exc:
        logger.error(
            "oauth_token_refresher_init_failed",
            error=str(exc),
            exc_info=True,
        )
        return
    await refresher.start()


async def stop_token_refresher():
    """Stop the global token refresher."""
    global _token_refresher
    if _token_refresher:
        await _token_refresher.stop()

