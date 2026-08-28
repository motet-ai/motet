"""
Motet - Tools Module

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Tool ecosystem for the Motet distributed framework.
    Provides tool registry, embedding-first discovery,
    and distributed tool execution.

    Built-in tools are registered onto the runtime singleton ``registry`` by
    delegating to ``builtin.register_all_builtin_tools`` — the single source of
    truth for the built-in tool list. Add built-ins in
    ``builtin._BUILTIN_TOOL_SPECS`` only.

Dependencies:
    - Tool registry and discovery system
    - FunctionDiscoveryVectorStore
    - Distributed tool execution
    - MCP client integration

Usage:
    from motet.core.tools import registry, ToolRegistry
    
    # Register tools
    registry.register("my_tool", tool_function)
    
    # Discover tools via embedding search (ADR-0051 / ADR-0074)
    from motet.core.tools import ToolDiscoveryService, ToolDiscoveryContext
    discovery = ToolDiscoveryService()
    candidates = discovery.discover_tools(
        content="search the web",
        context_type=ToolDiscoveryContext.DIRECT_QUERY,
    )

Notes:
    - Supports dynamic tool discovery via FunctionDiscoveryVectorStore
    - Agentic loop uses embedding-first routing + model_stream
    - Integrates with distributed execution
    - Includes MCP client support
    - To add a built-in tool, edit ``builtin._BUILTIN_TOOL_SPECS`` (one place).
"""

from __future__ import annotations

# Facade: expose registry API and register built-in tools (single source of truth)

from .registry import registry, ToolRegistry, set_runtime_stack, get_runtime_stack  # re-export
from .distributed_discovery import ToolDiscoveryService, ToolCandidate, ToolDiscoveryContext  # re-export
from .context_manager import ContextManager, ContextRequirement, ContextPriority, ContextStrategy  # re-export
from .tool_calls_parser import extract_tool_calls_from_response, parse_tool_calls  # re-export
from .function_discovery_vector_store import FunctionDiscoveryVectorStore  # re-export

# Register all built-in tools via the single canonical registrar. This is
# per-tool resilient (a broken optional tool is logged and skipped, not silently
# dropped) and keeps the built-in list defined in exactly one place.
from .builtin import register_all_builtin_tools as _register_all_builtin_tools

_register_all_builtin_tools(registry)

__all__ = [
    "registry", "ToolRegistry", "set_runtime_stack", "get_runtime_stack", 
    "ToolDiscoveryService", "ToolCandidate", "ToolDiscoveryContext", 
    "ContextManager", "ContextRequirement", "ContextPriority", "ContextStrategy",
    "extract_tool_calls_from_response", "parse_tool_calls",
    "FunctionDiscoveryVectorStore"  # Semantic search for tool/workflow discovery
]
