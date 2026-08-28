"""
Motet - MCP Motet Package

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    MCP Motet package for the Motet distributed framework.
    Implements the Motet Streams-based MCP (Model Context Protocol)
    communication system with comprehensive proxy management,
    stream bridging, and distributed coordination.

Dependencies:
    - MotetMCPProxy: Bridges Motet Streams ↔ MCP server stdio
    - MotetMCPStreamBridge: Handles Redis Streams operations
    - MCPInstanceManager: Manages MCP server proxy instances
    - MotetMCPClient: Synchronous client for Celery workers

Usage:
    from motet.core.tools.mcp_motet import (
        MotetMCPProxy, MotetMCPStreamBridge, MCPInstanceManager
    )

    # Create proxy
    proxy = MotetMCPProxy(service_id="weather", context_id="user123")

    # Create stream bridge
    bridge = MotetMCPStreamBridge(service_id="weather", context_id="user123")

    # Create instance manager
    manager = MCPInstanceManager()

Notes:
    - Provides comprehensive MCP Motet communication system
    - Includes proxy management and stream bridging
    - Supports instance lifecycle management
    - Includes distributed coordination and management
    - Supports comprehensive error handling and logging
    - Integrates with MCP protocol and stream operations
    - Includes comprehensive observability and monitoring
"""

__version__ = "1.0.0"
__all__ = [
    "MotetMCPProxy",
    "MotetMCPStreamBridge", 
    "MCPInstanceManager",
    "MotetMCPClient",
    "MCPStreamMessage",
    "MCPRequestMessage",
    "MCPResponseMessage",
    "MCPLogMessage"
]
