"""
Motet - MCP Adapters

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    This package contains adapter implementations that bridge external protocols
    (like OpenAPI/REST) to the Model Context Protocol (MCP).

Dependencies:
    - fastmcp: Generates MCP tools from OpenAPI specifications
    - httpx: HTTP client used by generated tools/adapters
    - redis: Optional caching for OpenAPI specs to speed startup

Usage:
    # OpenAPI/REST → MCP adapter server (stdio, used by MCPInstanceManager)
    # See: motet.core.tools.mcp_adapters.openapi_adapter_server

Notes:
    - Adapters are typically launched as subprocesses by MCPInstanceManager.
    - Keep stdout clean in adapters (stdio transport) to avoid corrupting JSON-RPC.
    - OpenAPI adapter request safety lives in ``openapi_request_safety.py``
      (HTTPS-by-default, optional host allowlist, timeout and payload caps).
"""
