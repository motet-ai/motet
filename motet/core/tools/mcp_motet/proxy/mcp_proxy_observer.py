"""
Motet - MCP Proxy Creation Observer

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
    Last Modified: 2026-08-13

Description:
    Event-driven MCP proxy creation observer for the Motet distributed framework.
    Pre-creates MCP server proxies when tool execution events are detected, providing
    proactive proxy management and reducing latency for MCP tool calls. Includes
    intelligent context detection and automatic proxy lifecycle management.

Dependencies:
    - asyncio: Asynchronous proxy creation and event handling
    - structlog: Structured logging and observability
    - typing: Type hints and annotations
    - Event system and observer pattern
    - MCP instance manager

Usage:
    from motet.core.tools.mcp_motet.proxy.mcp_proxy_observer import (
        MCPProxyCreationObserver, register_mcp_proxy_observer
    )

    # Create observer
    observer = MCPProxyCreationObserver(instance_manager)

    # Register with event system
    registered_observer = register_mcp_proxy_observer(instance_manager)

Notes:
    - Provides event-driven proxy creation for MCP tools
    - Includes intelligent context detection (task_id, conversation_id, tenant_id)
    - Supports automatic proxy lifecycle management
    - Includes fallback to stream scanning for reliability
    - Supports vault credential lookup with command context
    - Integrates with distributed event system
    - Provides comprehensive error handling and logging
"""


import asyncio
import os
import structlog
from typing import Any, Dict, Optional
from motet.core.workers import Observer, EventFilter, Event
from motet.core.tools.mcp_motet.protocol import (
    generate_instance_key,
)

logger = structlog.get_logger(__name__)


class MCPProxyCreationObserver(Observer):
    """
    Observer that listens for tool_execution events and pre-creates MCP proxies.
    
    This provides:
    - Fast proxy creation (50-100ms vs 1-2 seconds)
    - Lower Redis overhead (no continuous scanning)
    - More reactive system
    
    The stream scanning backup still runs at reduced frequency (10-30s)
    to catch missed events and provide eventual consistency.
    """
    
    def __init__(self, instance_manager):
        """
        Initialize the observer with a reference to the MCPInstanceManager.
        
        Args:
            instance_manager: MCPInstanceManager instance that will create proxies
        """
        super().__init__(name="mcp_proxy_creation")
        self.instance_manager = instance_manager
        logger.info("🎯 MCPProxyCreationObserver initialized")
    
    def get_event_filter(self) -> EventFilter:
        """
        Filter for MCP-related tool execution events.
        
        We want to intercept tool_execution events for MCP tools to pre-create
        proxies before the request arrives at the Redis stream.
        
        Note: Events are published by process_distributed_command with source="worker"
        """
        return EventFilter(
            # Match both the legacy "tool_execution_started" (pre-ADR-0071) and the
            # canonical "core.tool_execution_started" (post-ADR-0071 namespace).
            # command_tasks.py emits f"{command_type}_started" where command_type is
            # now "core.tool_execution", so the event kind is "core.tool_execution_started".
            event_types={"tool_execution_started", "core.tool_execution_started"},
            sources={"worker"},
            tags=set(),
        )
    
    def on_event(self, event: Event) -> None:
        """
        Handle tool execution event by pre-creating proxy if needed.
        
        Flow:
        1. Receive tool_execution_started event
        2. Check if it's an MCP tool (starts with "mcp:")
        3. Parse service_id and context information
        4. Check if proxy already exists
        5. If not, create proxy asynchronously
        
        Args:
            event: Event containing tool execution details
        """
        try:
            # Extract event data
            data = event.data if hasattr(event, 'data') else {}
            tool_name = data.get('tool_name', '')
            
            # Only handle MCP tools
            if not tool_name.startswith('mcp.'):
                return

            # Parse MCP tool name: mcp.service_id.tool_name
            parts = tool_name.split('.', 2)
            if len(parts) < 2:
                logger.warning(f"⚠️ Invalid MCP tool name format: {tool_name}")
                return
            
            service_id = parts[1]
            
            # Extract context information from event
            task_id = data.get('task_id')
            conversation_id = data.get('conversation_id')
            tenant_id = data.get('tenant_id')
            
            # Extract CommandContext fields for vault lookup
            command_context = data.get('command_context')  # Full context if available
            principal_id = data.get('principal_id')
            # Apply motet_id fallback BEFORE creating SimpleContext
            motet_id = data.get('motet_id') or os.getenv("MOTET_MOTET_ID", "default")
            
            logger.info(
                f"🚀 MCPProxyCreationObserver: Detected MCP tool call",
                tool_name=tool_name,
                service_id=service_id,
                task_id=task_id,
                conversation_id=conversation_id,
                principal_id=principal_id,
                tenant_id=tenant_id,
                motet_id=motet_id,
                has_command_context=command_context is not None
            )
            
            service_config = self.instance_manager.service_configs.get(service_id)
            if not service_config:
                logger.warning("⚠️ No service config found for MCP tool", service_id=service_id)
                return

            # Reconstruct CommandContext if needed (AFTER motet_id fallback applied)
            if command_context is None and principal_id:
                # Create minimal context for vault lookup
                class SimpleContext:
                    def __init__(self, principal_id, tenant_id, motet_id):
                        self.principal_id = principal_id
                        self.tenant_id = tenant_id
                        self.motet_id = motet_id
                
                command_context = SimpleContext(
                    principal_id=principal_id,
                    tenant_id=tenant_id,
                    motet_id=motet_id
                )
                logger.info(
                    "🔐 Created SimpleContext for vault lookup",
                    principal_id=principal_id,
                    tenant_id=tenant_id,
                    motet_id=motet_id
                )
            elif command_context:
                logger.info(
                    "🔐 Using existing command_context for vault lookup",
                    ctx_principal_id=getattr(command_context, 'principal_id', 'N/A'),
                    ctx_tenant_id=getattr(command_context, 'tenant_id', 'N/A'),
                    ctx_motet_id=getattr(command_context, 'motet_id', 'N/A')
                )
            else:
                logger.warning(
                    "⚠️ No command_context and no principal_id - vault lookup may use global scope",
                    principal_id=principal_id,
                    tenant_id=tenant_id,
                    motet_id=motet_id
                )

            try:
                instance_id = generate_instance_key(
                    service_id=service_id,
                    visibility=service_config.visibility,
                    lifecycle_duration=service_config.lifecycle_duration,
                    motet_id=motet_id,
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    conversation_id=conversation_id,
                    task_id=task_id,
                    session_id=None,
                )
            except Exception as e:
                logger.warning("⚠️ Unable to build instance key for event", service_id=service_id, error=str(e))
                return

            create_kwargs = {
                'service_id': service_id,
                'context_id': instance_id
            }
            # Tag instance creation so mcp.instance_created can distinguish call paths.
            create_kwargs["reason"] = "tool_execution"
            create_kwargs["origin"] = "mcp_proxy_observer"
            if principal_id:
                create_kwargs['principal_id'] = principal_id
            if tenant_id:
                create_kwargs['tenant_id'] = tenant_id
            if motet_id:
                create_kwargs['motet_id'] = motet_id
            if conversation_id:
                create_kwargs['conversation_id'] = conversation_id
            if task_id:
                create_kwargs['task_id'] = task_id
            
            # Add command_context for vault credential lookup
            if command_context:
                create_kwargs['command_context'] = command_context
            
            if instance_id in self.instance_manager.instances:
                logger.debug(
                    f"✓ Proxy already exists for {instance_id}, skipping creation"
                )
                return
            
            # Schedule async proxy creation
            # Note: This is called from sync context (event handler)
            # so we schedule it on the instance manager's event loop
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
                        "mcp_proxy_observer_no_event_loop",
                        instance_id=instance_id,
                        note="Cannot schedule create_instance; MotetMCPClient wait will time out",
                    )
                    return
            
            # Schedule proxy creation as a coroutine
            asyncio.run_coroutine_threadsafe(
                self._create_proxy_async(create_kwargs),
                loop
            )
            
            logger.info(
                f"✅ Scheduled proxy creation for {instance_id}",
                service_id=service_id,
                task_id=task_id,
                conversation_id=conversation_id
            )
            
        except Exception as e:
            logger.error(
                f"❌ MCPProxyCreationObserver error: {e}",
                exc_info=True
            )
            # Don't raise — the tool call path still waits on the request stream.
    
    async def _create_proxy_async(self, create_kwargs: Dict[str, Any]) -> None:
        """
        Asynchronously create the proxy.
        
        This is scheduled on the instance manager's event loop and creates
        the proxy without blocking the event handler.
        
        Args:
            create_kwargs: Arguments for create_instance()
        """
        try:
            service_id = create_kwargs['service_id']
            instance_id = create_kwargs.get('context_id') or create_kwargs.get('instance_id', 'unknown')
            task_id = create_kwargs.get('task_id')
            conversation_id = create_kwargs.get('conversation_id')
            
            logger.info(
                "mcp_proxy_observer_creating_proxy",
                service_id=service_id,
                instance_id=instance_id,
                task_id=task_id,
                conversation_id=conversation_id,
                context_kwargs=create_kwargs
            )
            
            instance = await asyncio.wait_for(
                self.instance_manager.create_instance(**create_kwargs),
                timeout=self.instance_manager._create_timeout_seconds(),
            )
            
            logger.info(
                "mcp_proxy_observer_proxy_created",
                service_id=service_id,
                instance_id=instance.instance_id,
                task_id=task_id,
                conversation_id=conversation_id,
                has_transport=instance.transport is not None,
                has_process=instance.process is not None,
                process_pid=instance.process.pid if instance.process else None,
                latency_comment="50-100ms (vs 1-2s for stream scanning)"
            )
            
        except Exception as e:
            logger.error(
                "mcp_proxy_observer_proxy_creation_failed",
                service_id=create_kwargs.get('service_id'),
                task_id=create_kwargs.get('task_id'),
                conversation_id=create_kwargs.get('conversation_id'),
                error=str(e),
                error_type=type(e).__name__,
                fallback="Create failed; MotetMCPClient is still waiting on the request stream",
                exc_info=True
            )


def register_mcp_proxy_observer(instance_manager) -> MCPProxyCreationObserver:
    """
    Register the MCP proxy creation observer with the event system.
    
    This should be called during MCPInstanceManager startup to enable
    event-driven proxy creation.
    
    Args:
        instance_manager: MCPInstanceManager instance
        
    Returns:
        The registered observer (for cleanup if needed)
    """
    from motet.core.workers import register_event_observer
    
    observer = MCPProxyCreationObserver(instance_manager)
    register_event_observer(observer)
    
    logger.info("✅ MCPProxyCreationObserver registered with event system")
    return observer

