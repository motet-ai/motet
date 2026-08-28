"""
Motet - CLI API request helper tests.

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-27

Description:
Unit tests for the shared CLI request wrapper used by API-backed motet-cli
commands.

Dependencies:
- pytest: Test framework
- requests: Response model used by the helper

Usage:
pytest tests/unit/cli/test_api_request.py

Notes:
- Multipart uploads must not carry a caller-supplied Content-Type header because
  requests generates the multipart boundary.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from motet_sdk.cli._api import api_request


def _response(status_code: int = 200) -> requests.Response:
    """Create a minimal requests response for api_request tests."""
    response = requests.Response()
    response.status_code = status_code
    response._content = b"{}"
    response.headers["content-type"] = "application/json"
    return response


def test_api_request_removes_content_type_for_multipart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multipart uploads let requests set Content-Type with the boundary."""
    captured: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> requests.Response:
        captured.update({"method": method, "url": url, **kwargs})
        return _response()

    monkeypatch.setattr("requests.request", fake_request)

    api_request(
        "POST",
        "https://example.test/api/v1/deploy/upload",
        headers={
            "Authorization": "Bearer token",
            "Content-Type": "application/json",
        },
        files={"bundle": ("bundle.zip", b"zip", "application/zip")},
    )

    assert captured["headers"] == {"Authorization": "Bearer token"}


def test_api_request_preserves_content_type_for_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON requests still use application/json headers."""
    captured: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> requests.Response:
        captured.update({"method": method, "url": url, **kwargs})
        return _response()

    monkeypatch.setattr("requests.request", fake_request)

    api_request(
        "POST",
        "https://example.test/api/v1/deploy",
        headers={"Authorization": "Bearer token"},
        json={"repo_url": "https://example.test/repo.git"},
    )

    assert captured["headers"] == {
        "Authorization": "Bearer token",
        "Content-Type": "application/json",
    }
