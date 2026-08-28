"""
Motet - Manage-App Scope Query Helpers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-17

Description:
    Shared tenant/motet query parameters and equality matching for manage-app
    list endpoints (Tasks, Memory, Schedules, Agents). Null/empty wanted
    values mean "all" and do not filter.

Dependencies:
    - fastapi: Query / Depends
    - dataclasses: ManageAppScope

Usage:
    from motet.interfaces.api.shared.scope import (
        ManageAppScope,
        get_manage_app_scope,
        matches_scope,
    )

    @router.get("/items")
    async def list_items(scope: ManageAppScope = Depends(get_manage_app_scope)):
        if matches_scope(row.tenant_id, row.motet_id, scope.tenant_id, scope.motet_id):
            ...

Notes:
    - Agent bundle targeting and memory SCAN globs stay in their routers.
    - Workspace containers remain tenant-only (no motet on the binding).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from fastapi import Query


@dataclass(frozen=True)
class ManageAppScope:
    """Optional tenant/motet filter from the manage-app selector."""

    tenant_id: Optional[str] = None
    motet_id: Optional[str] = None

    @property
    def is_set(self) -> bool:
        return bool(self.tenant_id or self.motet_id)


def matches_scope(
    actual_tenant: Any,
    actual_motet: Any,
    wanted_tenant: Optional[str],
    wanted_motet: Optional[str],
) -> bool:
    """True when actual ids match the optional wanted tenant/motet."""
    tid = (wanted_tenant or "").strip()
    mid = (wanted_motet or "").strip()
    if tid and str(actual_tenant or "").strip() != tid:
        return False
    if mid and str(actual_motet or "").strip() != mid:
        return False
    return True


def get_manage_app_scope(
    tenant_id: Optional[str] = Query(
        None,
        description="When set, only resources for this tenant. Omitted means all tenants.",
    ),
    motet_id: Optional[str] = Query(
        None,
        description="When set, only resources for this motet. Omitted means all motets.",
    ),
) -> ManageAppScope:
    """FastAPI dependency that reads manage-app tenant_id / motet_id query params."""
    return ManageAppScope(
        tenant_id=(tenant_id or "").strip() or None,
        motet_id=(motet_id or "").strip() or None,
    )
