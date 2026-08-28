"""
Motet - Agent Configuration (core.agents)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Agent configuration registry and related types. Provides
    AgentConfig, ToolFilter, TurnHooks, and AgentConfigRegistry for resolving
    named agent configurations (e.g. core.default, core.motet_admin) into
    execution parameters. Also hosts prompt-assembly policy helpers
    (``prompt_policy``) driven by AgentConfig metadata. Agent-related modules
    live under core/agents so registry, resolution helpers, and built-in
    configs stay co-located.

Dependencies:
    - pydantic: AgentConfig, ToolFilter, TurnHooks models
    - motet.core.tools: Tool registry and schema export for tool resolution

Usage:
    from motet.core.agents import AgentConfigRegistry, get_agent_registry
    registry = get_agent_registry()
    config = registry.get("core.motet_admin")

Notes:
    - Implementation adds registry.py (models + registry) and built-in
      config registration per Phase 1.
    - All lookups use fully-qualified agent IDs (e.g. core.default).
"""

from .registry import (
    AgentConfig,
    AgentConfigRegistry,
    ToolFilter,
    TurnHooks,
    ensure_conversation_id_prefix,
    get_agent_registry,
    get_discovery_filter_metadata,
    resolve_agent_id,
    resolve_tools,
)
from .discovery import (
    serialize_agent_config,
    sync_bundle_agents_into_registry,
    list_visible_agents,
    principal_may_access_agent,
)
from .prompt_policy import (
    PROMPT_POLICY_CLIENT_SYSTEM_PRIMARY,
    PROMPT_POLICY_DEFAULT,
    PROMPT_POLICY_MOTET_SYSTEM_PRIMARY,
    assemble_turn_history,
    ensure_protected_system_prefix,
    extract_protected_prefix,
    is_client_system_primary,
    is_prompt_policy_protected,
    prompt_policy_from_agent,
)

__all__ = [
    "AgentConfig",
    "AgentConfigRegistry",
    "ToolFilter",
    "TurnHooks",
    "ensure_conversation_id_prefix",
    "get_agent_registry",
    "get_discovery_filter_metadata",
    "resolve_agent_id",
    "resolve_tools",
    "serialize_agent_config",
    "sync_bundle_agents_into_registry",
    "list_visible_agents",
    "principal_may_access_agent",
    "PROMPT_POLICY_DEFAULT",
    "PROMPT_POLICY_MOTET_SYSTEM_PRIMARY",
    "PROMPT_POLICY_CLIENT_SYSTEM_PRIMARY",
    "assemble_turn_history",
    "ensure_protected_system_prefix",
    "extract_protected_prefix",
    "is_client_system_primary",
    "is_prompt_policy_protected",
    "prompt_policy_from_agent",
]
