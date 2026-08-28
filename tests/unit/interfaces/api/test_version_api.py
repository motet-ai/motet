"""
Motet - Stack Version API Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Unit tests for GET /api/v1/version: payload shape, skew detection,
    missing worker versions, sibling health URL resolution, sibling probes,
    and authentication.

Dependencies:
    - pytest: Test framework
    - fastapi.testclient: In-process HTTP
    - motet.interfaces.api.v1.version: helpers and router
    - motet.core.distributed.worker_readiness: WorkerInfo / WorkerState

Usage:
    pytest tests/unit/interfaces/api/test_version_api.py -q
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from motet._version import get_version
from motet.core.distributed.worker_readiness import WorkerInfo, WorkerState
from motet.core.types import Principal
from motet.interfaces.api.shared.auth import get_current_principal
from motet.interfaces.api.v1.version import (
    SiblingVersionEntry,
    build_stack_version,
    configured_sibling_targets,
    normalize_motet_version,
    probe_sibling,
    router,
    sibling_health_url,
    stack_has_skew,
)

USER = Principal(id="user-1", roles=["user"], tenant_id="acme")


def _worker(
    worker_id: str,
    *,
    motet_version: str | None = "0.1.0",
    state: WorkerState = WorkerState.READY,
) -> WorkerInfo:
    return WorkerInfo(
        worker_id=worker_id,
        state=state,
        capabilities=["tool_execution"],
        last_heartbeat=1.0,
        warmup_completed=True,
        motet_version=motet_version,
    )


def test_normalize_motet_version_treats_placeholders_as_missing() -> None:
    assert normalize_motet_version(None) is None
    assert normalize_motet_version("") is None
    assert normalize_motet_version("None") is None
    assert normalize_motet_version(" 0.1.0 ") == "0.1.0"


def test_stack_has_skew_when_worker_missing_or_mismatched() -> None:
    assert stack_has_skew("0.1.0", []) is False
    assert stack_has_skew("0.1.0", ["0.1.0"]) is False
    assert stack_has_skew("0.1.0", ["0.1.0", None]) is True
    assert stack_has_skew("0.1.0", ["0.2.0"]) is True


def test_build_stack_version_sorts_workers_and_sets_skew() -> None:
    workers = {
        "worker-b": _worker("worker-b", motet_version="0.2.0"),
        "worker-a": _worker("worker-a", motet_version="0.1.0"),
    }
    payload = build_stack_version("0.1.0", workers)
    assert payload.api == "0.1.0"
    assert [entry.worker_id for entry in payload.workers] == ["worker-a", "worker-b"]
    assert payload.skew is True


def test_build_stack_version_no_workers_is_not_skew() -> None:
    payload = build_stack_version("0.1.0", {})
    assert payload.workers == []
    assert payload.siblings == []
    assert payload.skew is False


def test_build_stack_version_sibling_unreachable_is_skew() -> None:
    payload = build_stack_version(
        "0.1.0",
        {},
        [
            SiblingVersionEntry(
                id="embedding-server",
                motet_version=None,
                reachable=False,
            )
        ],
    )
    assert payload.skew is True
    assert payload.siblings[0].reachable is False


def test_build_stack_version_sibling_mismatch_is_skew() -> None:
    payload = build_stack_version(
        "0.1.0",
        {},
        [
            SiblingVersionEntry(
                id="mcp-manager",
                motet_version="0.2.0",
                reachable=True,
            )
        ],
    )
    assert payload.skew is True


def test_build_stack_version_matching_siblings_is_not_skew() -> None:
    payload = build_stack_version(
        "0.1.0",
        {},
        [
            SiblingVersionEntry(
                id="embedding-server",
                motet_version="0.1.0",
                reachable=True,
            ),
            SiblingVersionEntry(
                id="mcp-manager",
                motet_version="0.1.0",
                reachable=True,
            ),
        ],
    )
    assert payload.skew is False
    assert [entry.id for entry in payload.siblings] == ["embedding-server", "mcp-manager"]


@pytest.mark.parametrize(
    ("sibling_id", "endpoint", "expected"),
    [
        ("embedding-server", None, None),
        ("embedding-server", "", None),
        ("embedding-server", "http://embedding-server:8091", "http://embedding-server:8091/healthz"),
        (
            "embedding-server",
            "http://embedding-server:8091/healthz",
            "http://embedding-server:8091/healthz",
        ),
        ("embedding-server", "embedding-server:8091", "http://embedding-server:8091/healthz"),
        ("mcp-manager", None, None),
        ("mcp-manager", "mcp-manager", "http://mcp-manager:9091/health"),
        ("mcp-manager", "mcp-manager:9091", "http://mcp-manager:9091/health"),
        ("mcp-manager", "http://mcp-manager:9091", "http://mcp-manager:9091/health"),
        ("mcp-manager", "http://mcp-manager:9091/health", "http://mcp-manager:9091/health"),
        ("unknown", "http://example", None),
    ],
)
def test_sibling_health_url(sibling_id: str, endpoint: str | None, expected: str | None) -> None:
    assert sibling_health_url(sibling_id, endpoint) == expected


def test_configured_sibling_targets_omits_unset() -> None:
    cfg = SimpleNamespace(embedding_endpoint=None, mcp_manager_endpoint=None)
    assert configured_sibling_targets(cfg) == []


def test_configured_sibling_targets_includes_both() -> None:
    cfg = SimpleNamespace(
        embedding_endpoint="http://embedding-server:8091",
        mcp_manager_endpoint="mcp-manager",
    )
    assert configured_sibling_targets(cfg) == [
        ("embedding-server", "http://embedding-server:8091/healthz"),
        ("mcp-manager", "http://mcp-manager:9091/health"),
    ]


@pytest.mark.asyncio
async def test_probe_sibling_extracts_version() -> None:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"motet_version": "0.1.0", "ready": True}

    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False

    with patch("motet.interfaces.api.v1.version.httpx.AsyncClient", return_value=client):
        entry = await probe_sibling("embedding-server", "http://embedding-server:8091/healthz")

    assert entry.reachable is True
    assert entry.motet_version == "0.1.0"


@pytest.mark.asyncio
async def test_probe_sibling_unreachable() -> None:
    client = AsyncMock()
    client.get = AsyncMock(side_effect=ConnectionError("refused"))
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False

    with patch("motet.interfaces.api.v1.version.httpx.AsyncClient", return_value=client):
        entry = await probe_sibling("mcp-manager", "http://mcp-manager:9091/health")

    assert entry.reachable is False
    assert entry.motet_version is None


def test_worker_info_from_dict_without_motet_version_is_none() -> None:
    info = WorkerInfo.from_dict(
        {
            "worker_id": "worker-1",
            "state": "ready",
            "capabilities": [],
            "last_heartbeat": 1.0,
            "warmup_completed": True,
        }
    )
    assert info.motet_version is None


def test_unauthenticated_version_is_401() -> None:
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/api/v1/version")
    assert response.status_code == 401


def test_authenticated_version_returns_api_and_workers() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_principal] = lambda: USER

    readiness = MagicMock()
    readiness.get_all_workers.return_value = {
        "worker-1": _worker("worker-1", motet_version=get_version()),
    }
    with patch(
        "motet.core.distributed.worker_readiness.get_readiness_service",
        return_value=readiness,
    ), patch(
        "motet.interfaces.api.v1.version.configured_sibling_targets",
        return_value=[],
    ):
        response = TestClient(app).get("/api/v1/version")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["api"] == get_version()
    assert body["skew"] is False
    assert body["siblings"] == []
    assert body["workers"] == [
        {
            "worker_id": "worker-1",
            "motet_version": get_version(),
            "state": "ready",
        }
    ]
