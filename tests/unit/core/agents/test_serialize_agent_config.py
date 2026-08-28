"""
Motet - Agent Config Serialization Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-08

Description:
    Unit tests for serialize_agent_config covering model, loop limits, skills,
    and metadata fields returned to API / ops-dashboard consumers.

Usage:
    pytest tests/unit/core/agents/test_serialize_agent_config.py -q
"""

from __future__ import annotations

from unittest.mock import patch

from motet.core.agents.discovery import serialize_agent_config
from motet.core.agents.registry import AgentConfig, ToolFilter, TurnHooks


def test_serialize_agent_config_includes_model_skills_and_metadata() -> None:
    """Serialized agents expose full config fields used by the ops dashboard."""
    cfg = AgentConfig(
        agent_id="assistant",
        bundle_id="cursor",
        display_name="Cursor Assistant",
        description="IDE backend agent",
        system_prompt="You are helpful.",
        allowed_roles=["*"],
        aliases=["assistant"],
        tool_filter=ToolFilter(mode="discovery"),
        turn_hooks=TurnHooks(context_prepare="core.prepare_context"),
        model_provider="xai",
        model_name="grok-4.5",
        model_profile_name="default",
        temperature=0.3,
        max_iterations=40,
        max_model_calls=90,
        max_tools=25,
        enable_thinking=True,
        reasoning_effort="high",
        conversation_id_prefix="cursor:",
        metadata={"prompt_policy": "client_system_primary"},
        skill_ids=["cursor.demo_skill"],
        skill_mode="discovery",
        skill_max_per_turn=2,
        allowed_surface_ids=["openai_compat"],
    )

    with patch(
        "motet.core.surfaces.resolve_effective_allowlist",
        return_value=["openai_compat"],
    ):
        payload = serialize_agent_config(cfg)

    assert payload["qualified_id"] == "cursor.assistant"
    assert payload["display_name"] == "Cursor Assistant"
    assert payload["model_provider"] == "xai"
    assert payload["model_name"] == "grok-4.5"
    assert payload["model_profile_name"] == "default"
    assert payload["temperature"] == 0.3
    assert payload["max_iterations"] == 40
    assert payload["max_model_calls"] == 90
    assert payload["max_tools"] == 25
    assert payload["enable_thinking"] is True
    assert payload["reasoning_effort"] == "high"
    assert payload["conversation_id_prefix"] == "cursor:"
    assert payload["metadata"] == {"prompt_policy": "client_system_primary"}
    assert payload["skill_ids"] == ["cursor.demo_skill"]
    assert payload["skill_mode"] == "discovery"
    assert payload["skill_max_per_turn"] == 2
    assert payload["allowed_surface_ids"] == ["openai_compat"]
    assert payload["turn_hooks"]["context_prepare"] == "core.prepare_context"


def test_serialize_agent_config_defaults_for_core_agent() -> None:
    """Core agents without overrides still serialize with stable defaults."""
    cfg = AgentConfig(
        agent_id="default",
        system_prompt="Default prompt",
    )

    with patch(
        "motet.core.surfaces.resolve_effective_allowlist",
        return_value=None,
    ):
        payload = serialize_agent_config(cfg)

    assert payload["qualified_id"] == "core.default"
    assert payload["model_provider"] is None
    assert payload["model_name"] is None
    assert payload["temperature"] == 0.2
    assert payload["max_iterations"] == 20
    assert payload["max_model_calls"] is None
    assert payload["enable_thinking"] is False
    assert payload["metadata"] is None
    assert payload["skill_ids"] is None
    assert payload["skill_mode"] == "allowlist"
    assert payload["skill_max_per_turn"] == 3
