"""
Motet - MCP Motet Proxy Package

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    MCP Motet Proxy package for the Motet distributed framework.
    Contains proxy components that bridge Motet Streams and MCP server
    stdio communication, plus the instance manager that orchestrates
    proxy lifecycles. Includes comprehensive proxy management and
    distributed coordination.

    Four-dimensional isolation model (state_model, credential_scope,
    visibility, lifecycle_duration) with instance-key/stream-name alignment.
    MCPAuthObserver remains for OAuth credential refresh.

Dependencies:
    - Motet MCP Stream Bridge for stream communication
    - Motet MCP Proxy for proxy management
    - MCP Instance Manager for lifecycle orchestration
    - MCP Auth Observer for OAuth credential refresh

Usage:
    from motet.core.tools.mcp_motet.proxy import (
        MotetMCPStreamBridge, MotetMCPProxy, MCPInstanceManager,
        MCPInstanceConfig, MCPAuthObserver, register_mcp_auth_observer
    )
    from motet.core.tools.mcp_motet.protocol import (
        Visibility, LifecycleDuration, StateModel, CredentialScope
    )

    # Create instance manager with ADR-0058 configuration
    config = MCPInstanceConfig(
        service_id="weather",
        state_model=StateModel.STATELESS,
        credential_scope=CredentialScope.MOTET,
        visibility=Visibility.MOTET,
        lifecycle_duration=LifecycleDuration.PERMANENT,
        instances=1
    )
    manager = MCPInstanceManager()

    # Create proxy (instance_key generated automatically)
    proxy = MotetMCPProxy(
        config=config,
        tenant_id="acme-corp",
        motet_id="production"
    )

Notes:
    - Provides comprehensive MCP Motet Proxy components
    - Includes stream bridge and proxy management
    - Supports instance lifecycle orchestration
    - Includes distributed coordination and management
    - Supports comprehensive error handling and logging
    - Integrates with MCP protocol and stream operations
    - Includes comprehensive observability and monitoring
    - OAuth credential refresh via MCPAuthObserver
"""

from .motet_mcp_stream_bridge import MotetMCPStreamBridge
from .motet_mcp_proxy import MotetMCPProxy
from .mcp_instance_manager import (
    MCPInstanceManager,
    MCPInstanceConfig,
    get_instance_manager,
    set_instance_manager,
    get_service_config,
    get_oauth_providers_from_config,
)
from .mcp_auth_observer import MCPAuthObserver, register_mcp_auth_observer

__all__ = [
    "MotetMCPStreamBridge",
    "MotetMCPProxy",
    "MCPInstanceManager",
    "MCPInstanceConfig",
    "get_instance_manager",
    "set_instance_manager",
    "get_service_config",
    "get_oauth_providers_from_config",
    "MCPAuthObserver",
    "register_mcp_auth_observer"
]
