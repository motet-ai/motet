"""
Motet - Cost API Cross-Tenant Aggregation Tests (ADR-0126)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-14

Description:
    Tests the all-tenants sentinel on /api/v1/cost. "All Tenants" now sums the
    catalog instead of aliasing the motet-global platform tenant, so these tests
    pin both the arithmetic and the rule that the sentinel can never widen a
    caller's access beyond their own tenant.

Dependencies:
    - fastapi.testclient: exercises the router without Redis
    - motet.interfaces.api.v1.cost: router under test
    - motet.core.tenancy: patched catalog used to expand the sentinel

Usage:
    pytest tests/unit/interfaces/api/test_cost_tenant_aggregation.py -q
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Tuple
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from motet.core.tenancy import ALL_TENANTS
from motet.core.types import Principal
from motet.interfaces.api.shared.auth import get_current_principal
from motet.interfaces.api.v1.cost import router


ADMIN = Principal(id="admin-user", roles=["admin"], tenant_id="motet-global")
ACME_USER = Principal(id="acme-user", roles=["user"], tenant_id="acme")

# Per-tenant fixtures: acme and globex have activity, motet-global holds the
# platform bucket. A correct aggregate must include all three.
DAILY: Dict[str, Dict[str, Any]] = {
    "acme": {
        "tenant_id": "acme",
        "date": "2026-07-28",
        "total_cost_usd": 1.50,
        "model_costs_usd": 1.50,
        "total_requests": 10,
        "total_prompt_tokens": 1000,
        "total_output_tokens": 200,
        "total_cache_read_tokens": 50,
        "total_cache_creation_tokens": 25,
        "total_reasoning_tokens": 5,
        "cache_savings_usd": 0.25,
    },
    "globex": {
        "tenant_id": "globex",
        "date": "2026-07-28",
        "total_cost_usd": 2.00,
        "model_costs_usd": 2.00,
        "total_requests": 4,
        "total_prompt_tokens": 400,
        "total_output_tokens": 80,
        "total_cache_read_tokens": 10,
        "total_cache_creation_tokens": 5,
        "total_reasoning_tokens": 1,
        "cache_savings_usd": 0.10,
    },
    "motet-global": {
        "tenant_id": "motet-global",
        "date": "2026-07-28",
        "total_cost_usd": 0.50,
        "model_costs_usd": 0.50,
        "total_requests": 2,
        "total_prompt_tokens": 100,
        "total_output_tokens": 20,
        "total_cache_read_tokens": 0,
        "total_cache_creation_tokens": 0,
        "total_reasoning_tokens": 0,
        "cache_savings_usd": 0.0,
    },
}

BY_PRINCIPAL: Dict[str, Dict[str, float]] = {
    "acme": {"user-a": 1.0, "shared-svc": 0.5},
    "globex": {"user-b": 1.5, "shared-svc": 0.5},
    "motet-global": {"ops": 0.5},
}

EVENTS: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {
    "acme": [
        ("1-0", {"timestamp": "2026-07-28T10:00:00Z", "model": "acme-old"}),
        ("2-0", {"timestamp": "2026-07-28T12:00:00Z", "model": "acme-new"}),
    ],
    "globex": [("3-0", {"timestamp": "2026-07-28T11:00:00Z", "model": "globex-mid"})],
    "motet-global": [
        ("4-0", {"timestamp": "2026-07-28T13:00:00Z", "model": "platform-newest"})
    ],
}

USAGE: Dict[str, Dict[str, Any]] = {
    "acme": {
        "date": "2026-07-28",
        "daily": {"cost_usd": 1.50, "requests": 10},
        "monthly": {"cost_usd": 20.0, "requests": 100},
        "limits": {"daily_limit_usd": 10.0, "alert_threshold_pct": 80.0},
    },
    "globex": {
        "date": "2026-07-28",
        "daily": {"cost_usd": 2.0, "requests": 4},
        "monthly": {"cost_usd": 5.0, "requests": 25},
        "limits": {"daily_limit_usd": 4.0, "alert_threshold_pct": 80.0},
    },
    "motet-global": {
        "date": "2026-07-28",
        "daily": {"cost_usd": 0.5, "requests": 2},
        "monthly": {"cost_usd": 1.0, "requests": 8},
        "limits": {},
    },
}


class _FakeCostService:
    def get_daily_summary(
        self, tenant_id: str, date_key: Optional[str] = None
    ) -> Dict[str, Any]:
        return DAILY.get(tenant_id, {"tenant_id": tenant_id, "date": "2026-07-28"})

    def get_daily_summary_by_principal(
        self, tenant_id: str, date_key: Optional[str] = None
    ) -> Dict[str, Any]:
        return {
            "tenant_id": tenant_id,
            "date": "2026-07-28",
            "by_principal": BY_PRINCIPAL.get(tenant_id, {}),
        }

    def get_cost_events(
        self, tenant_id: str, count: int = 100, start_id: str = "+"
    ) -> List[Tuple[str, Dict[str, Any]]]:
        return list(EVENTS.get(tenant_id, []))


class _FakeBudgetEnforcer:
    def get_usage_summary(
        self, tenant_id: str, date_key: Optional[str] = None
    ) -> Dict[str, Any]:
        return USAGE.get(tenant_id, {"date": "2026-07-28"})


class _FakeTenantRecord:
    def __init__(self, tenant_id: str) -> None:
        self.id = tenant_id


class _FakeTenantRegistry:
    def list_tenants(self, **_kwargs: Any) -> List[_FakeTenantRecord]:
        return [_FakeTenantRecord(t) for t in ("acme", "globex", "motet-global")]


class _BrokenTenantRegistry:
    def __init__(self) -> None:
        raise ConnectionError("catalog Redis unavailable")


def _client(principal: Principal) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_principal] = lambda: principal
    return TestClient(app)


@pytest.fixture(autouse=True)
def cost_backends() -> Iterator[None]:
    """Patch cost services and the tenant catalog used to expand the sentinel."""
    with (
        patch(
            "motet.core.cost.get_cost_tracking_service",
            return_value=_FakeCostService(),
        ),
        patch(
            "motet.core.cost.get_budget_enforcer",
            return_value=_FakeBudgetEnforcer(),
        ),
        patch("motet.core.tenancy.TenantRegistry", _FakeTenantRegistry),
    ):
        yield


def test_all_tenants_sums_the_catalog() -> None:
    """The headline number covers every tenant, not just the platform bucket."""
    response = _client(ADMIN).get(
        "/api/v1/cost/summary", params={"tenant_id": ALL_TENANTS}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    # 1.50 (acme) + 2.00 (globex) + 0.50 (platform) — the old behavior reported
    # only the 0.50 platform bucket under an "All Tenants" label.
    assert body["total_cost_usd"] == pytest.approx(4.00)
    assert body["total_requests"] == 16
    assert body["total_prompt_tokens"] == 1500
    assert body["cache_savings_usd"] == pytest.approx(0.35)
    assert body["tenant_id"] == ALL_TENANTS
    assert body["aggregated_tenant_ids"] == ["acme", "globex", "motet-global"]


def test_platform_tenant_is_just_one_tenant() -> None:
    """Selecting motet-global returns only the platform bucket."""
    body = _client(ADMIN).get(
        "/api/v1/cost/summary", params={"tenant_id": "motet-global"}
    ).json()

    assert body["total_cost_usd"] == pytest.approx(0.50)
    assert body["tenant_id"] == "motet-global"
    assert body["aggregated_tenant_ids"] is None


def test_single_tenant_query_is_unchanged() -> None:
    """An explicit tenant still reports only that tenant."""
    body = _client(ADMIN).get("/api/v1/cost/summary", params={"tenant_id": "acme"}).json()

    assert body["total_cost_usd"] == pytest.approx(1.50)
    assert body["tenant_id"] == "acme"


def test_sentinel_does_not_widen_access_for_non_admin() -> None:
    """A tenant-scoped caller passing the sentinel sees only their own tenant."""
    body = _client(ACME_USER).get(
        "/api/v1/cost/summary", params={"tenant_id": ALL_TENANTS}
    ).json()

    assert body["total_cost_usd"] == pytest.approx(1.50)
    assert body["tenant_id"] == "acme"
    assert body["aggregated_tenant_ids"] is None


def test_omitted_tenant_defaults_to_own_tenant() -> None:
    """No tenant_id keeps the pre-existing default rather than aggregating."""
    body = _client(ACME_USER).get("/api/v1/cost/summary").json()

    assert body["tenant_id"] == "acme"
    assert body["aggregated_tenant_ids"] is None


def test_by_principal_merges_principals_across_tenants() -> None:
    """A principal active in two tenants is summed into one row."""
    body = _client(ADMIN).get(
        "/api/v1/cost/summary/by_principal", params={"tenant_id": ALL_TENANTS}
    ).json()

    assert body["by_principal"]["shared-svc"] == pytest.approx(1.0)
    assert body["by_principal"]["user-a"] == pytest.approx(1.0)
    assert body["by_principal"]["ops"] == pytest.approx(0.5)
    assert body["aggregated_tenant_ids"] == ["acme", "globex", "motet-global"]


def test_events_merge_and_sort_newest_first() -> None:
    """Per-tenant streams are interleaved by timestamp, not concatenated."""
    body = _client(ADMIN).get(
        "/api/v1/cost/events", params={"tenant_id": ALL_TENANTS}
    ).json()

    assert [e["model"] for e in body["events"]] == [
        "platform-newest",
        "acme-new",
        "globex-mid",
        "acme-old",
    ]
    assert body["aggregated_tenant_ids"] == ["acme", "globex", "motet-global"]


def test_usage_aggregate_reports_no_budget() -> None:
    """Budgets are per tenant, so an aggregate must not invent a combined limit."""
    body = _client(ADMIN).get(
        "/api/v1/cost/usage", params={"tenant_id": ALL_TENANTS}
    ).json()

    assert body["daily"]["cost_usd"] == pytest.approx(4.0)
    assert body["monthly"]["cost_usd"] == pytest.approx(26.0)
    assert body["limits"] == {}
    assert body["budget_status"] == "not_applicable"


def test_usage_single_tenant_still_reports_budget_status() -> None:
    """Single-tenant budget evaluation is untouched by aggregation support."""
    body = _client(ADMIN).get("/api/v1/cost/usage", params={"tenant_id": "globex"}).json()

    # 2.00 spent against a 4.00 daily limit is 50%, below the 80% threshold.
    assert body["budget_status"] == "ok"
    assert body["limits"]["daily_limit_usd"] == pytest.approx(4.0)


@pytest.mark.parametrize(
    "path",
    (
        "/api/v1/cost/summary",
        "/api/v1/cost/summary/by_principal",
        "/api/v1/cost/usage",
        "/api/v1/cost/events",
    ),
)
def test_non_admin_cannot_name_another_tenant(path: str) -> None:
    """Issue #143: an explicit foreign tenant_id is 403, not a silent leak."""
    response = _client(ACME_USER).get(path, params={"tenant_id": "globex"})

    assert response.status_code == 403, response.text
    assert "another tenant" in response.json()["detail"]


def test_non_admin_may_name_own_tenant() -> None:
    """Naming the caller's own tenant is not a cross-tenant request."""
    response = _client(ACME_USER).get(
        "/api/v1/cost/summary", params={"tenant_id": "acme"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["tenant_id"] == "acme"
    assert response.json()["total_cost_usd"] == pytest.approx(1.50)


def test_admin_may_name_another_tenant() -> None:
    """Admins retain explicit cross-tenant access."""
    response = _client(ADMIN).get(
        "/api/v1/cost/summary", params={"tenant_id": "globex"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["tenant_id"] == "globex"
    assert response.json()["total_cost_usd"] == pytest.approx(2.00)


def test_global_scope_claim_may_name_another_tenant() -> None:
    """tenant_scope=global is the same predicate /api/v1/tenants uses."""
    scoped = Principal(
        id="platform-reader",
        roles=["user"],
        tenant_id="acme",
        claims={"tenant_scope": "global"},
    )
    response = _client(scoped).get(
        "/api/v1/cost/summary", params={"tenant_id": "globex"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["tenant_id"] == "globex"


def test_unavailable_catalog_falls_back_to_own_tenant() -> None:
    """A broken catalog degrades to the caller's tenant instead of erroring."""
    with patch("motet.core.tenancy.TenantRegistry", _BrokenTenantRegistry):
        response = _client(ADMIN).get(
            "/api/v1/cost/summary", params={"tenant_id": ALL_TENANTS}
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenant_id"] == "motet-global"
    assert body["aggregated_tenant_ids"] is None
