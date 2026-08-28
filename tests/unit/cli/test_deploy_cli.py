"""
Motet - Deploy CLI Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-06

Description:
    Unit tests for destructive `motet-cli deploy` commands (rollback, undeploy)
    confirmation prompts.

Dependencies:
    - pytest: Test framework
    - click.testing: CliRunner
    - motet.cli.deploy: deploy_group

Usage:
    pytest tests/unit/cli/test_deploy_cli.py
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from click.testing import CliRunner

from motet.cli.deploy import deploy_group


class _Resp:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> Dict[str, Any]:
        return self._payload


def _capture_api(monkeypatch: pytest.MonkeyPatch, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []

    def fake_api_request(method: str, url: str, **kwargs: Any) -> _Resp:
        calls.append({"method": method, "url": url, **kwargs})
        return _Resp(payload)

    monkeypatch.setattr("motet_sdk.cli.deploy.get_api_headers", lambda: {"Authorization": "Bearer t"})
    monkeypatch.setattr("motet_sdk.cli.deploy.api_request", fake_api_request)
    return calls


def test_deploy_undeploy_aborts_without_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_api(monkeypatch, {})
    runner = CliRunner()

    result = runner.invoke(
        deploy_group,
        ["undeploy", "my-bundle", "--api-url", "http://localhost:8000"],
        input="n\n",
    )

    assert result.exit_code != 0
    assert "Undeploy this bundle from all workers?" in result.output
    assert calls == []


def test_deploy_undeploy_with_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_api(monkeypatch, {"deploy_job_id": "job-1"})
    runner = CliRunner()

    result = runner.invoke(
        deploy_group,
        ["undeploy", "my-bundle", "--yes", "--api-url", "http://localhost:8000"],
    )

    assert result.exit_code == 0, result.output
    assert "Undeploy job accepted" in result.output
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["url"] == "http://localhost:8000/api/v1/deploy/my-bundle"


def test_deploy_rollback_aborts_without_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_api(monkeypatch, {})
    runner = CliRunner()

    result = runner.invoke(
        deploy_group,
        [
            "rollback",
            "my-bundle",
            "--version",
            "abc123",
            "--api-url",
            "http://localhost:8000",
        ],
        input="n\n",
    )

    assert result.exit_code != 0
    assert "Rollback this bundle to the specified version?" in result.output
    assert calls == []


def test_deploy_rollback_with_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_api(
        monkeypatch,
        {
            "bundle_id": "my-bundle",
            "bundle_version": "abc123",
            "deploy_job_id": "job-2",
        },
    )
    runner = CliRunner()

    result = runner.invoke(
        deploy_group,
        [
            "rollback",
            "my-bundle",
            "--version",
            "abc123",
            "--yes",
            "--api-url",
            "http://localhost:8000",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Rollback job accepted" in result.output
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "http://localhost:8000/api/v1/deploy/my-bundle/rollback"
    assert calls[0]["json"] == {"bundle_version": "abc123"}
