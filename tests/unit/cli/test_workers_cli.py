"""
Motet - Workers CLI Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-06

Description:
Unit tests for `motet-cli workers` operator commands.

Dependencies:
- pytest: Test framework
- click.testing: CliRunner
- motet.cli.workers: workers_group
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from click.testing import CliRunner

from motet.cli.workers import workers_group


class _Resp:
    """Simple response stub with JSON payload."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> Dict[str, Any]:
        return self._payload


def test_workers_skill_workspaces_calls_workspace_containers_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """workers skill-workspaces prints the workspace container snapshot."""
    captured: Dict[str, Any] = {}
    payload = {
        "status": "success",
        "containers": [
            {
                "tenant_id": "default",
                "conversation_id": "conv-1",
                "bundle_id": "skills-vendor-demo",
                "skill_name": "pdf",
                "image_stack": "python-minimal",
                "container_id_short": "abc123",
            }
        ],
    }

    def _fake_api_request(*args: Any, **kwargs: Any) -> _Resp:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _Resp(payload)

    monkeypatch.setattr("motet_sdk.cli.workers.get_api_headers", lambda: {"Authorization": "Bearer t"})
    monkeypatch.setattr("motet_sdk.cli.workers.api_request", _fake_api_request)

    runner = CliRunner()
    result = runner.invoke(
        workers_group,
        [
            "skill-workspaces",
            "--tenant-id",
            "default",
            "--api-url",
            "http://localhost:8000",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["args"][:2] == ("GET", "http://localhost:8000/api/v1/workspace-containers")
    assert captured["kwargs"]["params"] == {"tenant_id": "default"}
    assert '"skill_name": "pdf"' in result.output
    assert '"container_id_short": "abc123"' in result.output


def test_workers_terminate_unhealthy_aborts_without_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """terminate-unhealthy aborts when confirmation is declined."""

    calls: list[Dict[str, Any]] = []

    def _fake_api_request(*args: Any, **kwargs: Any) -> _Resp:
        calls.append({"args": args, "kwargs": kwargs})
        return _Resp({"terminated": []})

    monkeypatch.setattr("motet_sdk.cli.workers.get_api_headers", lambda: {"Authorization": "Bearer t"})
    monkeypatch.setattr("motet_sdk.cli.workers.api_request", _fake_api_request)

    runner = CliRunner()
    result = runner.invoke(
        workers_group,
        ["terminate-unhealthy", "--api-url", "http://localhost:8000"],
        input="n\n",
    )

    assert result.exit_code != 0
    assert "Terminate ALL unhealthy workers?" in result.output
    assert calls == []


def test_workers_terminate_unhealthy_with_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    """terminate-unhealthy proceeds with --yes."""

    captured: Dict[str, Any] = {}

    def _fake_api_request(*args: Any, **kwargs: Any) -> _Resp:
        captured["args"] = args
        return _Resp({"terminated": ["worker-1"]})

    monkeypatch.setattr("motet_sdk.cli.workers.get_api_headers", lambda: {"Authorization": "Bearer t"})
    monkeypatch.setattr("motet_sdk.cli.workers.api_request", _fake_api_request)

    runner = CliRunner()
    result = runner.invoke(
        workers_group,
        ["terminate-unhealthy", "--yes", "--api-url", "http://localhost:8000"],
    )

    assert result.exit_code == 0, result.output
    assert captured["args"][:2] == (
        "POST",
        "http://localhost:8000/api/v1/workers/terminate-unhealthy",
    )
    assert '"terminated"' in result.output
