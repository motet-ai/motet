"""
Motet - Skills API Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
Unit tests for the top-level /api/v1/skills operator endpoint.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Dict
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from motet.core.types import Principal
from motet.interfaces.api.shared.auth import get_current_principal
from motet.interfaces.api.v1.skills import router


CATALOGS: Dict[str, Dict[str, Any]] = {
    "skills-vendor-demo": {
        "bundle_id": "skills-vendor-demo",
        "bundle_version": "deadbeef",
        "targeting": {"tenant_ids": ["default"], "motet_ids": []},
        "skills": [
            {
                "id": "skills-vendor-demo.pdf",
                "name": "pdf",
                "description": "Work with PDF files.",
                "path": "skills/pdf/",
                "dir": "pdf",
                "dir_matches_name": True,
            }
        ],
        "exec": {
            "base_image_stack": "python-office",
            "runtime_capabilities": ["python", "office", "pdf"],
            "requirements_path": "exec/requirements.txt",
            "oci_image_ref": "registry.example.com/motet/python-office@sha256:" + "a" * 64,
        },
    },
    "hidden-demo": {
        "bundle_id": "hidden-demo",
        "bundle_version": "cafebabe",
        "targeting": {"tenant_ids": ["other"], "motet_ids": []},
        "skills": [{"id": "hidden-demo.secret", "name": "secret"}],
        "exec": {},
    },
}


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        id="test-user",
        roles=["admin", "ops", "user"],
        tenant_id="default",
        motet_id="default",
    )
    yield TestClient(app)


def test_list_skills_flattens_bundle_catalogs(client: TestClient) -> None:
    """GET /api/v1/skills returns skill-shaped rows with runtime metadata."""
    with (
        patch("motet.core.distributed.redis_manager.get_sync_redis_client", return_value=object()),
        patch("motet.core.bundles.deploy._list_all_catalogs", return_value=CATALOGS),
    ):
        response = client.get("/api/v1/skills", params={"tenant_id": "default"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    row = body["skills"][0]
    assert row["skill_id"] == "skills-vendor-demo.pdf"
    assert row["bundle_id"] == "skills-vendor-demo"
    assert row["base_image_stack"] == "python-office"
    assert row["runtime_capabilities"] == ["python", "office", "pdf"]
    assert row["requirements_path"] == "exec/requirements.txt"
    assert row["execution_available"] is True


def test_list_skills_filters_by_bundle_id(client: TestClient) -> None:
    """The endpoint supports bundle_id filtering for CLI/UI drill-downs."""
    with (
        patch("motet.core.distributed.redis_manager.get_sync_redis_client", return_value=object()),
        patch("motet.core.bundles.deploy._list_all_catalogs", return_value=CATALOGS),
    ):
        response = client.get(
            "/api/v1/skills",
            params={"tenant_id": "default", "bundle_id": "missing-bundle"},
        )

    assert response.status_code == 200, response.text
    assert response.json() == {"skills": [], "total": 0}
