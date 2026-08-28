"""
Motet - Devices API Unit Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Unit tests for /api/v1/devices. Verifies list response shape
    (last_connected_at exposure, revoked filtering), explicit worker_id
    registration (ADR-0123 Phase D): validation, uniqueness conflict mapping
    (400 vs 409), pass-through to the registry, and device-scoped edge worker
    readiness deregister (device token / owning principal).

Dependencies:
    - fastapi.testclient: API testing
    - motet.interfaces.api.v1.devices: router under test
    - motet.core.edge.device_registry: validate_worker_id under test

Usage:
    pytest tests/unit/test_devices_api.py -q
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from motet.core.edge.device_registry import DeviceAuthSession, validate_worker_id
from motet.core.types import Principal
from motet.interfaces.api.shared.auth import get_current_principal
from motet.interfaces.api.v1.devices import router


@dataclass(frozen=True)
class _Device:
    device_id: str
    worker_id: str
    device_name: str
    principal_id: str
    tenant_id: str
    created_at: str
    revoked: bool
    last_connected_at: str | None = None
    command_scope: str = "principal"


class _FakeDeviceRegistry:
    def list_devices(self, _principal_id: str, tenant_id=None):
        return [
            _Device(
                device_id="d1000001-0000-0000-0000-000000000000",
                worker_id="edge_d1000001",
                device_name="mbp",
                principal_id="user_1",
                tenant_id="tenant_1",
                created_at="2026-03-26T00:00:00Z",
                revoked=False,
                last_connected_at="2026-03-26T10:00:00Z",
            ),
            _Device(
                device_id="d2000002-0000-0000-0000-000000000000",
                worker_id="edge_d2000002",
                device_name="old",
                principal_id="user_1",
                tenant_id="tenant_1",
                created_at="2026-03-25T00:00:00Z",
                revoked=True,
                last_connected_at=None,
            ),
        ]


def test_devices_list_exposes_last_connected_at(monkeypatch) -> None:
    import motet.interfaces.api.v1.devices as devices_api

    monkeypatch.setattr(devices_api, "EdgeDeviceRegistry", _FakeDeviceRegistry)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        id="user_1",
        tenant_id="tenant_1",
        roles=["user"],
    )
    client = TestClient(app)

    r = client.get("/api/v1/devices")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["device_id"] == "d1000001-0000-0000-0000-000000000000"
    assert rows[0]["last_connected_at"] == "2026-03-26T10:00:00Z"


class TestValidateWorkerId:
    """Explicit worker id validation (ADR-0123 Phase D)."""

    def test_accepts_multi_app_builder_ids(self) -> None:
        assert validate_worker_id("edge_app_builder_myapp") == "edge_app_builder_myapp"
        assert validate_worker_id("edge_app_builder") == "edge_app_builder"
        assert validate_worker_id("edge_a1") == "edge_a1"
        assert validate_worker_id("edge_app.builder-2") == "edge_app.builder-2"
        # Surrounding whitespace is normalized away.
        assert validate_worker_id("  edge_app_builder_x ") == "edge_app_builder_x"

    def test_rejects_bad_shapes(self) -> None:
        for bad in (
            "",
            "edge_",  # nothing after prefix
            "cloud_worker1",  # not an edge id
            "edge_App",  # uppercase
            "edge_-x",  # first char after prefix must be alnum
            "edge_a b",  # whitespace
            "edge_" + "a" * 60,  # > 64 chars total
        ):
            with pytest.raises(ValueError):
                validate_worker_id(bad)


def _register_app(monkeypatch, fake_registry_cls) -> TestClient:
    import motet.interfaces.api.v1.devices as devices_api

    monkeypatch.setattr(devices_api, "EdgeDeviceRegistry", fake_registry_cls)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        id="user_1",
        tenant_id="tenant_1",
        roles=["user"],
    )
    return TestClient(app)


class TestRegisterExplicitWorkerId:
    """POST /register with explicit worker_id (ADR-0123 Phase D)."""

    def test_worker_id_passed_through_to_registry(self, monkeypatch) -> None:
        captured: dict = {}

        class _FakeRegistry:
            def register_device(self, **kwargs):
                captured.update(kwargs)
                return ("dev-1", kwargs["worker_id"] or "edge_derived", "ld_secret")

        client = _register_app(monkeypatch, _FakeRegistry)
        r = client.post(
            "/api/v1/devices/register",
            json={"device_name": "builder", "worker_id": "edge_app_builder_myapp"},
        )
        assert r.status_code == 201
        assert r.json()["worker_id"] == "edge_app_builder_myapp"
        assert captured["worker_id"] == "edge_app_builder_myapp"

    def test_malformed_worker_id_maps_to_400(self, monkeypatch) -> None:
        class _FakeRegistry:
            def register_device(self, **kwargs):
                raise ValueError(f"Invalid worker_id: {kwargs['worker_id']!r}.")

        client = _register_app(monkeypatch, _FakeRegistry)
        r = client.post(
            "/api/v1/devices/register",
            json={"device_name": "builder", "worker_id": "not-an-edge-id"},
        )
        assert r.status_code == 400
        assert "Invalid worker_id" in r.json()["detail"]

    def test_claimed_worker_id_maps_to_409(self, monkeypatch) -> None:
        class _FakeRegistry:
            def register_device(self, **kwargs):
                raise ValueError(
                    f"worker_id already in use: {kwargs['worker_id']!r}."
                )

        client = _register_app(monkeypatch, _FakeRegistry)
        r = client.post(
            "/api/v1/devices/register",
            json={"device_name": "builder", "worker_id": "edge_app_builder_myapp"},
        )
        assert r.status_code == 409
        assert "already in use" in r.json()["detail"]

    def test_omitted_worker_id_defaults_to_none(self, monkeypatch) -> None:
        captured: dict = {}

        class _FakeRegistry:
            def register_device(self, **kwargs):
                captured.update(kwargs)
                return ("dev-1", "edge_ab12cd34", "ld_secret")

        client = _register_app(monkeypatch, _FakeRegistry)
        r = client.post("/api/v1/devices/register", json={"device_name": "laptop"})
        assert r.status_code == 201
        assert r.json()["worker_id"] == "edge_ab12cd34"
        assert captured["worker_id"] is None


class _FakeRedis:
    """Minimal in-memory Redis for EdgeDeviceRegistry tests."""

    def __init__(self) -> None:
        self.kv: dict = {}
        self.hashes: dict = {}
        self.sets: dict = {}

    def set(self, key, value, nx=False):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    def get(self, key):
        return self.kv.get(key)

    def exists(self, key):
        return 1 if key in self.kv or key in self.hashes or key in self.sets else 0

    def scan(self, cursor, match=None, count=50):
        import fnmatch

        keys = list(self.kv.keys()) + list(self.hashes.keys()) + list(self.sets.keys())
        found = [k for k in keys if not match or fnmatch.fnmatch(k, match)]
        return 0, found

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, field=None, value=None, mapping=None):
        h = self.hashes.setdefault(key, {})
        if mapping:
            h.update(mapping)
        if field is not None:
            h[field] = value

    def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)

    def srem(self, key, member):
        self.sets.get(key, set()).discard(member)

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def delete(self, key):
        self.kv.pop(key, None)

    def pipeline(self, transaction=False):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, r: _FakeRedis) -> None:
        self._r = r
        self._ops: list = []

    def __getattr__(self, name):
        def _queue(*args, **kwargs):
            self._ops.append((name, args, kwargs))
            return self

        return _queue

    def execute(self):
        for name, args, kwargs in self._ops:
            getattr(self._r, name)(*args, **kwargs)
        self._ops = []


class TestRegistryWorkerIdClaim:
    """EdgeDeviceRegistry explicit worker-id uniqueness (ADR-0123 Phase D)."""

    @pytest.fixture
    def registry(self, monkeypatch):
        from motet.core.edge import device_registry as dr

        fake = _FakeRedis()
        monkeypatch.setattr(dr, "get_sync_redis_client", lambda client_id: fake)
        return dr.EdgeDeviceRegistry(), fake

    def test_explicit_id_registered_and_claim_enforced(self, registry) -> None:
        reg, fake = registry
        device_id, worker_id, token = reg.register_device(
            principal_id="app-builder/myapp",
            tenant_id="tenant_1",
            device_name="builder-remote",
            worker_id="edge_app_builder_myapp",
        )
        assert worker_id == "edge_app_builder_myapp"
        assert token.startswith("ld_")
        assert "tenant_1:edge_device:worker:edge_app_builder_myapp" in fake.kv

        # Second registration under the same worker id is rejected.
        with pytest.raises(ValueError, match="already in use"):
            reg.register_device(
                principal_id="app-builder/other",
                tenant_id="tenant_1",
                device_name="impostor",
                worker_id="edge_app_builder_myapp",
            )

    def test_revoke_releases_claim_for_reregistration(self, registry) -> None:
        reg, fake = registry
        device_id, worker_id, _ = reg.register_device(
            principal_id="app-builder/myapp",
            tenant_id="tenant_1",
            device_name="builder-remote",
            worker_id="edge_app_builder_myapp",
        )
        assert reg.revoke_device("app-builder/myapp", device_id) is True
        assert "tenant_1:edge_device:worker:edge_app_builder_myapp" not in fake.kv

        # The id is claimable again after revocation.
        _, worker_id2, _ = reg.register_device(
            principal_id="app-builder/myapp",
            tenant_id="tenant_1",
            device_name="builder-remote-v2",
            worker_id="edge_app_builder_myapp",
        )
        assert worker_id2 == "edge_app_builder_myapp"

    def test_verify_token_uses_locator_not_scan(self, registry) -> None:
        reg, fake = registry

        def _fail_scan(*_args, **_kwargs):
            raise AssertionError("device verify must not SCAN")

        fake.scan = _fail_scan
        _device_id, worker_id, token = reg.register_device(
            principal_id="user_1",
            tenant_id="tenant_1",
            device_name="laptop",
        )
        session = reg.verify_token(token)
        assert session is not None
        assert session.worker_id == worker_id
        assert session.tenant_id == "tenant_1"

    def test_derived_id_unchanged_without_explicit_worker_id(self, registry) -> None:
        reg, fake = registry
        device_id, worker_id, _ = reg.register_device(
            principal_id="user_1",
            tenant_id="tenant_1",
            device_name="laptop",
        )
        assert worker_id == f"edge_{device_id.replace('-', '')[:8]}"
        # Derived ids do not create worker claim keys.
        assert not any("edge_device:worker:" in k for k in fake.kv)


class _FakeReadiness:
    """In-memory WorkerReadinessService stand-in for deregister tests."""

    def __init__(self, workers: Optional[Dict[str, Any]] = None) -> None:
        self.workers: Dict[str, Any] = dict(workers or {})
        self.removed: list[str] = []

    def get_worker_info(self, worker_id: str):
        return self.workers.get(worker_id)

    def remove_worker(self, worker_id: str) -> None:
        self.removed.append(worker_id)
        self.workers.pop(worker_id, None)


class TestDeregisterEdgeWorker:
    """POST /api/v1/devices/workers/{worker_id}/deregister."""

    def test_device_token_removes_readiness(self, monkeypatch) -> None:
        import motet.interfaces.api.v1.devices as devices_api

        readiness = _FakeReadiness(
            {
                "edge_ab12cd34": SimpleNamespace(
                    worker_id="edge_ab12cd34",
                    owner_principal_id="user_1",
                )
            }
        )

        class _FakeRegistry:
            def verify_token(self, token: str):
                assert token == "ld_test_token"
                return DeviceAuthSession(
                    principal_id="user_1",
                    tenant_id="tenant_1",
                    device_id="d1000001-0000-0000-0000-000000000000",
                    worker_id="edge_ab12cd34",
                )

            def list_devices(self, _principal_id: str, tenant_id=None):
                return []

        monkeypatch.setattr(devices_api, "EdgeDeviceRegistry", _FakeRegistry)
        monkeypatch.setattr(
            "motet.core.distributed.worker_readiness.WorkerReadinessService",
            lambda: readiness,
        )

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        r = client.post(
            "/api/v1/devices/workers/edge_ab12cd34/deregister",
            headers={"Authorization": "Bearer ld_test_token"},
        )
        assert r.status_code == 200
        assert r.json() == {"worker_id": "edge_ab12cd34", "removed": True}
        assert readiness.removed == ["edge_ab12cd34"]

    def test_owner_principal_can_deregister(self, monkeypatch) -> None:
        import motet.interfaces.api.v1.devices as devices_api

        readiness = _FakeReadiness(
            {
                "edge_ab12cd34": SimpleNamespace(
                    worker_id="edge_ab12cd34",
                    owner_principal_id="user_1",
                )
            }
        )

        class _FakeRegistry:
            def list_devices(self, principal_id: str, tenant_id=None):
                return [
                    _Device(
                        device_id="d1000001-0000-0000-0000-000000000000",
                        worker_id="edge_ab12cd34",
                        device_name="mbp",
                        principal_id=principal_id,
                        tenant_id="tenant_1",
                        created_at="2026-03-26T00:00:00Z",
                        revoked=False,
                    )
                ]

            def verify_token(self, _token: str):
                return None

        monkeypatch.setattr(devices_api, "EdgeDeviceRegistry", _FakeRegistry)
        monkeypatch.setattr(
            "motet.core.distributed.worker_readiness.WorkerReadinessService",
            lambda: readiness,
        )

        from motet.interfaces.api.v1.devices import _get_device_or_user_principal

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[_get_device_or_user_principal] = lambda: Principal(
            id="user_1",
            tenant_id="tenant_1",
            roles=["admin"],
        )
        client = TestClient(app)

        r = client.post("/api/v1/devices/workers/edge_ab12cd34/deregister")
        assert r.status_code == 200
        assert r.json()["removed"] is True
        assert readiness.removed == ["edge_ab12cd34"]

    def test_mismatched_device_token_forbidden(self, monkeypatch) -> None:
        import motet.interfaces.api.v1.devices as devices_api

        class _FakeRegistry:
            def verify_token(self, _token: str):
                return DeviceAuthSession(
                    principal_id="user_1",
                    tenant_id="tenant_1",
                    device_id="d1000001-0000-0000-0000-000000000000",
                    worker_id="edge_other",
                )

            def list_devices(self, _principal_id: str, tenant_id=None):
                return []

        monkeypatch.setattr(devices_api, "EdgeDeviceRegistry", _FakeRegistry)
        monkeypatch.setattr(
            "motet.core.distributed.worker_readiness.WorkerReadinessService",
            lambda: _FakeReadiness(),
        )

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        r = client.post(
            "/api/v1/devices/workers/edge_ab12cd34/deregister",
            headers={"Authorization": "Bearer ld_wrong"},
        )
        assert r.status_code == 403

    def test_non_edge_worker_id_rejected(self, monkeypatch) -> None:
        import motet.interfaces.api.v1.devices as devices_api

        class _FakeRegistry:
            def verify_token(self, _token: str):
                return DeviceAuthSession(
                    principal_id="user_1",
                    tenant_id="tenant_1",
                    device_id="d1",
                    worker_id="edge_ab12cd34",
                )

        monkeypatch.setattr(devices_api, "EdgeDeviceRegistry", _FakeRegistry)

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        r = client.post(
            "/api/v1/devices/workers/cloud_worker1/deregister",
            headers={"Authorization": "Bearer ld_test"},
        )
        assert r.status_code == 400

    def test_legacy_cloud_edge_prefix_normalized(self, monkeypatch) -> None:
        import motet.interfaces.api.v1.devices as devices_api

        readiness = _FakeReadiness(
            {
                "edge_ab12cd34": SimpleNamespace(
                    worker_id="edge_ab12cd34",
                    owner_principal_id="user_1",
                )
            }
        )

        class _FakeRegistry:
            def verify_token(self, _token: str):
                return DeviceAuthSession(
                    principal_id="user_1",
                    tenant_id="tenant_1",
                    device_id="d1",
                    worker_id="edge_ab12cd34",
                )

            def list_devices(self, _principal_id: str, tenant_id=None):
                return []

        monkeypatch.setattr(devices_api, "EdgeDeviceRegistry", _FakeRegistry)
        monkeypatch.setattr(
            "motet.core.distributed.worker_readiness.WorkerReadinessService",
            lambda: readiness,
        )

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        r = client.post(
            "/api/v1/devices/workers/cloud_edge_ab12cd34/deregister",
            headers={"Authorization": "Bearer ld_test"},
        )
        assert r.status_code == 200
        assert r.json() == {"worker_id": "edge_ab12cd34", "removed": True}

    def test_idempotent_when_absent(self, monkeypatch) -> None:
        import motet.interfaces.api.v1.devices as devices_api

        readiness = _FakeReadiness()

        class _FakeRegistry:
            def verify_token(self, _token: str):
                return DeviceAuthSession(
                    principal_id="user_1",
                    tenant_id="tenant_1",
                    device_id="d1",
                    worker_id="edge_ab12cd34",
                )

            def list_devices(self, _principal_id: str, tenant_id=None):
                return []

        monkeypatch.setattr(devices_api, "EdgeDeviceRegistry", _FakeRegistry)
        monkeypatch.setattr(
            "motet.core.distributed.worker_readiness.WorkerReadinessService",
            lambda: readiness,
        )

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        r = client.post(
            "/api/v1/devices/workers/edge_ab12cd34/deregister",
            headers={"Authorization": "Bearer ld_test"},
        )
        assert r.status_code == 200
        assert r.json() == {"worker_id": "edge_ab12cd34", "removed": False}
        assert readiness.removed == []

