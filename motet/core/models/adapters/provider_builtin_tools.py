"""
Motet - Provider Built-in Tools

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Defines provider-native "built-in tools" that are executed by the provider itself
    (not by Motet's ToolRegistry) and exposes them as canonical tool schemas.

    This module is intentionally small and policy-driven:
    - Built-ins are disabled by default and must be allowlisted.
    - Canonical names are namespaced (e.g. "openai.web_search") to avoid collisions.
    - Adapters are responsible for mapping canonical built-in tool names to provider wire formats.

Dependencies:
    - motet.core.types: CanonicalToolSchema
    - typing: Optional / List / Set helpers

Usage:
    from motet.core.models.adapters.provider_builtin_tools import (
        get_provider_builtin_tool_names,
        get_unified_web_search_schema,
    )

    # Check which provider builtins are enabled by policy
    enabled_names = get_provider_builtin_tool_names(
        provider="openai",
        allowlist_csv="openai.web_search",
        denylist_csv=None,
    )
    
    # Get the unified web_search schema (used by all providers)
    if any(name.endswith(".web_search") for name in enabled_names):
        schema = get_unified_web_search_schema()

Notes:
    - These are NOT ToolRegistry tools; do not route them through tool_execution.
    - Only enable once the relevant provider adapter supports the built-in mapping.
"""

from __future__ import annotations

from typing import List, Optional, Set

from ...types import CanonicalToolSchema


def _parse_csv_set(value: Optional[str]) -> Set[str]:
    if not value:
        return set()
    return {v.strip() for v in value.split(",") if v.strip()}


def _is_allowed(*, name: str, allowlist: Set[str], denylist: Set[str]) -> bool:
    if name in denylist:
        return False
    if allowlist and name not in allowlist:
        return False
    return True


# Provider-specific builtin tool names (not full schemas - those are unified)
_PROVIDER_BUILTIN_TOOLS: dict[str, List[str]] = {
    "openai": ["openai.web_search"],
    "anthropic": ["anthropic.web_search"],
    "moonshot": ["moonshot.web_search"],
    "xai": ["xai.web_search"],
    "deepseek": ["deepseek.web_search"],
    "meta": ["meta.web_search"],
}


def get_provider_builtin_tool_names(
    *,
    provider: str,
    allowlist_csv: Optional[str],
    denylist_csv: Optional[str],
) -> List[str]:
    """
    Return list of enabled provider-native builtin tool names, filtered by policy allow/deny lists.
    
    These are namespaced names (e.g., "openai.web_search") used for policy checking.
    The actual schema sent to the model is the unified web_search schema.
    """
    allowlist = _parse_csv_set(allowlist_csv)
    denylist = _parse_csv_set(denylist_csv)

    provider = (provider or "").strip().lower()
    tool_names = _PROVIDER_BUILTIN_TOOLS.get(provider, [])
    
    return [name for name in tool_names if _is_allowed(name=name, allowlist=allowlist, denylist=denylist)]


# Module-level constant: Unified web_search schema (ADR-0064)
# Cross-provider canonical schema that adapters map to their wire formats
UNIFIED_WEB_SEARCH_SCHEMA = CanonicalToolSchema(
    name="web_search",
    description="Search the web for current information. Useful for finding recent data, news, facts, and general information.",
    json_schema={
        "type": "object",
        "properties": {
            "query": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": None,
                "description": "Search query to find information about",
                "title": "Query",
            },
            "max_results": {
                "default": 5,
                "description": "Maximum number of search results to return",
                "maximum": 10,
                "minimum": 1,
                "title": "Max Results",
                "type": "integer",
            },
        },
        "required": [],
    },
    strict=False,
)


def get_unified_web_search_schema() -> CanonicalToolSchema:
    """Return the unified web_search canonical schema (cross-provider)."""
    return UNIFIED_WEB_SEARCH_SCHEMA


# Providers that require explicit tool result messages for server-executed builtins
_PROVIDERS_REQUIRING_TOOL_RESULT_MESSAGES: Set[str] = {"moonshot"}


def requires_tool_result_for_provider_builtins(provider: str) -> bool:
    """
    Return True if provider requires explicit tool result messages for server-executed tools.
    
    Some providers (e.g., Moonshot) require every tool_call to have a corresponding tool result
    message in conversation history, even for server-executed builtins like web_search.
    
    Other providers (e.g., Anthropic, OpenAI) embed results directly in the response and
    do NOT expect tool result messages for their server-executed tools.
    
    Args:
        provider: Provider name (e.g., "moonshot", "anthropic", "openai")
    
    Returns:
        True if tool result messages are required, False otherwise
    """
    return (provider or "").strip().lower() in _PROVIDERS_REQUIRING_TOOL_RESULT_MESSAGES


# ---------------------------------------------------------------------------
# Tool name wire-format helpers (ADR-0071)
# ---------------------------------------------------------------------------
# Canonical internal name:  <namespace>.<qualifier>.<tool>  (dots)
# Provider wire format:     <namespace>__<qualifier>__<tool> (double-underscores,
#                           satisfies ^[a-zA-Z0-9_-]+$ required by all LLM providers)
#
# Double-underscore is used so that names containing single underscores remain
# unambiguous in the reverse mapping.  Applies to ALL namespaced tool names:
#   MCP:      mcp.server_id.tool_name  ↔  mcp__server_id__tool_name
#   Core:     core.web_search          ↔  core__web_search
#   Bundle:   bundle_id.tool_name      ↔  bundle_id__tool_name
# ---------------------------------------------------------------------------

def tool_canonical_to_wire(name: str) -> str:
    """Convert a canonical namespaced tool name to the provider wire format.

    Any dotted canonical name has its dots replaced with double-underscores so the
    reverse mapping is unambiguous and the result satisfies ^[a-zA-Z0-9_-]+$:

        mcp.server_id.tool_name  →  mcp__server_id__tool_name
        core.web_search          →  core__web_search
        bundle_id.tool_name      →  bundle_id__tool_name

    Names without dots are returned unchanged (bare or already-wire names).
    """
    if "." in name:
        return name.replace(".", "__")
    return name


def tool_wire_to_canonical(name: str) -> str:
    """Convert a provider wire name back to the canonical dotted format.

    Reverses ``tool_canonical_to_wire`` for any namespaced wire name:

        mcp__server_id__tool_name  →  mcp.server_id.tool_name  (MCP: 3-part, 2 separators)
        core__web_search           →  core.web_search            (core: 2-part, 1 separator)
        bundle_id__tool_name       →  bundle_id.tool_name        (bundle: 2-part, 1 separator)

    Names without ``__`` are returned unchanged (bare names or already-canonical names).
    """
    if "__" not in name:
        return name
    parts = name.split("__", maxsplit=2)
    return ".".join(parts)

