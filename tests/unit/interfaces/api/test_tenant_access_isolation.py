"""
Motet - HTTP Tenant Isolation Regression Tests (issue #214)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-15

Description:
    Regression tests for caller-supplied tenant_id / motet_id on schedules,
    service accounts, deploy catalogs, skills catalogs, and debug routes.
    A request tenant_id is a name, not permission: an acme user naming
    globex must get 403, and omitted tenant_id must resolve to acme.

Dependencies:
    - fastapi.testclient: exercises routers without Redis
    - motet.interfaces.api.shared.auth: require_tenant_access invariant
    - motet.interfaces.api.v1: schedules, service_accounts, deploy, skills, debug

Usage:
    pytest tests/unit/interfaces/api/test_tenant_access_isolation.py -q

Notes:
    - Does not implement per-tenant worker pools
    - Debug tests patch DEBUG_MODE so they do not depend on import-time env
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from motet.core.orchestration.scheduling.models import (
    ScheduleMetadata,
    ScheduleStatus,
    ScheduleType,
)
from motet.core.security.service_accounts import ServiceAccountToken
from motet.core.types import Principal
from motet.interfaces.api.shared.auth import (
    get_current_principal,
    require_motet_access,
    require_tenant_access,
)
from motet.interfaces.api.v1 import debug as debug_module
from motet.interfaces.api.v1.debug import router as debug_router
from motet.interfaces.api.v1.deploy import router as deploy_router
from motet.interfaces.api.v1.schedules import router as schedules_router
from motet.interfaces.api.v1.service_accounts import router as service_accounts_router
from motet.interfaces.api.v1.skills import router as skills_router

ADMIN = Principal(
    id="admin-user",
    roles=["admin"],
    tenant_id="motet-global",
    motet_id="default",
)
ACME_USER = Principal(
    id="acme-user",
    roles=["user"],
    tenant_id="acme",
    motet_id="prod",
)


def _client(router: Any, principal: Principal) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_principal] = lambda: principal
    return TestClient(app)


def _schedule(*, schedule_id: str, tenant_id: str) -> ScheduleMetadata:
    return ScheduleMetadata(
        schedule_id=schedule_id,
        command_id=f"cmd-{schedule_id}",
        command_type="core.echo",
        schedule_type=ScheduleType.DELAYED,
        status=ScheduleStatus.ACTIVE,
        tenant_id=tenant_id,
        created_by="acme-user",
    )


def _sa_token(*, token_id: str, tenant_id: str, motet_id: str = "prod") -> ServiceAccountToken:
    now = datetime.now(timezone.utc)
    return ServiceAccountToken(
        id=token_id,
        name="ci",
        principal_id=f"service-account:{token_id}",
        tenant_id=tenant_id,
        motet_id=motet_id,
        roles=["ci"],
        created_at=now,
        expires_at=now,
        created_by="acme-user",
    )


# ---------------------------------------------------------------------------
# require_tenant_access / require_motet_access
# ---------------------------------------------------------------------------


def test_require_tenant_access_omitted_uses_principal() -> None:
    assert require_tenant_access(ACME_USER, None) == "acme"


def test_require_tenant_access_foreign_tenant_forbidden() -> None:
    with pytest.raises(HTTPException) as exc:
        require_tenant_access(ACME_USER, "globex")
    assert exc.value.status_code == 403


def test_require_tenant_access_admin_may_name_foreign_tenant() -> None:
    assert require_tenant_access(ADMIN, "globex") == "globex"


def test_require_motet_access_foreign_motet_forbidden() -> None:
    with pytest.raises(HTTPException) as exc:
        require_motet_access(ACME_USER, "staging")
    assert exc.value.status_code == 403


def test_require_motet_access_admin_may_name_foreign_motet() -> None:
    assert require_motet_access(ADMIN, "staging") == "staging"


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


def test_schedule_create_foreign_tenant_forbidden() -> None:
    client = _client(schedules_router, ACME_USER)
    response = client.post(
        "/api/v1/schedules/",
        json={
            "command_type": "core.echo",
            "command_data": {},
            "schedule_type": "delayed",
            "delay_seconds": 30,
            "tenant_id": "globex",
        },
    )
    assert response.status_code == 403
    assert "another tenant" in response.json()["detail"]


def test_schedule_create_created_by_impersonation_forbidden() -> None:
    client = _client(schedules_router, ACME_USER)
    response = client.post(
        "/api/v1/schedules/",
        json={
            "command_type": "core.echo",
            "command_data": {},
            "schedule_type": "delayed",
            "delay_seconds": 30,
            "created_by": "other-user",
        },
    )
    assert response.status_code == 403
    assert "impersonate" in response.json()["detail"]


def test_schedule_list_foreign_tenant_forbidden() -> None:
    client = _client(schedules_router, ACME_USER)
    response = client.get("/api/v1/schedules/", params={"tenant_id": "globex"})
    assert response.status_code == 403


def test_schedule_list_omitted_tenant_filters_to_principal() -> None:
    client = _client(schedules_router, ACME_USER)
    with patch(
        "motet.interfaces.api.v1.schedules.schedule_manager.list_schedules",
        return_value=[],
    ) as list_schedules:
        response = client.get("/api/v1/schedules/")
    assert response.status_code == 200
    filters = list_schedules.call_args.args[0]
    assert filters.tenant_id == "acme"


def test_schedule_get_foreign_tenant_forbidden() -> None:
    client = _client(schedules_router, ACME_USER)
    with patch(
        "motet.interfaces.api.v1.schedules.schedule_manager.get_schedule",
        return_value=_schedule(schedule_id="sched-g", tenant_id="globex"),
    ):
        response = client.get("/api/v1/schedules/sched-g")
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Service accounts
# ---------------------------------------------------------------------------


def test_service_account_create_foreign_tenant_forbidden() -> None:
    client = _client(service_accounts_router, ACME_USER)
    response = client.post(
        "/api/v1/service-accounts",
        json={"name": "ci", "roles": ["ci"], "tenant_id": "globex"},
    )
    assert response.status_code == 403
    assert "another tenant" in response.json()["detail"]


def test_service_account_create_foreign_motet_forbidden() -> None:
    client = _client(service_accounts_router, ACME_USER)
    response = client.post(
        "/api/v1/service-accounts",
        json={"name": "ci", "roles": ["ci"], "motet_id": "staging"},
    )
    assert response.status_code == 403
    assert "another motet" in response.json()["detail"]


def test_service_account_list_foreign_tenant_forbidden() -> None:
    client = _client(service_accounts_router, ACME_USER)
    response = client.get("/api/v1/service-accounts", params={"tenant_id": "globex"})
    assert response.status_code == 403


def test_service_account_list_omitted_tenant_filters_to_principal() -> None:
    client = _client(service_accounts_router, ACME_USER)
    manager = Mock()
    manager.list_service_accounts.return_value = []
    with patch(
        "motet.interfaces.api.v1.service_accounts.get_service_account_manager",
        return_value=manager,
    ):
        response = client.get("/api/v1/service-accounts")
    assert response.status_code == 200
    manager.list_service_accounts.assert_called_once_with(tenant_id="acme", motet_id=None)


def test_service_account_revoke_foreign_tenant_forbidden() -> None:
    client = _client(service_accounts_router, ACME_USER)
    manager = Mock()
    manager.verify_service_account.return_value = _sa_token(
        token_id="sa_globex", tenant_id="globex"
    )
    with patch(
        "motet.interfaces.api.v1.service_accounts.get_service_account_manager",
        return_value=manager,
    ):
        response = client.delete("/api/v1/service-accounts/sa_globex")
    assert response.status_code == 403
    manager.revoke_service_account.assert_not_called()


# ---------------------------------------------------------------------------
# Deploy / skills catalogs
# ---------------------------------------------------------------------------


BUNDLES: List[Dict[str, Any]] = [
    {
        "bundle_id": "acme.demo",
        "bundle_version": "1",
        "targeting": {"tenant_ids": ["acme"], "motet_ids": []},
    },
    {
        "bundle_id": "globex.secret",
        "bundle_version": "1",
        "targeting": {"tenant_ids": ["globex"], "motet_ids": []},
    },
    {
        "bundle_id": "shared.tools",
        "bundle_version": "1",
        "targeting": {},
    },
]

CATALOGS: Dict[str, Dict[str, Any]] = {
    "acme.demo": {
        "bundle_id": "acme.demo",
        "bundle_version": "1",
        "targeting": {"tenant_ids": ["acme"], "motet_ids": []},
        "skills": [{"id": "acme.demo.pdf", "name": "pdf"}],
        "exec": {},
    },
    "globex.secret": {
        "bundle_id": "globex.secret",
        "bundle_version": "1",
        "targeting": {"tenant_ids": ["globex"], "motet_ids": []},
        "skills": [{"id": "globex.secret.hidden", "name": "hidden"}],
        "exec": {},
    },
}


@contextmanager
def _patched_deploy_list() -> Iterator[None]:
    with (
        patch(
            "motet.core.distributed.redis_manager.get_sync_redis_client",
            return_value=object(),
        ),
        patch(
            "motet.core.bundles.deploy._list_all_bundles",
            return_value=list(BUNDLES),
        ),
        patch("motet.core.bundles.deploy._get_catalog", return_value={}),
        patch("motet.core.bundles.deploy._get_worker_state", return_value={}),
    ):
        yield


def test_deploy_list_foreign_tenant_forbidden() -> None:
    client = _client(deploy_router, ACME_USER)
    response = client.get("/api/v1/deploy", params={"tenant_id": "globex"})
    assert response.status_code == 403


def test_deploy_list_omitted_tenant_hides_foreign_targeted_bundles() -> None:
    client = _client(deploy_router, ACME_USER)
    with _patched_deploy_list():
        response = client.get("/api/v1/deploy")
    assert response.status_code == 200, response.text
    ids = {row["bundle_id"] for row in response.json()["bundles"]}
    assert "acme.demo" in ids
    assert "shared.tools" in ids
    assert "globex.secret" not in ids


def test_skills_list_foreign_tenant_forbidden() -> None:
    client = _client(skills_router, ACME_USER)
    response = client.get("/api/v1/skills", params={"tenant_id": "globex"})
    assert response.status_code == 403


def test_skills_list_omitted_tenant_hides_foreign_targeted_skills() -> None:
    client = _client(skills_router, ACME_USER)
    with (
        patch(
            "motet.core.distributed.redis_manager.get_sync_redis_client",
            return_value=object(),
        ),
        patch("motet.core.bundles.deploy._list_all_catalogs", return_value=CATALOGS),
    ):
        response = client.get("/api/v1/skills")
    assert response.status_code == 200, response.text
    ids = {row["skill_id"] for row in response.json()["skills"]}
    assert "acme.demo.pdf" in ids
    assert "globex.secret.hidden" not in ids


def test_admin_may_filter_deploy_to_foreign_tenant() -> None:
    client = _client(deploy_router, ADMIN)
    with _patched_deploy_list():
        response = client.get("/api/v1/deploy", params={"tenant_id": "globex"})
    assert response.status_code == 200, response.text
    ids = {row["bundle_id"] for row in response.json()["bundles"]}
    assert "globex.secret" in ids
    assert "shared.tools" in ids
    assert "acme.demo" not in ids


# ---------------------------------------------------------------------------
# Debug admin-only
# ---------------------------------------------------------------------------


@pytest.fixture
def debug_mode_on() -> Iterator[None]:
    previous = debug_module.DEBUG_MODE
    debug_module.DEBUG_MODE = True
    try:
        yield
    finally:
        debug_module.DEBUG_MODE = previous


def test_debug_non_admin_forbidden_when_debug_mode_on(debug_mode_on: None) -> None:
    client = _client(debug_router, ACME_USER)
    response = client.get("/api/v1/debug/commands")
    assert response.status_code == 403
    assert "admin" in response.json()["detail"].lower()


def test_debug_admin_passes_auth_gate(debug_mode_on: None) -> None:
    client = _client(debug_router, ADMIN)
    response = client.get("/api/v1/debug/commands")
    # Auth gate passed; Redis may be absent in unit tests (500), but not 403.
    assert response.status_code != 403
