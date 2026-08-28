"""
Motet - Unit tests for commands API registration serialization

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-09

Description:
    Ensures GET /api/v1/commands list/detail payloads include the first-class
    CommandRegistration.description used by discovery and the manage UI.

Dependencies:
    - pytest
    - motet.interfaces.api.v1.commands
    - motet.core.commands.command_type_registry

Usage:
    pytest tests/unit/interfaces/api/test_commands_registration_dict.py -q

Notes:
    - Pure serialization coverage; does not hit Redis or workers.
"""

from __future__ import annotations

from types import SimpleNamespace

from motet.core.commands.command_type_registry import CommandImplementationType
from motet.interfaces.api.v1.commands import _registration_to_dict


def test_registration_to_dict_includes_description() -> None:
    reg = SimpleNamespace(
        command_type="core.memory_store",
        implementation_type=CommandImplementationType.DECORATOR_BASED,
        version="1.0.0",
        bundle_id=None,
        description=(
            "Store a note or memory item in distributed tenant-isolated memory, "
            "with optional tags, metadata, and embedding indexing."
        ),
        metadata={},
        data_class=None,
    )
    payload = _registration_to_dict(reg)
    assert payload["command_type"] == "core.memory_store"
    assert payload["description"] is not None
    assert "tenant-isolated memory" in payload["description"]


def test_registration_to_dict_empty_description_becomes_none() -> None:
    reg = SimpleNamespace(
        command_type="core.example",
        implementation_type=CommandImplementationType.DECORATOR_BASED,
        version="1.0.0",
        bundle_id=None,
        description="   ",
        metadata={},
        data_class=None,
    )
    payload = _registration_to_dict(reg)
    assert payload["description"] is None
