"""
Motet - Unit tests for bundle catalog command descriptions in commands API

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-09

Description:
    Covers catalog-sourced ``description`` on GET /api/v1/commands bundle rows
    and soft fallback to the shared function-discovery manifest.

Dependencies:
    - pytest
    - motet.interfaces.api.v1.commands

Usage:
    pytest tests/unit/interfaces/api/test_commands_bundle_catalog_descriptions.py -q
"""

from __future__ import annotations

from typing import Any, Dict

from motet.interfaces.api.v1 import commands as commands_api


class _FakeRedis:
    def __init__(self, catalogs: Dict[str, Dict[str, Any]], *, manifest: Any = None) -> None:
        self._catalogs = catalogs
        self._manifest = manifest

    def get(self, key: str) -> Any:
        if key in {
            "motet:function_discovery:manifest",
            "imf:function_discovery:manifest",
        }:
            return self._manifest
        return None


def test_bundle_catalog_commands_include_stored_descriptions(monkeypatch) -> None:
    catalogs = {
        "hello-world": {
            "bundle_version": "abc",
            "targeting": {},
            "commands": ["hello-world.hello_world"],
            "command_descriptions": {
                "hello-world.hello_world": "Return a greeting message.",
            },
        }
    }
    monkeypatch.setattr(
        "motet.core.bundles.deploy._list_all_catalogs",
        lambda _redis: catalogs,
    )
    items = commands_api._get_bundle_catalog_commands(_FakeRedis(catalogs))
    assert len(items) == 1
    assert items[0]["command_type"] == "hello-world.hello_world"
    assert items[0]["description"] == "Return a greeting message."


def test_bundle_catalog_commands_include_stored_schemas(monkeypatch) -> None:
    schema = {
        "title": "HelloWorldData",
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }
    catalogs = {
        "hello-world": {
            "bundle_version": "abc",
            "targeting": {},
            "commands": ["hello-world.hello_world"],
            "command_descriptions": {
                "hello-world.hello_world": "Return a greeting message.",
            },
            "command_schemas": {
                "hello-world.hello_world": schema,
            },
        }
    }
    monkeypatch.setattr(
        "motet.core.bundles.deploy._list_all_catalogs",
        lambda _redis: catalogs,
    )
    items = commands_api._get_bundle_catalog_commands(_FakeRedis(catalogs))
    assert items[0]["data_schema"] == schema


def test_bundle_catalog_commands_fallback_to_discovery_manifest(monkeypatch) -> None:
    catalogs = {
        "memo": {
            "bundle_version": "def",
            "targeting": {},
            "commands": ["memo.health_check"],
            # Pre-command_descriptions catalog shape
        }
    }
    monkeypatch.setattr(
        "motet.core.bundles.deploy._list_all_catalogs",
        lambda _redis: catalogs,
    )
    monkeypatch.setattr(
        commands_api,
        "_command_descriptions_from_discovery_manifest",
        lambda _redis: {
            "memo.health_check": "Return bundle identity for deploy verification.",
        },
    )
    items = commands_api._get_bundle_catalog_commands(_FakeRedis(catalogs))
    assert items[0]["description"] == "Return bundle identity for deploy verification."
