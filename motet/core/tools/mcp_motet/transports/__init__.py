"""
Motet - MCP Transport Layer Package

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    MCP Transport Layer package for the Motet distributed framework.
    Provides transport abstraction for MCP servers, enabling support for multiple
    communication protocols (stdio, HTTP, WebSocket, gRPC, etc.) while maintaining
    a consistent interface for clients. Includes comprehensive transport management
    and distributed coordination.

Dependencies:
    - MCPTransport: Abstract base class defining the transport interface
    - MCPTransportFactory: Factory for creating transport instances
    - StdioMCPTransport: Subprocess-based transport using stdin/stdout
    - HTTPMCPTransport: HTTP-based transport with bearer token authentication

Usage:
    from motet.core.tools.mcp_motet.transports import (
        MCPTransportFactory, MCPToolDefinition
    )
    
    # Create a stdio transport
    transport = MCPTransportFactory.create_transport(
        transport_type="stdio",
        service_id="weather",
        config={
            "command": "npx",
            "args": ["-y", "@timlukahorstmann/mcp-weather"],
            "env": {"ACCUWEATHER_API_KEY": "..."}
        }
    )

Notes:
    - Provides comprehensive MCP Transport Layer components
    - Includes transport abstraction and factory management
    - Supports multiple communication protocols
    - Includes comprehensive transport management
    - Supports distributed coordination and management
    - Integrates with MCP protocol and transport system
    - Includes comprehensive observability and monitoring
"""

from motet.core.tools.mcp_motet.transports.base import (
    MCPTransport,
    MCPToolDefinition,
    MCPResourceDefinition,
    MCPResourceContent,
    MCPPromptDefinition,
    MCPPromptMessage,
    MCPPromptResult,
)
from motet.core.tools.mcp_motet.transports.factory import (
    MCPTransportFactory
)

# Transports are auto-registered by factory on import
# Additional transports can be imported explicitly if needed:
# from motet.core.tools.mcp_motet.transports.stdio import StdioMCPTransport
# from motet.core.tools.mcp_motet.transports.http import HTTPMCPTransport

__all__ = [
    "MCPTransport",
    "MCPToolDefinition",
    "MCPResourceDefinition",
    "MCPResourceContent",
    "MCPPromptDefinition",
    "MCPPromptMessage",
    "MCPPromptResult",
    "MCPTransportFactory",
]

