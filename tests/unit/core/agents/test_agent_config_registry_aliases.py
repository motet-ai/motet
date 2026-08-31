"""
Motet - AgentConfigRegistry Alias Semantics Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-31

Description:
    Unit tests for agent bare-alias opt-in behavior (issue #186). Bare agent_id is
    not auto-registered as a global alias; only explicit AgentConfig.aliases claim
    the global bare namespace. Qualified IDs and built-in short names still resolve.

Dependencies:
    - pytest
    - motet.core.agents.registry: AgentConfig, AgentConfigRegistry, get_agent_registry

Usage:
    pytest tests/unit/core/agents/test_agent_config_registry_aliases.py -q
"""

from __future__ import annotations

import pytest

from motet.core.agents.registry import (
    AgentConfig,
    AgentConfigRegistry,
    get_agent_registry,
)


def _minimal_agent(
    *,
    agent_id: str,
    bundle_id: str | None,
    aliases: list[str] | None = None,
) -> AgentConfig:
    return AgentConfig(
        agent_id=agent_id,
        display_name=agent_id,
        description="test",
        system_prompt="test",
        bundle_id=bundle_id,
        aliases=list(aliases or []),
    )


class TestBareAliasOptIn:
    def test_same_agent_id_in_two_bundles_without_aliases(self) -> None:
        """Two bundles may share bare agent_id when neither claims a global alias."""
        registry = AgentConfigRegistry()
        registry.register_agent(_minimal_agent(agent_id="planner", bundle_id="app-builder"))
        registry.register_agent(_minimal_agent(agent_id="planner", bundle_id="plan-mode"))

        assert registry.get("app-builder.planner") is not None
        assert registry.get("plan-mode.planner") is not None
        assert registry.resolve_id("planner") == "planner"  # no alias claim
        assert registry.resolve_id("app-builder.planner") == "app-builder.planner"
        assert registry.resolve_id("plan-mode.planner") == "plan-mode.planner"

    def test_explicit_alias_maps_bare_name(self) -> None:
        registry = AgentConfigRegistry()
        registry.register_agent(
            _minimal_agent(
                agent_id="planner",
                bundle_id="app-builder",
                aliases=["planner"],
            )
        )
        assert registry.resolve_id("planner") == "app-builder.planner"

    def test_explicit_alias_collision_names_owner(self) -> None:
        registry = AgentConfigRegistry()
        registry.register_agent(
            _minimal_agent(
                agent_id="planner",
                bundle_id="app-builder",
                aliases=["planner"],
            )
        )
        with pytest.raises(ValueError, match=r"already claimed by 'app-builder\.planner'") as exc:
            registry.register_agent(
                _minimal_agent(
                    agent_id="planner",
                    bundle_id="plan-mode",
                    aliases=["planner"],
                )
            )
        assert "plan-mode.planner" in str(exc.value)

    def test_agent_id_alone_does_not_claim_global_alias(self) -> None:
        registry = AgentConfigRegistry()
        registry.register_agent(_minimal_agent(agent_id="assistant", bundle_id="sales"))
        assert "assistant" not in registry._aliases
        assert registry.resolve_id("assistant") == "assistant"

    def test_dotted_alias_rejected(self) -> None:
        registry = AgentConfigRegistry()
        with pytest.raises(ValueError, match="bare names without dots"):
            registry.register_agent(
                _minimal_agent(
                    agent_id="assistant",
                    bundle_id="sales",
                    aliases=["sales.assistant"],
                )
            )

    def test_reregister_replaces_aliases(self) -> None:
        registry = AgentConfigRegistry()
        registry.register_agent(
            _minimal_agent(
                agent_id="assistant",
                bundle_id="sales",
                aliases=["helpdesk"],
            )
        )
        assert registry.resolve_id("helpdesk") == "sales.assistant"
        registry.register_agent(
            _minimal_agent(
                agent_id="assistant",
                bundle_id="sales",
                aliases=["support"],
            )
        )
        assert "helpdesk" not in registry._aliases
        assert registry.resolve_id("support") == "sales.assistant"

    def test_unregister_releases_aliases(self) -> None:
        registry = AgentConfigRegistry()
        registry.register_agent(
            _minimal_agent(
                agent_id="assistant",
                bundle_id="sales",
                aliases=["helpdesk"],
            )
        )
        assert registry.unregister("sales.assistant") is True
        assert "helpdesk" not in registry._aliases


class TestBuiltinShortNames:
    def test_builtin_aliases_still_resolve(self) -> None:
        registry = get_agent_registry()
        assert registry.resolve_id(None) == "core.default"
        assert registry.resolve_id("") == "core.default"
        assert registry.resolve_id("default") == "core.default"
        assert registry.resolve_id("agent") == "core.default"
        assert registry.resolve_id("motet_admin") == "core.motet_admin"
        assert registry.get("core.default") is not None
        assert registry.get("core.motet_admin") is not None
        sub = registry.get("core.subagent")
        assert sub is not None
        assert sub.selectable is False
        assert "core.spawn_agents" in (sub.tool_filter.exclude_tools or [])
        assert sub.turn_hooks.conversation_analysis is None
        assert sub.turn_hooks.context_prepare == "core.prepare_context"
        assert f"{sub.max_iterations} tool rounds" in sub.system_prompt
        assert f"{sub.max_tools} tool calls" in sub.system_prompt
        seconds = int(sub.metadata["max_tool_time_ms"]) // 1000
        assert f"{seconds} seconds of tool time" in sub.system_prompt
        assert (
            f"{seconds} seconds of tool time"
            in sub.metadata["discovery_system_prompt"]
        )
