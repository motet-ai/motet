"""
Motet - Agent Bundle Scope Filter Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-17

Description:
    Unit tests for manage-app Agents page bundle targeting filters.

Usage:
    pytest tests/unit/interfaces/api/test_agent_scope.py -q
"""

from __future__ import annotations

from motet.interfaces.api.v1.agents import AgentListItem, _agent_matches_bundle_scope


def _agent(bundle_id: str | None) -> AgentListItem:
    return AgentListItem(
        qualified_id="core.default" if not bundle_id else f"{bundle_id}.demo",
        agent_id="demo",
        bundle_id=bundle_id,
        display_name="Demo",
    )


def test_core_agents_always_match() -> None:
    assert _agent_matches_bundle_scope(_agent(None), {}, "acme", "default")


def test_untargeted_bundle_matches() -> None:
    catalogs = {"sales": {"targeting": {}}}
    assert _agent_matches_bundle_scope(_agent("sales"), catalogs, "acme", None)


def test_targeted_bundle_excludes_other_tenant() -> None:
    catalogs = {"sales": {"targeting": {"tenant_ids": ["acme"]}}}
    assert _agent_matches_bundle_scope(_agent("sales"), catalogs, "acme", None)
    assert not _agent_matches_bundle_scope(_agent("sales"), catalogs, "other", None)
