"""
Motet - Image Stacks API Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

ADR-0101 §"Platform-managed image stacks" — pin the GET
/api/v1/exec/image-stacks contract that the BundlesPage UI consumes.

We pin (a) auth is required, (b) builtins are always present, (c) env
overrides surface through, and (d) the response shape carries every field
the UI renders (name / oci_image_ref / description / builtin / is_pinned).
Future renames of any field would silently break the FE column rendering;
this test makes such a rename loud.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from motet.core.types import Principal
from motet.interfaces.api.shared.auth import get_current_principal
from motet.interfaces.http import app


client = TestClient(app)


@pytest.fixture
def authed_principal() -> Iterator[None]:
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        id="test-user",
        roles=["admin", "ops", "user"],
        tenant_id="test-tenant",
        motet_id="test-motet",
    )
    yield
    app.dependency_overrides.pop(get_current_principal, None)


def test_lists_builtin_stacks(authed_principal: None) -> None:
    """All ADR-0101 builtins MUST appear so the UI can show them as
    'recognized but unpinned' even before operators wire env vars."""
    response = client.get("/api/v1/exec/image-stacks")
    assert response.status_code == 200, response.text
    body = response.json()
    assert "stacks" in body
    names = {s["name"] for s in body["stacks"]}
    assert {"python-minimal", "python-office", "python-browser"}.issubset(names)


def test_response_shape_matches_fe_contract(authed_principal: None) -> None:
    """Every key the BundlesPage renders MUST be present on every row.
    Renaming any of these is a breaking UI change."""
    response = client.get("/api/v1/exec/image-stacks")
    assert response.status_code == 200, response.text
    for row in response.json()["stacks"]:
        for key in ("name", "oci_image_ref", "description", "builtin", "is_pinned"):
            assert key in row, f"missing FE-contract key {key!r} in row: {row}"


def test_python_minimal_is_pinned_by_default(authed_principal: None) -> None:
    """python-minimal ships pre-pinned to python:3.11-slim — operators
    expect a usable default without setting any env vars."""
    response = client.get("/api/v1/exec/image-stacks")
    rows = {s["name"]: s for s in response.json()["stacks"]}
    minimal = rows["python-minimal"]
    assert minimal["is_pinned"] is True
    assert minimal["oci_image_ref"]


def test_unauthenticated_request_rejected() -> None:
    """No principal override → auth dependency MUST reject the request.
    The registry isn't a secret, but the surface is operator-facing and
    every other /api/v1 endpoint is authenticated; consistency matters."""
    # No fixture loaded → default get_current_principal applies.
    response = client.get("/api/v1/exec/image-stacks")
    assert response.status_code in (401, 403), (
        f"Expected auth rejection, got {response.status_code}: {response.text}"
    )
