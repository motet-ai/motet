"""
Motet - Registry Naming Helpers Unit Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Unit tests for shared qualified-name parsing and scope derivation helpers.
    Validates core-default behavior, namespace/bundle mapping, and normalization.

Dependencies:
    - pytest: Test framework
    - motet.core.registry.naming: Helper functions under test

Usage:
    pytest tests/unit/core/test_registry_naming.py

Notes:
    - These tests preserve current permissive behavior (no strict validation).
"""

from motet.core.registry.naming import (
    CORE_NAMESPACE,
    normalize_namespace,
    namespace_to_bundle_id,
    parse_qualified_name,
    namespace_from_qualified_name,
    scope_from_qualified_name,
)


def test_parse_qualified_name_defaults_to_core_for_bare_name() -> None:
    namespace, local_name = parse_qualified_name("echo")
    assert namespace == CORE_NAMESPACE
    assert local_name == "echo"


def test_parse_qualified_name_splits_dotted_name() -> None:
    namespace, local_name = parse_qualified_name("sales.lookup_customer")
    assert namespace == "sales"
    assert local_name == "lookup_customer"


def test_parse_qualified_name_normalizes_whitespace() -> None:
    namespace, local_name = parse_qualified_name("  sales.lookup_customer  ")
    assert namespace == "sales"
    assert local_name == "lookup_customer"


def test_parse_qualified_name_handles_empty_string() -> None:
    namespace, local_name = parse_qualified_name("   ")
    assert namespace == CORE_NAMESPACE
    assert local_name == ""


def test_namespace_to_bundle_id_maps_core_to_none() -> None:
    assert namespace_to_bundle_id("core") is None
    assert namespace_to_bundle_id("sales") == "sales"


def test_namespace_from_qualified_name_defaults_to_core() -> None:
    assert namespace_from_qualified_name("lookup_customer") == CORE_NAMESPACE
    assert namespace_from_qualified_name("sales.lookup_customer") == "sales"


def test_scope_from_qualified_name_sets_namespace_and_bundle_id() -> None:
    sales_scope = scope_from_qualified_name("sales.lookup_customer")
    assert sales_scope.namespace == "sales"
    assert sales_scope.bundle_id == "sales"

    core_scope = scope_from_qualified_name("lookup_customer")
    assert core_scope.namespace == CORE_NAMESPACE
    assert core_scope.bundle_id is None


def test_normalize_namespace_defaults_empty_to_core() -> None:
    assert normalize_namespace(None) == CORE_NAMESPACE
    assert normalize_namespace("") == CORE_NAMESPACE
    assert normalize_namespace("  ") == CORE_NAMESPACE
    assert normalize_namespace("sales") == "sales"

