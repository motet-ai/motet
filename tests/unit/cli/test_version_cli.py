"""
Motet - Stack Version CLI Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Unit tests for ``motet-cli version`` (GET /api/v1/version). Distinct
    from ``motet-cli --version``, which prints the local package version.

Dependencies:
    - pytest: Test framework
    - click.testing: CliRunner
    - motet_sdk.cli.version: version_command

Usage:
    pytest tests/unit/cli/test_version_cli.py -q
"""

from __future__ import annotations

from typing import Any, Dict, List

from click.testing import CliRunner

from motet_sdk.cli.version import version_command


class _Resp:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def test_version_command_calls_stack_version_api(monkeypatch: Any) -> None:
    calls: List[Dict[str, Any]] = []

    def fake_api_request(method: str, url: str, **kwargs: Any) -> _Resp:
        calls.append({"method": method, "url": url, **kwargs})
        return _Resp({"api": "0.1.0", "workers": [], "skew": False})

    monkeypatch.setattr("motet_sdk.cli.version.api_request", fake_api_request)
    monkeypatch.setattr("motet_sdk.cli.version.get_api_headers", lambda: {"Authorization": "Bearer t"})

    result = CliRunner().invoke(version_command, ["--api-url", "http://localhost:8000"])
    assert result.exit_code == 0, result.output
    assert '"skew": false' in result.output
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "http://localhost:8000/api/v1/version"
