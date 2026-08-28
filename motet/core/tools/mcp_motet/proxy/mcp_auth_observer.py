"""
Motet - MCP Auth Observer

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Event-driven MCP authentication observer for the Motet distributed framework.
    Listens for mcp.auth_updated events and refreshes credentials for affected MCP
    services, enabling seamless OAuth flow integration.

Dependencies:
    - asyncio: Asynchronous credential refresh
    - structlog: Structured logging and observability
    - typing: Type hints and annotations
    - Event system and observer pattern
    - MCP instance manager

Usage:
    from motet.core.tools.mcp_motet.proxy.mcp_auth_observer import (
        MCPAuthObserver, register_mcp_auth_observer
    )

    # Create observer
    observer = MCPAuthObserver(instance_manager)

    # Register with event system
    registered_observer = register_mcp_auth_observer(instance_manager)

Notes:
    - Listens for mcp.auth_updated events from OAuth callback
    - Refreshes vault credentials for affected services
    - Restarts MCP instances to pick up new credentials
    - Worker-agnostic: each worker's instance manager self-filters by service_id
    - Part of MCP OAuth Prompt Flow for Missing Authorization
"""

import asyncio
import structlog
from typing import Any, Dict, Optional
from motet.core.workers import Observer, EventFilter, Event

logger = structlog.get_logger(__name__)


class MCPAuthObserver(Observer):
    """
    Observer that listens for mcp.auth_updated events and refreshes credentials.
    
    When a user completes OAuth authorization, this observer:
    1. Receives the mcp.auth_updated event
    2. Checks if this instance manager manages the affected service
    3. Refreshes credentials from vault
    4. Restarts affected MCP instances with new credentials
    
    This enables seamless OAuth flow integration where:
    - Tool call fails with auth_required
    - User completes OAuth in popup
    - Event is emitted
    - This observer refreshes credentials
    - Subsequent tool calls succeed
    """
    
    def __init__(self, instance_manager):
        """
        Initialize the observer with a reference to the MCPInstanceManager.
        
        Args:
            instance_manager: MCPInstanceManager instance that will refresh credentials
        """
        super().__init__(name="mcp_auth")
        self.instance_manager = instance_manager
        logger.info("🔐 MCPAuthObserver initialized")
    
    def get_event_filter(self) -> EventFilter:
        """
        Filter for mcp.auth_updated and mcp.auth_revoked events.
        
        These events are emitted by:
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
        Handle mcp.auth_updated and mcp.auth_revoked events.
        
        For mcp.auth_updated (authorization complete):
        1. Clear vault cache for the credential
        2. Refresh credentials from vault
        3. Restart affected MCP instances with new credentials
        
        For mcp.auth_revoked (credential disconnected):
        1. Clear vault cache for the credential
        2. Stop affected MCP instances (they'll fail without credentials)
        
        Args:
            event: Event containing auth update/revoke details
        """
        try:
            # Extract event data
            # Event class uses event_type, but dict events use "kind" field
            event_kind = getattr(event, 'event_type', None) or getattr(event, 'kind', None) or "unknown"
            data = event.data if hasattr(event, 'data') else {}
            service_id = data.get('service_id')
            principal_id = data.get('principal_id')
            tenant_id = data.get('tenant_id')
            motet_id = data.get('motet_id')
            status = data.get('status')
            
            if not service_id:
                logger.warning(f"⚠️ {event_kind} event missing service_id")
                return
            
            logger.info(
                f"🔐 MCPAuthObserver: Received {event_kind}",
                service_id=service_id,
                principal_id=principal_id,
                tenant_id=tenant_id,
                motet_id=motet_id,
                status=status
            )
            
            # Check if we manage this service
            if service_id not in self.instance_manager.service_configs:
                logger.debug(
                    f"ℹ️ Service {service_id} not managed by this instance manager, ignoring"
                )
                return
            
            # Handle based on event type
            # Note: VaultCacheObserver handles cache clearing automatically via events
            if event_kind == "mcp.auth_revoked":
                logger.info(
                    f"🔌 Auth revoked for {service_id}, stopping affected instances",
                    service_id=service_id,
                    principal_id=principal_id,
                    tenant_id=tenant_id,
                    motet_id=motet_id
                )
                # For revoked credentials, stop/destroy affected MCP instances
                # This ensures they don't continue using stale cached credentials
                self._stop_affected_instances(service_id, principal_id, tenant_id, motet_id)
                return
            
            # For auth_updated, only process successful authorizations
            if status != "authorized":
                logger.info(
                    f"ℹ️ Auth status is '{status}', not refreshing credentials"
                )
                return
            
            logger.info(
                f"✅ Service {service_id} is managed by this instance manager, refreshing credentials"
            )
            
            # Schedule async credential refresh
            import asyncio
            
            # Get the instance manager's event loop
            if hasattr(self.instance_manager, '_loop') and self.instance_manager._loop:
                loop = self.instance_manager._loop
            else:
                # Fallback to default event loop
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    logger.warning(
                        f"⚠️ No event loop available for async credential refresh"
                    )
                    return
            
            # Schedule credential refresh as a coroutine
            asyncio.run_coroutine_threadsafe(
                self._refresh_credentials_async(service_id, principal_id, tenant_id, motet_id),
                loop
            )
            
            logger.info(
                f"✅ Scheduled credential refresh for {service_id}",
                service_id=service_id,
                principal_id=principal_id,
                tenant_id=tenant_id,
                motet_id=motet_id
            )
            
        except Exception as e:
            logger.error(
                f"❌ MCPAuthObserver error: {e}",
                exc_info=True
            )
    
    async def _refresh_credentials_async(
        self,
        service_id: str,
        principal_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        motet_id: Optional[str] = None
    ) -> None:
        """
        Asynchronously refresh credentials for a service.
        
        This is scheduled on the instance manager's event loop and refreshes
        credentials without blocking the event handler.
        
        Args:
            service_id: MCP service identifier
            principal_id: User who completed authorization
            tenant_id: Tenant context for multi-tenant credential management
            motet_id: Motet ID for per-motet credential isolation (ADR-0058)
        """
        try:
            logger.info(
                f"🔄 Refreshing credentials for service",
                service_id=service_id,
                principal_id=principal_id,
                tenant_id=tenant_id,
                motet_id=motet_id
            )
            
            # Refresh service credentials in the instance manager
            # Note: VaultCacheObserver handles cache clearing automatically via events
            # Pass principal_id, tenant_id, and motet_id for per-user credential lookup
            success = await self.instance_manager.refresh_service_credentials(
                service_id, 
                principal_id=principal_id, 
                tenant_id=tenant_id,
                motet_id=motet_id
            )
            
            if success:
                logger.info(
                    f"✅ Credential refresh complete",
                    service_id=service_id,
                    principal_id=principal_id,
                    tenant_id=tenant_id,
                    motet_id=motet_id
                )
            else:
                logger.warning(
                    f"⚠️ Credential refresh returned false",
                    service_id=service_id,
                    principal_id=principal_id,
                    tenant_id=tenant_id,
                    motet_id=motet_id
                )
                
        except Exception as e:
            logger.error(
                f"❌ Credential refresh failed: {e}",
                service_id=service_id,
                principal_id=principal_id,
                tenant_id=tenant_id,
                motet_id=motet_id,
                exc_info=True
            )
    
    def _stop_affected_instances(
        self,
        service_id: str,
        principal_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        motet_id: Optional[str] = None
    ) -> None:
        """
        Stop/destroy MCP instances affected by credential revocation.
        
        For per-user instances, stops the user's instance.
        For per-tenant instances, stops the tenant's instance.
        For per-motet instances, stops the motet's instance.
        For shared instances, stops all instances for the service.
        
        Args:
            service_id: MCP service identifier
            principal_id: User whose credentials were revoked
            tenant_id: Tenant context
            motet_id: Motet ID for per-motet credential isolation (ADR-0058)
        """
        try:
            service_config = self.instance_manager.service_configs.get(service_id)
            if not service_config:
                return
            
            # Determine which instances to stop based on visibility (ADR-0058)
            from motet.core.tools.mcp_motet.protocol import Visibility, generate_instance_key, parse_instance_key
            
            instances_to_stop = []
            
            # Match instances based on visibility and context IDs
            visibility = service_config.visibility
            
            if visibility == Visibility.USER and principal_id:
                # Stop per-user instances for this principal
                # Match instances where instance_id contains the principal_id, tenant_id, and motet_id
                for inst_id, inst in self.instance_manager.instances.items():
                    if inst.service_id == service_id:
                        try:
                            # Use inst_id (the dictionary key) as the instance_key for parsing
                            parsed = parse_instance_key(service_id, visibility, inst_id)
                            # Match principal_id, tenant_id, and motet_id for USER visibility
                            if parsed.get("principal_id") == principal_id:
                                if tenant_id is None or parsed.get("tenant_id") == tenant_id:
                                    if motet_id is None or parsed.get("motet_id") == motet_id:
                                        instances_to_stop.append(inst_id)
                        except (ValueError, KeyError):
                            # Fallback: check if principal_id, tenant_id, and motet_id are in instance_id string
                            if principal_id in inst_id:
                                if tenant_id is None or tenant_id in inst_id:
                                    if motet_id is None or motet_id in inst_id:
                                        instances_to_stop.append(inst_id)
            elif visibility == Visibility.TENANT and tenant_id:
                # Stop per-tenant instances for this tenant
                for inst_id, inst in self.instance_manager.instances.items():
                    if inst.service_id == service_id:
                        try:
                            # Use inst_id (the dictionary key) as the instance_key for parsing
                            parsed = parse_instance_key(service_id, visibility, inst_id)
                            if parsed.get("tenant_id") == tenant_id:
                                instances_to_stop.append(inst_id)
                        except (ValueError, KeyError):
                            # Fallback: check if tenant_id is in instance_id string
                            if tenant_id in inst_id:
                                instances_to_stop.append(inst_id)
            elif visibility == Visibility.MOTET and tenant_id:
                # Stop per-motet instances for this tenant and motet (motets belong to tenants)
                for inst_id, inst in self.instance_manager.instances.items():
                    if inst.service_id == service_id:
                        try:
                            # Use inst_id (the dictionary key) as the instance_key for parsing
                            parsed = parse_instance_key(service_id, visibility, inst_id)
                            # Match both tenant_id and motet_id for MOTET visibility
                            if parsed.get("tenant_id") == tenant_id:
                                if motet_id is None or parsed.get("motet_id") == motet_id:
                                    instances_to_stop.append(inst_id)
                        except (ValueError, KeyError):
                            # Fallback: check if tenant_id and motet_id are in instance_id string
                            if tenant_id in inst_id:
                                if motet_id is None or motet_id in inst_id:
                                    instances_to_stop.append(inst_id)
            else:
                # GLOBAL visibility or no specific context - stop all for this service
                for inst_id, inst in self.instance_manager.instances.items():
                    if inst.service_id == service_id:
                        instances_to_stop.append(inst_id)
            
            # Stop each affected instance
            if instances_to_stop:
                logger.info(
                    f"🛑 Stopping {len(instances_to_stop)} MCP instances after credential revocation",
                    service_id=service_id,
                    principal_id=principal_id,
                    tenant_id=tenant_id,
                    motet_id=motet_id,
                    instance_count=len(instances_to_stop)
                )
                
                # Get the instance manager's event loop
                if hasattr(self.instance_manager, '_loop') and self.instance_manager._loop:
                    loop = self.instance_manager._loop
                else:
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        logger.warning(
                            f"⚠️ No event loop available for stopping instances"
                        )
                        return
                
                # Schedule instance destruction asynchronously
                for instance_id in instances_to_stop:
                    asyncio.run_coroutine_threadsafe(
                        self.instance_manager.destroy_instance(instance_id),
                        loop
                    )
                
                logger.info(
                    f"✅ Scheduled destruction of {len(instances_to_stop)} instances",
                    service_id=service_id,
                    principal_id=principal_id,
                    tenant_id=tenant_id,
                    motet_id=motet_id
                )
            else:
                logger.debug(
                    f"ℹ️ No instances to stop for revoked credentials",
                    service_id=service_id,
                    principal_id=principal_id,
                    tenant_id=tenant_id,
                    motet_id=motet_id
                )
                
        except Exception as e:
            logger.warning(
                f"⚠️ Failed to stop affected instances: {e}",
                service_id=service_id,
                principal_id=principal_id,
                tenant_id=tenant_id,
                motet_id=motet_id,
                exc_info=True
            )


def register_mcp_auth_observer(instance_manager) -> MCPAuthObserver:
    """
    Register the MCP auth observer with the event system.
    
    This should be called during MCPInstanceManager startup to enable
    event-driven credential refresh after OAuth completion.
    
    Args:
        instance_manager: MCPInstanceManager instance
        
    Returns:
        The registered observer (for cleanup if needed)
    """
    from motet.core.workers import register_event_observer
    
    observer = MCPAuthObserver(instance_manager)
    register_event_observer(observer)
    
    logger.info("✅ MCPAuthObserver registered with event system")
    return observer

