"""
Motet - Scoped Registry Base Unit Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-09

Description:
    Unit tests for ADR-0079 scoped registry foundation primitives. Validates
    ScopeGrant matching/serialization and ScopedRegistry visibility filtering
    and namespace lifecycle behavior, including CommandTypeRegistry migration (#61).

Dependencies:
    - pytest: Test framework and assertions
    - motet.core.registry: Scope and registry primitives under test

Usage:
    pytest tests/unit/core/test_registry_base.py

Notes:
    - These tests are intentionally small and deterministic to validate base contracts.
"""

from typing import Any, Dict

from motet.core.registry import RegistryScope, ScopeFilter, ScopeGrant, ScopedRegistry
from motet.core.tools.registry import ToolRegistry
from motet.core.commands.command_type_registry import (
    CommandImplementationType,
    CommandRegistration,
    command_type_registry,
)


def test_scope_grant_matches_exact_and_wildcards() -> None:
    wildcard = ScopeGrant()
    assert wildcard.matches("tenant-a", "motet-a", "admin", "user-a")

    grant = ScopeGrant(tenant_id="tenant-a", motet_id="motet-a", role="admin", principal_id="user-a")
    assert grant.matches("tenant-a", "motet-a", "admin", "user-a")
    assert not grant.matches("tenant-b", "motet-a", "admin", "user-a")


def test_scope_key_roundtrip_and_fail_closed() -> None:
    grant = ScopeGrant(tenant_id="tenant-a", motet_id="prod", role="operator", principal_id="user-1")
    serialized = grant.to_scope_key()
    parsed = ScopeGrant.from_scope_key(serialized)
    assert parsed == grant

    try:
        ScopeGrant.from_scope_key("tenant-a:prod:operator")
    except ValueError:
        pass
    else:
        raise AssertionError("Expected malformed scope key to raise ValueError")


def test_scoped_registry_visibility_and_namespace_unregistration() -> None:
    registry: ScopedRegistry[dict] = ScopedRegistry(registry_name="test_registry")

    registry.register(
        "core.global",
        {"name": "global"},
        scope=RegistryScope(namespace="core", grants=[ScopeGrant()]),
    )
    registry.register(
        "bundle_a.admin_only",
        {"name": "admin"},
        scope=RegistryScope(
            namespace="bundle_a",
            bundle_id="bundle_a",
            grants=[ScopeGrant(tenant_id="tenant-a", role="admin")],
        ),
    )

    admin_visible = registry.list_visible(
        ScopeFilter(tenant_id="tenant-a", motet_id="prod", role="admin", principal_id="user-1")
    )
    operator_visible = registry.list_visible(
        ScopeFilter(tenant_id="tenant-a", motet_id="prod", role="operator", principal_id="user-1")
    )

    assert set(admin_visible.keys()) == {"core.global", "bundle_a.admin_only"}
    assert set(operator_visible.keys()) == {"core.global"}

    removed = registry.unregister_namespace("bundle_a")
    assert removed == ["bundle_a.admin_only"]
    assert registry.get("bundle_a.admin_only") is None


def test_tool_registry_common_api_surface() -> None:
    registry = ToolRegistry()

    def _tool(params: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True, "params": params}

    registry.register(
        name="bundle_x.echo",
        description="Echo tool",
        func=_tool,
        scope=RegistryScope(namespace="bundle_x", grants=[ScopeGrant(tenant_id="tenant-a")]),
    )

    assert "bundle_x.echo" in registry.list_items()
    entry = registry.get_entry("bundle_x.echo")
    assert entry is not None
    assert entry.key == "bundle_x.echo"
    assert entry.item.name == "bundle_x.echo"
    assert entry.scope.namespace == "bundle_x"
    assert any(e.key == "bundle_x.echo" for e in registry.list_entries())

    visible = registry.list_visible(
        ScopeFilter(tenant_id="tenant-a", motet_id="*", role="*", principal_id="*")
    )
    hidden = registry.list_visible(
        ScopeFilter(tenant_id="tenant-b", motet_id="*", role="*", principal_id="*")
    )
    assert "bundle_x.echo" in visible
    assert "bundle_x.echo" not in hidden
    assert any(
        e.key == "bundle_x.echo"
        for e in registry.list_visible_entries(
            ScopeFilter(tenant_id="tenant-a", motet_id="*", role="*", principal_id="*")
        )
    )

    removed = registry.unregister_namespace("bundle_x")
    assert removed == ["bundle_x.echo"]
    assert registry.get("bundle_x.echo") is None


def test_command_type_registry_common_api_surface() -> None:
    """CommandTypeRegistry participates in the ScopedRegistry common API (ADR-0079 / #61)."""
    reg = command_type_registry
    key = "bundle_x.test_command"
    # Ensure a clean key even if a prior test left residue.
    reg.unregister(key)

    try:
        registration = CommandRegistration(
            command_type=key,
            implementation_type=CommandImplementationType.DECORATOR_BASED,
            implementation=lambda data, **kwargs: {"data": data, "kwargs": kwargs},
            metadata={"capabilities": ["TOOL_EXECUTION"]},
            version="1.0.0",
            bundle_id="bundle_x",
            hot_loadable=True,
        )
        reg.register(
            key,
            registration,
            scope=RegistryScope(namespace="bundle_x", grants=[ScopeGrant(tenant_id="tenant-a")]),
        )

        assert isinstance(reg, ScopedRegistry)
        assert reg.get(key) is not None
        assert key in reg.list_items()
        assert any(e.key == key for e in reg.list_entries())
        entry = reg.get_entry(key)
        assert entry is not None
        assert entry.scope.namespace == "bundle_x"
        assert reg.get_scope(key) is not None
        assert reg.get_scope(key).namespace == "bundle_x"

        visible = reg.list_visible(
            ScopeFilter(tenant_id="tenant-a", motet_id="*", role="*", principal_id="*")
        )
        hidden = reg.list_visible(
            ScopeFilter(tenant_id="tenant-b", motet_id="*", role="*", principal_id="*")
        )
        assert key in visible
        assert key not in hidden
        assert any(
            e.key == key
            for e in reg.list_visible_entries(
                ScopeFilter(tenant_id="tenant-a", motet_id="*", role="*", principal_id="*")
            )
        )

        summary = reg.stats()
        assert summary["registry_name"] == "command_type_registry"
        assert summary["total"] >= 1
        assert summary["by_namespace"].get("bundle_x", 0) >= 1

        removed = reg.unregister_namespace("bundle_x")
        assert key in removed
        assert reg.get(key) is None
        assert reg.get_versions(key) == []
    finally:
        reg.unregister_namespace("bundle_x")
