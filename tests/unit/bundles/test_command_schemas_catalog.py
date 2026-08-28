"""
Motet - Unit tests for command_schemas catalog harvest/merge

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-09

Description:
    Covers harvesting Pydantic JSON schemas from the command type registry after
    bundle load, and merging worker reload acks into the Redis catalog shape.

Dependencies:
    - pytest
    - motet.core.bundles.bundle_reload
    - motet.core.bundles.deploy
    - motet.core.commands.command_type_registry

Usage:
    pytest tests/unit/bundles/test_command_schemas_catalog.py -q
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, Field

from motet.core.bundles.bundle_reload import _command_schemas_from_registry
from motet.core.bundles.deploy import _merge_command_schemas_into_catalog
from motet.core.commands.command_type_registry import (
    CommandImplementationType,
    command_type_registry,
)


class _SampleData(BaseModel):
    name: str = Field(default="World", description="Name to greet")


def test_command_schemas_from_registry_includes_model_json_schema() -> None:
    command_type = "unit-test.sample_schema_cmd"
    try:
        command_type_registry.register_command(
            command_type=command_type,
            implementation=object,
            implementation_type=CommandImplementationType.DECORATOR_BASED,
            data_class=_SampleData,
            description="Sample",
            overwrite=True,
        )
        schemas = _command_schemas_from_registry([command_type])
        assert command_type in schemas
        assert schemas[command_type].get("properties", {}).get("name") is not None
    finally:
        command_type_registry.unregister(command_type)


def test_merge_command_schemas_into_catalog_first_ack_wins() -> None:
    catalog: Dict[str, Any] = {
        "commands": ["hello-world.hello_world"],
        "command_schemas": {},
    }
    first = {
        "registered_commands": ["hello-world.hello_world"],
        "command_schemas": {
            "hello-world.hello_world": {"title": "A", "type": "object"},
        },
    }
    second = {
        "registered_commands": ["hello-world.hello_world"],
        "command_schemas": {
            "hello-world.hello_world": {"title": "B", "type": "object"},
        },
    }
    merged = _merge_command_schemas_into_catalog(catalog, [first, second])
    assert merged["command_schemas"]["hello-world.hello_world"]["title"] == "A"


def test_merge_command_schemas_skips_error_acks() -> None:
    catalog: Dict[str, Any] = {"command_schemas": {}}
    results = [
        {"_error": True, "message": "boom"},
        {
            "command_schemas": {
                "memo.health_check": {"title": "Health", "type": "object"},
            }
        },
    ]
    merged = _merge_command_schemas_into_catalog(catalog, results)
    assert "memo.health_check" in merged["command_schemas"]
