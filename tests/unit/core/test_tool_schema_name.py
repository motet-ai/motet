"""
Motet - Canonical Tool Schema Name Accessor Tests (ADR-0064)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-17

Description:
    Unit tests for ``motet.core.types.tool_schema_name``, the accessor for
    reading a tool name off CanonicalToolSchema or a canonical-like dict.
    Provider ``function.name`` dicts are not a schema shape (ADR-0137).

    Validates:
    - canonical schema and dict shapes resolve, with surrounding whitespace stripped
    - unreadable input returns "" (never a sentinel), so callers can filter on
      truthiness
    - an object whose ``.name`` is None yields "" rather than the string "None"
    - a bare Mock yields "" rather than its repr
    - orchestration.turn.prepare re-exports the same object, not a second copy

Dependencies:
    - pytest
    - motet.core.types: CanonicalToolSchema / tool_schema_name

Usage:
    pytest tests/unit/core/test_tool_schema_name.py -q

Notes:
    - The None/Mock cases are regression guards, not hypotheticals: a bare
      ``str(getattr(schema, "name", ""))`` returns "None" and a Mock repr
      respectively, and both read as valid tool names downstream.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from motet.core.types import CanonicalToolSchema, tool_schema_name


def _canonical(name: str) -> CanonicalToolSchema:
    return CanonicalToolSchema(name=name, description="d", json_schema={})


def test_reads_canonical_tool_schema() -> None:
    assert tool_schema_name(_canonical("core.help")) == "core.help"


def test_reads_canonical_dict() -> None:
    assert tool_schema_name({"name": "core.tools_search"}) == "core.tools_search"


def test_legacy_openai_function_dict_is_not_a_schema_name() -> None:
    schema = {"type": "function", "function": {"name": "core.tool_call"}}
    assert tool_schema_name(schema) == ""


@pytest.mark.parametrize(
    "schema",
    [
        _canonical(" core.help "),
        {"name": " core.help "},
    ],
)
def test_strips_surrounding_whitespace(schema: object) -> None:
    assert tool_schema_name(schema) == "core.help"


def test_canonical_name_wins_over_legacy_function_name() -> None:
    schema = {"name": "core.outer", "function": {"name": "core.inner"}}
    assert tool_schema_name(schema) == "core.outer"


@pytest.mark.parametrize(
    "schema",
    [None, {}, "not-a-schema", 42, [], {"function": {}}, {"name": 123}],
)
def test_unreadable_input_returns_empty_string(schema: object) -> None:
    assert tool_schema_name(schema) == ""


def test_none_name_attribute_does_not_become_the_string_none() -> None:
    """Regression: str(getattr(schema, "name", "")) yields "None" here."""

    class NoName:
        name = None

    assert tool_schema_name(NoName()) == ""


def test_mock_does_not_leak_its_repr_as_a_tool_name() -> None:
    """Regression: hasattr(Mock(), "name") is True, so a bare str() leaks a repr."""
    assert tool_schema_name(Mock()) == ""


def test_prepare_reexports_the_same_object() -> None:
    from motet.core.orchestration.turn import prepare

    assert prepare.tool_schema_name is tool_schema_name
