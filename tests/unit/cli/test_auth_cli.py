"""
Motet - Auth CLI Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-06

Description:
    Unit tests for `motet-cli auth` commands, especially logout which clears
    both the server refresh token and local credentials.

Dependencies:
    - pytest: Test framework
    - click.testing: CliRunner
    - motet.cli.auth: auth_group

Usage:
    pytest tests/unit/cli/test_auth_cli.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from motet.cli.auth import auth_group


class _Resp:
    def __init__(self, payload: Dict[str, Any] | None = None) -> None:
        self._payload = payload or {"status": "success"}

    def json(self) -> Dict[str, Any]:
        return self._payload


def test_auth_logout_calls_api_and_clears_local(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """logout hits GET /api/v1/auth/logout then clears local credentials."""

    creds = tmp_path / "credentials.json"
    creds.write_text('{"jwt_token": "tok"}')
    calls: List[Dict[str, Any]] = []

    def fake_api_request(method: str, url: str, **kwargs: Any) -> _Resp:
        calls.append({"method": method, "url": url, **kwargs})
        return _Resp()

    monkeypatch.setattr("motet_sdk.cli.auth.get_credentials_path", lambda: creds)
    monkeypatch.setattr("motet_sdk.cli.auth.get_stored_token", lambda: "tok")
    monkeypatch.setattr("motet_sdk.cli.auth.get_api_headers", lambda: {"Authorization": "Bearer tok"})
    monkeypatch.setattr("motet_sdk.cli.auth.api_request", fake_api_request)
    monkeypatch.setattr("motet_sdk.cli.auth.clear_credentials", lambda: creds.unlink())

    runner = CliRunner()
    result = runner.invoke(auth_group, ["logout", "--api-url", "http://localhost:8000"])

    assert result.exit_code == 0, result.output
    assert "Server session cleared." in result.output
    assert "Local credentials cleared." in result.output
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "http://localhost:8000/api/v1/auth/logout"
    assert calls[0]["retry_on_401_refresh"] is False
    assert not creds.exists()


def test_auth_logout_skips_api_without_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """logout without a token only reports that server logout was skipped."""

    creds = tmp_path / "credentials.json"
    monkeypatch.setattr("motet_sdk.cli.auth.get_credentials_path", lambda: creds)
    monkeypatch.setattr("motet_sdk.cli.auth.get_stored_token", lambda: None)
    monkeypatch.delenv("MOTET_JWT_TOKEN", raising=False)
    monkeypatch.delenv("MOTET_SERVICE_ACCOUNT_TOKEN", raising=False)

    def fail_api(*_args: Any, **_kwargs: Any) -> _Resp:
        raise AssertionError("api_request should not be called without a token")

    monkeypatch.setattr("motet_sdk.cli.auth.api_request", fail_api)

    runner = CliRunner()
    with patch.dict("os.environ", {}, clear=False):
        result = runner.invoke(auth_group, ["logout", "--api-url", "http://localhost:8000"])

    assert result.exit_code == 0, result.output
    assert "No token available; skipping server logout." in result.output
    assert "No stored credentials." in result.output


def test_auth_logout_still_clears_local_when_api_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """API failure should not block clearing local credentials."""

    import click

    creds = tmp_path / "credentials.json"
    creds.write_text('{"jwt_token": "tok"}')

    monkeypatch.setattr("motet_sdk.cli.auth.get_credentials_path", lambda: creds)
    monkeypatch.setattr("motet_sdk.cli.auth.get_stored_token", lambda: "tok")
    monkeypatch.setattr("motet_sdk.cli.auth.get_api_headers", lambda: {"Authorization": "Bearer tok"})

    def boom(*_args: Any, **_kwargs: Any) -> _Resp:
        raise click.ClickException("API error 500: boom")

    monkeypatch.setattr("motet_sdk.cli.auth.api_request", boom)
    monkeypatch.setattr("motet_sdk.cli.auth.clear_credentials", lambda: creds.unlink())

    runner = CliRunner()
    result = runner.invoke(auth_group, ["logout", "--api-url", "http://localhost:8000"])

    assert result.exit_code == 0, result.output
    assert "could not clear server session" in result.output
    assert "Local credentials cleared." in result.output
    assert not creds.exists()
