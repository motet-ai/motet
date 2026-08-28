"""
Motet - Manage-App Scope Helper Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-17

Description:
    Unit tests for shared tenant/motet matching used by manage-app list APIs.

Dependencies:
    - pytest
    - motet.interfaces.api.shared.scope

Usage:
    pytest tests/unit/interfaces/api/test_manage_app_scope.py -q
"""

from __future__ import annotations

from motet.interfaces.api.shared.scope import get_manage_app_scope, matches_scope


def test_no_scope_matches_all() -> None:
    assert matches_scope("acme", "default", None, None)
    assert matches_scope(None, None, None, None)


def test_tenant_filter_requires_exact_match() -> None:
    assert matches_scope("acme", "default", "acme", None)
    assert not matches_scope("acme", "default", "other", None)
    assert not matches_scope(None, "default", "acme", None)


def test_motet_filter_requires_exact_match() -> None:
    assert matches_scope("acme", "default", "acme", "default")
    assert not matches_scope("acme", "default", "acme", "other")
    assert not matches_scope("acme", None, None, "default")


def test_get_manage_app_scope_strips_blank() -> None:
    scope = get_manage_app_scope("  acme  ", "   ")
    assert scope.tenant_id == "acme"
    assert scope.motet_id is None
    assert scope.is_set

    empty = get_manage_app_scope(None, None)
    assert empty.tenant_id is None
    assert empty.motet_id is None
    assert not empty.is_set
