"""
Motet - Deploy API Catalog Exec Block Exposure Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

ADR-0100: pin that the Deploy API surfaces the bundle ``exec`` catalog block.

The bundle catalog (``bundle:{bundle_id}:catalog``) carries an ``exec`` sub-dict
populated at validate/publish from ``config/exec.yaml`` — ``oci_image_ref``,
``exec_artifact_digest``, ``base_image_stack``, ``requirements_path``, and the
computed ``requirements_sha256`` (see
``tests/unit/core/orchestration/test_bundle_exec_phase3.py``). Without these in
the API response, the ops dashboard cannot show operators which image / digest /
stack a deployed bundle will actually pull at run time.

These tests pin the API ↔ FE contract: ``GET /api/v1/deploy`` and
``GET /api/v1/deploy/{bundle_id}/status`` MUST include ``catalog.exec`` with
the keys produced by ``_extract_bundle_catalog``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from motet.core.types import Principal
from motet.interfaces.api.shared.auth import get_current_principal
from motet.interfaces.http import app

client = TestClient(app)

EXEC_BLOCK: Dict[str, str] = {
    "oci_image_ref": "registry.example.com/acme/demo@sha256:" + "a" * 64,
    "exec_artifact_digest": "sha256:" + "b" * 64,
    "base_image_stack": "python-office",
    "requirements_path": "exec/requirements.txt",
    "requirements_sha256": "c" * 64,
}

CATALOG: Dict[str, Any] = {
    "bundle_id": "acme.demo",
    "bundle_version": "deadbeef",
    "commands": ["acme.demo.run"],
    "tools": [],
    "workflows": [],
    "agents": [],
    "agent_configs": {},
    "mcp_servers": [],
    "model_ids": [],
    "skills": [],
    "exec": EXEC_BLOCK,
}

BUNDLES: List[Dict[str, Any]] = [
    {
        "bundle_id": "acme.demo",
        "bundle_version": "deadbeef",
        "bundle_ref": "1234567890abcdef",
        "manifest_version": "1.0.0",
        "status": "complete",
        "deployed_at": "1700000000",
        "targeting": {},
        "deploy_job_id": "job-123",
    }
]


@pytest.fixture(autouse=True)
def override_principal_dependency() -> Iterator[None]:
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        id="test-user",
        roles=["admin", "ops", "user"],
        tenant_id="test-tenant",
        motet_id="test-motet",
    )
    yield
    app.dependency_overrides.pop(get_current_principal, None)


def _patch_redis_helpers():
    """Patch the catalog/worker_state/redis lookups used by both endpoints.

    The endpoints look these up via local imports inside the handler bodies,
    so we patch the source modules rather than the deploy module.
    """
    fake_redis = object()
    return [
        patch(
            "motet.core.distributed.redis_manager.get_sync_redis_client",
            return_value=fake_redis,
        ),
        patch(
            "motet.core.bundles.deploy._list_all_bundles",
            return_value=list(BUNDLES),
        ),
        patch(
            "motet.core.bundles.deploy._get_catalog",
            return_value=dict(CATALOG),
        ),
        patch(
            "motet.core.bundles.deploy._get_worker_state",
            return_value={},
        ),
        patch(
            "motet.core.bundles.deploy._get_registry_entry",
            return_value=dict(BUNDLES[0]),
        ),
    ]


def test_list_bundles_includes_exec_block() -> None:
    """ADR-0100 §"Catalog shape" — list_bundles must surface the exec block."""
    with (
        _patch_redis_helpers()[0],
        _patch_redis_helpers()[1],
        _patch_redis_helpers()[2],
        _patch_redis_helpers()[3],
    ):
        response = client.get("/api/v1/deploy")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1, body
    catalog = body["bundles"][0]["catalog"]
    assert "exec" in catalog, (
        "list_bundles must include catalog.exec — the FE has no other route to "
        "discover the pinned image/digest a deployed bundle will pull. "
        f"Got keys: {sorted(catalog.keys())}"
    )
    assert catalog["exec"] == EXEC_BLOCK, (
        "exec block must round-trip verbatim from the catalog (no field stripping). "
        f"Expected {EXEC_BLOCK}, got {catalog['exec']}"
    )


def test_list_bundles_exec_defaults_to_empty_dict_when_unpinned() -> None:
    """Bundles without config/exec.yaml MUST return an empty dict, not null/missing.

    The FE relies on a stable dict shape so ``catalog.exec.oci_image_ref ?? "—"``
    works without a null-check on the parent. Returning ``None`` or omitting the
    key would break the rendering contract.
    """
    catalog_no_exec = {k: v for k, v in CATALOG.items() if k != "exec"}
    with (
        patch(
            "motet.core.distributed.redis_manager.get_sync_redis_client",
            return_value=object(),
        ),
        patch(
            "motet.core.bundles.deploy._list_all_bundles",
            return_value=list(BUNDLES),
        ),
        patch(
            "motet.core.bundles.deploy._get_catalog",
            return_value=catalog_no_exec,
        ),
        patch(
            "motet.core.bundles.deploy._get_worker_state",
            return_value={},
        ),
    ):
        response = client.get("/api/v1/deploy")

    assert response.status_code == 200, response.text
    catalog = response.json()["bundles"][0]["catalog"]
    assert catalog["exec"] == {}, (
        "Unpinned bundles MUST return catalog.exec == {} (not None, not missing) "
        f"to keep the FE shape stable. Got: {catalog.get('exec')!r}"
    )


def test_get_deploy_status_includes_exec_block() -> None:
    """The single-bundle status endpoint must mirror list_bundles' exec exposure."""
    with (
        patch(
            "motet.core.distributed.redis_manager.get_sync_redis_client",
            return_value=object(),
        ),
        patch(
            "motet.core.bundles.deploy._get_registry_entry",
            return_value=dict(BUNDLES[0]),
        ),
        patch(
            "motet.core.bundles.deploy._get_catalog",
            return_value=dict(CATALOG),
        ),
        patch(
            "motet.core.bundles.deploy._get_worker_state",
            return_value={},
        ),
    ):
        response = client.get("/api/v1/deploy/acme.demo/status")

    assert response.status_code == 200, response.text
    catalog = response.json()["catalog"]
    assert "exec" in catalog, (
        "get_deploy_status must include catalog.exec for parity with list_bundles."
    )
    assert catalog["exec"] == EXEC_BLOCK, catalog["exec"]
