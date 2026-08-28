"""
Motet - MCP Instance Credentials

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Vault prefetch, auth-required events, and targeted credential refresh.

Dependencies:
    - asyncio: per-instance locks and background loops
    - structlog: structured logging

Usage:
    Mixed into MCPInstanceManager in instance_manager.py. Do not instantiate alone.

Notes:
    - Public import path remains motet.core.tools.mcp_motet.proxy.mcp_instance_manager
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import structlog

from motet.core.tools.mcp_motet.manager.config import (
    CredentialCheckResult,
    ServiceAuthConfig,
)
from motet.core.tools.mcp_motet.protocol import Visibility, parse_instance_key

logger = structlog.get_logger(__name__)


def _get_vault_client_for_context():
    """Return the vault client for the current worker mode (ADR-0095)."""
    if os.getenv("MOTET_VAULT_RESOLVE_URL", "").strip():
        from motet.core.edge.http_vault_client import HttpVaultClient
        return HttpVaultClient()
    from motet.core.security.vault_client import get_vault_client
    return get_vault_client()

class CredentialsMixin:
    async def _prefetch_vault_credentials(self) -> None:
        """
        Pre-fetch vault credentials for all configured services at startup.
        
        This method retrieves credentials from the vault and merges them into
        the service configuration environment variables, making them available
        when MCP server instances are created.
        """
        if not self.startup_command_context:
            logger.info("ℹ️ No startup context provided, skipping vault credential prefetch")
            return
        
        logger.info("🔐 Pre-fetching vault credentials for all MCP services...")
        
        prefetch_count = 0
        for service_id, service_config in self.service_configs.items():
            try:
                logger.info(f"🔐 Fetching vault credentials for service: {service_id}")
                
                # Import vault integration
                from motet.core.security.vault_mcp_integration import get_mcp_env_vars_from_vault
                
                # Fetch credentials from vault
                vault_env = get_mcp_env_vars_from_vault(service_id, self.startup_command_context)
                
                if vault_env:
                    # Merge vault credentials into service config environment
                    if service_config.env is None:
                        service_config.env = {}
                    
                    # Update with vault credentials (they override YAML config)
                    service_config.env.update(vault_env)
                    prefetch_count += 1
                    
                    logger.info(f"✅ Pre-fetched {len(vault_env)} vault credentials for {service_id}")
                    logger.debug(f"   Credential keys: {list(vault_env.keys())}")
                else:
                    logger.info(f"ℹ️ No vault credentials found for {service_id}")
                    
            except Exception as e:
                logger.warning(f"⚠️ Failed to pre-fetch vault credentials for {service_id}: {e}", exc_info=True)
                logger.info(f"ℹ️ Service {service_id} will use YAML config environment variables only")
        
        if prefetch_count > 0:
            logger.info(f"✅ Vault credential prefetch complete: {prefetch_count}/{len(self.service_configs)} services updated")
        else:
            logger.info(f"ℹ️ No vault credentials were prefetched for any service")

    def check_service_credentials(
        self,
        service_id: str,
        context: Optional[Any] = None
    ) -> CredentialCheckResult:
        """
        Check if a service has required credentials in vault (ADR-0057).
        
        For oauth2 type, emits mcp.auth_required if missing.
        For api_key/service_account, logs warning but doesn't prompt user.
        For none, always succeeds.
        
        Args:
            service_id: MCP service identifier
            context: CommandContext for vault lookup and event emission
            
        Returns:
            CredentialCheckResult indicating whether auth is required
        """
        service_config = self.service_configs.get(service_id)
        if not service_config:
            logger.warning(f"Service {service_id} not found in service configs")
            return CredentialCheckResult(env_vars={}, auth_required=False)
        
        auth_config = service_config.auth
        if not auth_config or auth_config.type == AuthType.NONE:
            # No auth required
            return CredentialCheckResult(env_vars={}, auth_required=False)
        
        if not auth_config.vault_credential_key:
            logger.warning(f"Service {service_id} has auth config but no vault_credential_key")
            return CredentialCheckResult(env_vars={}, auth_required=False)
        
        # Check vault for credentials (ADR-0095: uses HttpVaultClient on local workers)
        try:
            vault_client = _get_vault_client_for_context()
            
            credential_data = vault_client.get_credential(
                credential_key=auth_config.vault_credential_key,
                context=context  # type: ignore[arg-type]
            )
            
            if credential_data:
                # Extract token and build env vars
                env_vars = {}
                if auth_config.env_var and auth_config.token_field:
                    token_value = credential_data.get(auth_config.token_field)
                    if token_value:
                        env_vars[auth_config.env_var] = token_value
                        logger.debug(f"✅ Found credential for {service_id}: {auth_config.env_var}")
                return CredentialCheckResult(env_vars=env_vars, auth_required=False)
            
            # Credentials missing
            if auth_config.type == AuthType.OAUTH2:
                # User can authorize - emit event and return auth_required
                self._emit_auth_required_event(service_id, auth_config, context)
                return CredentialCheckResult(
                    env_vars={},
                    auth_required=True,
                    auth_config=auth_config,
                    missing_credential_key=auth_config.vault_credential_key
                )
            else:
                # api_key or service_account - admin must configure
                logger.warning(
                    f"Service {service_id} missing {auth_config.type.value} credentials. "
                    f"Admin must configure vault key: {auth_config.vault_credential_key}"
                )
                return CredentialCheckResult(env_vars={}, auth_required=False)
                
        except Exception as e:
            logger.error(f"Failed to check credentials for {service_id}: {e}", exc_info=True)
            return CredentialCheckResult(env_vars={}, auth_required=False)

    def _emit_auth_required_event(
        self,
        service_id: str,
        auth_config: ServiceAuthConfig,
        context: Optional[Any] = None
    ) -> None:
        """
        Emit mcp.auth_required event for user-facing OAuth prompt (ADR-0057).
        
        Args:
            service_id: MCP service identifier
            auth_config: Service auth configuration
            context: CommandContext for principal/tenant info
        """
        try:
            from motet.core.workers.events import global_bus
            
            # OAuth URLs must be explicitly configured in YAML (no fallbacks)
            # This ensures configuration errors are visible and prevents silent failures
            auth_url = auth_config.auth_url
            token_url = auth_config.token_url
            
            if not auth_url or not token_url:
                logger.warning("OAuth URLs not fully configured in YAML",
                             service_id=service_id,
                             provider=auth_config.provider,
                             has_auth_url=bool(auth_url),
                             has_token_url=bool(token_url))
                # Use empty strings if not configured (will cause OAuth flow to fail with clear error)
                auth_url = auth_url or ""
                token_url = token_url or ""
            
            # Build event data
            event_data = {
                "service_id": service_id,
                "provider": auth_config.provider,
                "auth_type": auth_config.type.value,
                "display_name": auth_config.display_name or service_id,
                "description": auth_config.description or "",
                "vault_credential_key": auth_config.vault_credential_key,
                "required_scopes": auth_config.scopes,
                "authorization_endpoint": f"/api/v1/oauth/{service_id}/initiate",
                "auth_url": auth_url,
                "token_url": token_url,
            }
            
            # Add context info if available
            if context:
                event_data["principal_id"] = getattr(context, "principal_id", None)
                event_data["tenant_id"] = getattr(context, "tenant_id", None)
                event_data["conversation_id"] = getattr(context, "conversation_id", None)
                event_data["task_id"] = getattr(context, "task_id", None)
            
            global_bus.publish({
                "kind": "mcp.auth_required",
                "source": "mcp_instance_manager",
                "priority": 8,
                "data": event_data
            })
            
            logger.info(
                f"🔐 Emitted mcp.auth_required event for {service_id}",
                extra={
                    "service_id": service_id,
                    "display_name": auth_config.display_name,
                    "provider": auth_config.provider
                }
            )
            
        except Exception as e:
            logger.error(f"Failed to emit auth_required event for {service_id}: {e}", exc_info=True)

    async def refresh_service_credentials(
        self, 
        service_id: str,
        principal_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        motet_id: Optional[str] = None
    ) -> bool:
        """
        Refresh credentials for a service after OAuth completion (ADR-0057).
        
        Called by MCPAuthObserver when mcp.auth_updated event is received.
        Re-fetches credentials from vault and updates running instances.
        
        Args:
            service_id: MCP service identifier
            principal_id: User who completed authorization (for per-user credentials)
            tenant_id: Tenant context for multi-tenant credential management
            motet_id: Motet ID for per-motet credential isolation (ADR-0058)
            
        Returns:
            True if credentials were refreshed successfully
        """
        service_config = self.service_configs.get(service_id)
        if not service_config:
            logger.warning(f"Cannot refresh credentials: service {service_id} not found")
            return False
        
        try:
            logger.info(f"🔄 Refreshing credentials for service: {service_id}",
                       principal_id=principal_id, tenant_id=tenant_id, motet_id=motet_id)
            
            # Use provided motet_id or fall back to environment variable
            effective_motet_id = motet_id or os.getenv("MOTET_MOTET_ID", "default")

            # CRITICAL: Clear vault cache before fetching to ensure fresh credentials
            # VaultCacheObserver should also clear it, but we do it here to avoid race conditions
            try:
                vault_client = _get_vault_client_for_context()
                
                # Clear cache for this service/principal/tenant/motet combination
                # Generate all possible credential keys that might be cached
                from motet.core.security.oauth_manager import _get_oauth_tokens_key_candidates
                cache_keys = _get_oauth_tokens_key_candidates(
                    server_id=service_id,
                    principal_id=principal_id,
                    tenant_id=tenant_id,
                    motet_id=effective_motet_id
                )
                
                for key in cache_keys:
                    # IMPORTANT: VaultClient.clear_cache() expects a principal_id, not a credential_key.
                    # We must clear the specific cached credential entry for this principal.
                    vault_client.clear_cached_credential(principal_id or "", key)
                
                logger.info(f"🗑️ Cleared vault cache for {len(cache_keys)} credential keys",
                           service_id=service_id, principal_id=principal_id, tenant_id=tenant_id, motet_id=effective_motet_id)
            except Exception as cache_error:
                logger.warning(f"⚠️ Failed to clear vault cache: {cache_error}", exc_info=True)
                # Continue anyway - VaultCacheObserver might have cleared it
            
            # Re-fetch from vault using the correct principal/tenant/motet context
            from motet.core.security.vault_mcp_integration import get_mcp_env_vars_from_vault
            
            # Create a context with the correct principal_id, tenant_id, and motet_id
            # This ensures we look up the per-user credentials, not global ones
            if principal_id or tenant_id or motet_id:
                from motet.core.commands.base import CommandContext
                
                refresh_context = CommandContext(
                    task_id="credential_refresh",
                    principal_id=principal_id or "",
                    tenant_id=tenant_id or "default",
                    motet_id=effective_motet_id
                )
            else:
                refresh_context = self.startup_command_context
            
            vault_env = get_mcp_env_vars_from_vault(service_id, refresh_context)  # type: ignore[arg-type]
            
            if vault_env:
                # Update service config env
                if service_config.env is None:
                    service_config.env = {}
                service_config.env.update(vault_env)
                
                logger.info(f"✅ Refreshed {len(vault_env)} credentials for {service_id}",
                           principal_id=principal_id, tenant_id=tenant_id, motet_id=effective_motet_id)
                
                # Restart instances to pick up new credentials
                # IMPORTANT: Target instances by principal_id/tenant_id for per-user services
                # to avoid restarting unrelated instances
                svc_cfg = self.service_configs.get(service_id)
                instances_to_restart = []
                for inst_id, inst in self.instances.items():
                    if inst.service_id != service_id:
                        continue
                    
                    # For per-user services, only restart instances matching principal_id/tenant_id/motet_id
                    # For shared services, restart all instances for the service
                    if svc_cfg and svc_cfg.visibility == Visibility.USER:
                        # Parse instance key to extract principal_id/tenant_id/motet_id
                        try:
                            parsed = parse_instance_key(service_id=service_id, visibility=svc_cfg.visibility, instance_key=inst_id)
                            # Match principal_id, tenant_id, and motet_id for USER visibility
                            if parsed.get("principal_id") == principal_id and parsed.get("tenant_id") == tenant_id:
                                if effective_motet_id is None or parsed.get("motet_id") == effective_motet_id:
                                    instances_to_restart.append(inst_id)
                        except (ValueError, KeyError):
                            # Fallback: check if principal_id, tenant_id, and motet_id are in instance_id string
                            if principal_id and principal_id in inst_id and tenant_id and tenant_id in inst_id:
                                if effective_motet_id is None or effective_motet_id in inst_id:
                                    instances_to_restart.append(inst_id)
                    else:
                        # For non-user services, restart all instances for the service
                        instances_to_restart.append(inst_id)
                
                logger.info(f"🔍 Found {len(instances_to_restart)} instances to restart for {service_id}",
                           instance_ids=instances_to_restart,
                           principal_id=principal_id,
                           tenant_id=tenant_id,
                           motet_id=effective_motet_id,
                           total_instances=len(self.instances),
                           service_visibility=svc_cfg.visibility.value if svc_cfg else "unknown")
                
                # Extract lifecycle IDs from instance keys before destroying
                # This allows us to recreate task/conversation/session-scoped instances
                instances_to_recreate = []
                for instance_id in instances_to_restart:
                    instance = self.instances.get(instance_id)
                    if not instance:
                        continue
                    
                    # Parse instance key to extract lifecycle IDs (task_id, conversation_id, session_id)
                    parsed = {}
                    try:
                        parsed = parse_instance_key(
                            service_id=service_id,
                            visibility=svc_cfg.visibility if svc_cfg else Visibility.GLOBAL,
                            instance_key=instance_id
                        )
                    except (ValueError, KeyError) as e:
                        logger.warning(f"Failed to parse instance_key {instance_id}: {e}")
                    
                    instances_to_recreate.append({
                        "instance_id": instance_id,
                        "parsed": parsed
                    })
                
                # Destroy all matching instances synchronously before returning
                # This ensures they're removed from self.instances before any new requests can reuse them
                for instance_info in instances_to_recreate:
                    instance_id = instance_info["instance_id"]
                    logger.info(f"🔄 Restarting instance {instance_id} with new credentials")
                    await self.destroy_instance(instance_id, reason="credential_refresh")
                
                # Recreate instances with extracted lifecycle IDs
                # This ensures task/conversation/session-scoped instances are recreated with same IDs
                eager_created = False
                try:
                    if svc_cfg:
                        # effective_motet_id is already normalized (line 620), no need to normalize again
                        for instance_info in instances_to_recreate:
                            parsed = instance_info["parsed"]
                            recreate_tenant_id = parsed.get("tenant_id") or tenant_id
                            recreate_principal_id = parsed.get("principal_id") or principal_id
                            # Use motet_id from parsed instance_key, or fall back to effective_motet_id (already normalized)
                            recreate_motet_id = parsed.get("motet_id") or effective_motet_id
                            recreate_task_id = parsed.get("task_id")
                            recreate_conversation_id = parsed.get("conversation_id")
                            recreate_session_id = parsed.get("session_id")
                            
                            # Recreate instance with extracted IDs
                            if svc_cfg.visibility == Visibility.USER:
                                if recreate_principal_id and recreate_tenant_id:
                                    await self.create_instance(
                                        service_id=service_id,
                                        tenant_id=recreate_tenant_id,
                                        principal_id=recreate_principal_id,
                                        motet_id=recreate_motet_id,
                                        task_id=recreate_task_id,
                                        conversation_id=recreate_conversation_id,
                                        session_id=recreate_session_id,
                                        command_context=refresh_context,
                                        reason="credential_refresh",
                                        origin="refresh_service_credentials",
                                    )
                                    eager_created = True
                            elif svc_cfg.visibility in (Visibility.TENANT, Visibility.MOTET):
                                if recreate_tenant_id:
                                    await self.create_instance(
                                        service_id=service_id,
                                        tenant_id=recreate_tenant_id,
                                        motet_id=recreate_motet_id,
                                        task_id=recreate_task_id,
                                        conversation_id=recreate_conversation_id,
                                        session_id=recreate_session_id,
                                        command_context=refresh_context,
                                        reason="credential_refresh",
                                        origin="refresh_service_credentials",
                                    )
                                    eager_created = True
                            elif svc_cfg.visibility == Visibility.GLOBAL:
                                await self.create_instance(
                                    service_id=service_id,
                                    motet_id=recreate_motet_id,
                                    task_id=recreate_task_id,
                                    conversation_id=recreate_conversation_id,
                                    session_id=recreate_session_id,
                                    command_context=refresh_context,
                                    reason="credential_refresh",
                                    origin="refresh_service_credentials",
                                )
                                eager_created = True
                    else:
                        logger.warning(
                            "⚠️ Cannot recreate instances - service config not found",
                            service_id=service_id
                        )
                except Exception as e:
                    # Don't fail the refresh if eager creation fails; next tool call will recreate lazily.
                    logger.warning(
                        "⚠️ Eager instance creation failed after credential refresh",
                        service_id=service_id,
                        principal_id=principal_id,
                        tenant_id=tenant_id,
                        error=str(e),
                        error_type=type(e).__name__,
                        exc_info=True,
                    )

                logger.info(
                    f"✅ Credential refresh completed; instances restarted",
                    service_id=service_id,
                    destroyed_count=len(instances_to_restart),
                    eager_created=eager_created,
                    principal_id=principal_id,
                    tenant_id=tenant_id,
                )
                return True
            else:
                logger.warning(f"No vault credentials found for {service_id} after auth update",
                              principal_id=principal_id, tenant_id=tenant_id, motet_id=effective_motet_id)
                return False
                
        except Exception as e:
            logger.error(f"Failed to refresh credentials for {service_id}: {e}", 
                        principal_id=principal_id, tenant_id=tenant_id, motet_id=motet_id, exc_info=True)
            return False

    def get_service_auth_config(self, service_id: str) -> Optional[ServiceAuthConfig]:
        """
        Get auth configuration for a service (ADR-0057).
        
        Args:
            service_id: MCP service identifier
            
        Returns:
            ServiceAuthConfig if service has auth config, None otherwise
        """
        service_config = self.service_configs.get(service_id)
        if service_config:
            return service_config.auth
        return None
