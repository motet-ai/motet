"""
Motet - Workspace Containers API Tests (ADR-0106 Slice C)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-14

Pin the GET /api/v1/workspace-containers contract that the
WorkspaceContainersPage UI consumes:

    * envelope shape (status / config / tenants / containers / timestamp)
    * each container row carries every field the FE renders, including
      the warm-only metadata block
    * tenant_id query filter narrows ``containers`` but NOT ``tenants``
      (the panel header always shows the global per-tenant cardinality)
    * config block reflects current env (kill switch + warm gate +
      idle TTL + cap)

Auth note: this endpoint requires authentication (ADR-0066 / #68). Tests
override ``get_current_principal`` with an admin so existing contract
assertions stay focused on payload shape.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterator, List
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from motet.core.distributed.workspace_container_registry import WorkspaceContainerBinding
from motet.core.types import Principal
from motet.interfaces.api.shared.auth import get_current_principal
from motet.interfaces.http import app

ADMIN = Principal(id="admin-user", roles=["admin"], tenant_id="motet-global")
ACME_USER = Principal(id="acme-user", roles=["user"], tenant_id="tenant-a")


client = TestClient(app)


@pytest.fixture(autouse=True)
def _admin_auth() -> Iterator[None]:
    app.dependency_overrides[get_current_principal] = lambda: ADMIN
    yield
    app.dependency_overrides.pop(get_current_principal, None)


def _binding(
    *,
    tenant_id: str,
    conversation_id: str,
    image_stack: str = "python-minimal",
    mode: str = "cold",
    container_id: str = "deadbeefcafef00d",
    created_at: float | None = None,
    last_active_at: float | None = None,
    metadata: Dict[str, Any] | None = None,
) -> WorkspaceContainerBinding:
    now = time.time()
    return WorkspaceContainerBinding(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        image_stack=image_stack,
        container_id=container_id,
        image="python:3.11-slim",
        mode=mode,
        created_at=created_at or now - 600,
        last_active_at=last_active_at or now - 30,
        worker_attribution="cloud_worker1",
        metadata=metadata or {},
    )


@pytest.fixture
def fake_bindings() -> List[WorkspaceContainerBinding]:
    """Three bindings spread across two tenants and both modes.

    Layout chosen so the test can verify (a) sorting by ``last_active_at``,
    (b) per-tenant cardinality math, and (c) the warm-only metadata
    surfacing for warm rows only.
    """
    now = time.time()
    return [
        _binding(
            tenant_id="tenant-a",
            conversation_id="conv-1",
            mode="cold",
            container_id="aaaaaaaaaaaaaaaa",
            last_active_at=now - 5,  # most recent
        ),
        _binding(
            tenant_id="tenant-a",
            conversation_id="conv-2",
            mode="warm",
            container_id="bbbbbbbbbbbbbbbb",
            last_active_at=now - 50,
            metadata={
                "script_sha256": "f" * 64,
                "script_logical_name": "counter.py",
            },
        ),
        _binding(
            tenant_id="tenant-b",
            conversation_id="conv-x",
            mode="cold",
            container_id="cccccccccccccccc",
            last_active_at=now - 200,  # oldest
        ),
    ]


@pytest.fixture
def patched_registry(fake_bindings):
    """Patch the registry constructor to a stub that returns our fixture data.

    The endpoint instantiates ``WorkspaceContainerRegistry()`` per call — so
    we replace the class symbol the endpoint imports lazily; replacing it
    on the module the API imports via gives us a hermetic test that does
    not touch real Redis.
    """

    class _StubRegistry:
        DEFAULT_IDLE_TTL_SECONDS = 1800

        def __init__(self) -> None:  # accept whatever args
            pass

        def list_all(self):
            return list(fake_bindings)

    with patch(
        "motet.core.distributed.workspace_container_registry.WorkspaceContainerRegistry",
        _StubRegistry,
    ):
        yield _StubRegistry


# ---------------------------------------------------------------------------
# Envelope shape
# ---------------------------------------------------------------------------


def test_envelope_shape_pins_fe_contract(patched_registry) -> None:
    """Every top-level key the WorkspaceContainersPage reads MUST be present."""
    r = client.get("/api/v1/workspace-containers")
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("status", "config", "tenants", "containers", "timestamp"):
        assert key in body, f"missing envelope key {key!r}: {body!r}"
    assert body["status"] == "success"


def test_container_row_carries_every_fe_field(patched_registry) -> None:
    body = client.get("/api/v1/workspace-containers").json()
    assert body["containers"], "fixture should yield at least one container"
    fe_contract = {
        "tenant_id",
        "conversation_id",
        "image_stack",
        "container_id",
        "container_id_short",
        "image",
        "mode",
        "endpoint",
        "created_at",
        "last_active_at",
        "idle_seconds",
        "worker_attribution",
        "script_sha256",
        "script_logical_name",
    }
    for row in body["containers"]:
        missing = fe_contract - set(row.keys())
        assert not missing, f"container row missing FE-contract keys: {missing}"


def test_warm_row_surfaces_warm_metadata(patched_registry) -> None:
    """Stateful-mode containers must surface the supervisor's loaded module
    name + sha256; cold rows leave them None so the FE can hide the
    'Warm Supervisor' details panel."""
    body = client.get("/api/v1/workspace-containers").json()
    rows = {r["container_id_short"]: r for r in body["containers"]}
    warm = rows["bbbbbbbbbbbb"]
    assert warm["mode"] == "warm"
    assert warm["script_logical_name"] == "counter.py"
    assert warm["script_sha256"] == "f" * 64

    cold = rows["aaaaaaaaaaaa"]
    assert cold["mode"] == "cold"
    assert cold["script_logical_name"] is None
    assert cold["script_sha256"] is None


def test_container_id_short_is_first_twelve_chars(patched_registry) -> None:
    body = client.get("/api/v1/workspace-containers").json()
    for row in body["containers"]:
        assert row["container_id_short"] == row["container_id"][:12]
        assert len(row["container_id_short"]) <= 12


def test_idle_seconds_is_non_negative_and_monotone_with_last_active(
    patched_registry,
) -> None:
    body = client.get("/api/v1/workspace-containers").json()
    rows = body["containers"]
    for r in rows:
        assert r["idle_seconds"] >= 0
    # Most-recently-active row should have the smallest idle_seconds.
    by_active = sorted(rows, key=lambda r: r["last_active_at"], reverse=True)
    assert by_active[0]["idle_seconds"] <= by_active[-1]["idle_seconds"]


# ---------------------------------------------------------------------------
# Sorting + filtering
# ---------------------------------------------------------------------------


def test_default_sort_is_last_active_descending(patched_registry) -> None:
    """The FE depends on the API to pre-sort so the ``defaultSortOrder``
    on the Idle column lines up with the row order on first paint."""
    body = client.get("/api/v1/workspace-containers").json()
    last_actives = [r["last_active_at"] for r in body["containers"]]
    assert last_actives == sorted(last_actives, reverse=True)


def test_tenant_filter_narrows_containers_but_not_tenants_map(
    patched_registry,
) -> None:
    """Per the endpoint contract, ``tenants`` is the unfiltered global
    cardinality so the panel header doesn't shift when the user
    selects a tenant."""
    body = client.get("/api/v1/workspace-containers?tenant_id=tenant-a").json()
    assert all(r["tenant_id"] == "tenant-a" for r in body["containers"])
    assert len(body["containers"]) == 2
    assert body["tenants"] == {"tenant-a": 2, "tenant-b": 1}


def test_unknown_tenant_returns_empty_list_not_404(patched_registry) -> None:
    body = client.get("/api/v1/workspace-containers?tenant_id=ghost").json()
    assert body["containers"] == []
    # The unfiltered cardinality is unchanged.
    assert body["tenants"] == {"tenant-a": 2, "tenant-b": 1}


# ---------------------------------------------------------------------------
# Config callout
# ---------------------------------------------------------------------------


def test_config_block_reflects_env(monkeypatch, patched_registry) -> None:
    monkeypatch.setenv("MOTET_WORKSPACE_CONTAINER_ENABLED", "false")
    monkeypatch.setenv("MOTET_WORKSPACE_STATEFUL_MODE_ENABLED", "false")
    monkeypatch.setenv("MOTET_WORKSPACE_CONTAINER_IDLE_TTL_SECONDS", "300")
    monkeypatch.setenv("MOTET_WORKSPACE_CONTAINER_MAX_PER_TENANT", "7")
    monkeypatch.setenv("MOTET_WORKSPACE_CONTAINER_MAX_BYTES", "2048")

    body = client.get("/api/v1/workspace-containers").json()
    cfg = body["config"]
    assert cfg["enabled"] is False
    assert cfg["stateful_mode_enabled"] is False
    assert cfg["idle_ttl_seconds"] == 300
    assert cfg["max_per_tenant"] == 7
    assert cfg["max_bytes"] == 2048


def test_config_defaults_when_env_unset(monkeypatch, patched_registry) -> None:
    for var in (
        "MOTET_WORKSPACE_CONTAINER_ENABLED",
        "MOTET_WORKSPACE_STATEFUL_MODE_ENABLED",
        "MOTET_WORKSPACE_CONTAINER_IDLE_TTL_SECONDS",
        "MOTET_WORKSPACE_CONTAINER_MAX_PER_TENANT",
        "MOTET_WORKSPACE_CONTAINER_MAX_BYTES",
    ):
        monkeypatch.delenv(var, raising=False)

    body = client.get("/api/v1/workspace-containers").json()
    cfg = body["config"]
    assert cfg["enabled"] is True  # default-on per ADR-0106
    assert cfg["stateful_mode_enabled"] is True  # default-on per ADR-0106
    assert cfg["idle_ttl_seconds"] == 1800  # ADR-0106 default
    assert cfg["max_per_tenant"] == 100  # ADR-0106 default
    assert cfg["max_bytes"] == 1073741824  # 1 GiB default


# ---------------------------------------------------------------------------
# Auth contract (ADR-0066 / #68)
# ---------------------------------------------------------------------------


def test_endpoint_requires_auth(patched_registry) -> None:
    """Unauthenticated callers are rejected; the ops.html-style open read is gone."""
    app.dependency_overrides.pop(get_current_principal, None)
    r = client.get("/api/v1/workspace-containers")
    assert r.status_code == 401


def test_non_admin_cannot_name_another_tenant(patched_registry) -> None:
    app.dependency_overrides[get_current_principal] = lambda: ACME_USER
    r = client.get("/api/v1/workspace-containers", params={"tenant_id": "tenant-b"})
    assert r.status_code == 403


def test_non_admin_sees_only_own_tenant(patched_registry) -> None:
    app.dependency_overrides[get_current_principal] = lambda: ACME_USER
    r = client.get("/api/v1/workspace-containers")
    assert r.status_code == 200
    body = r.json()
    assert all(c["tenant_id"] == "tenant-a" for c in body["containers"])
    assert list(body["tenants"].keys()) == ["tenant-a"]
