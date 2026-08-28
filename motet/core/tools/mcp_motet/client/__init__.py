"""
Motet - MCP Motet Client Package

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    MCP Motet Client package for the Motet distributed framework.
    Contains the synchronous client for distributed MCP operations using
    Motet Streams. Includes comprehensive client management and
    distributed coordination for Celery workers.

Dependencies:
    - MotetMCPClient: Synchronous client for distributed MCP operations

Usage:
    from motet.core.tools.mcp_motet.client import MotetMCPClient

    # Create client
    client = MotetMCPClient(service_id="weather", context_id="user123")

    # List tools
    tools = client.list_tools()

    # Execute tool
    result = client.call_tool("get_weather", {"location": "NYC"})

Notes:
    - Provides comprehensive MCP Motet Client components
    - Includes synchronous client for distributed MCP operations
    - Supports Motet Streams communication
    - Includes distributed coordination and management
    - Supports comprehensive error handling and logging
    - Integrates with MCP protocol and stream operations
    - Includes comprehensive observability and monitoring
"""

from .motet_mcp_client import MotetMCPClient

__all__ = [
    "MotetMCPClient"
]
