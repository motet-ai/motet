"""
Motet - Tasks CLI Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-12

Description:
    Unit tests for ``motet-cli tasks`` live/list/get/cancel commands.

Dependencies:
    - pytest: Test framework
    - click.testing: CliRunner
    - motet.cli.tasks: tasks_group (SDK re-export)
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from click.testing import CliRunner

from motet.cli.tasks import tasks_group

API = "http://localhost:8000"


class _Resp:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> Dict[str, Any]:
        return self._payload


def _patch_api(
    monkeypatch: pytest.MonkeyPatch,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    captured: Dict[str, Any] = {}

    def _fake_api_request(*args: Any, **kwargs: Any) -> _Resp:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _Resp(payload)

    monkeypatch.setattr(
        "motet_sdk.cli.tasks.get_api_headers",
        lambda: {"Authorization": "Bearer t"},
    )
    monkeypatch.setattr("motet_sdk.cli.tasks.api_request", _fake_api_request)
    return captured


def _invoke(args: List[str]) -> Any:
    return CliRunner().invoke(tasks_group, args)


def test_tasks_help_lists_commands() -> None:
    result = _invoke(["--help"])
    assert result.exit_code == 0
    for name in ("live", "list", "get", "cancel"):
        assert name in result.output


def test_tasks_live_include_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_api(monkeypatch, {"count": 0, "tasks": []})
    result = _invoke(["live", "--include-cancelled", "--api-url", API])
    assert result.exit_code == 0, result.output
    assert captured["args"][:2] == ("GET", f"{API}/api/v1/tasks/live")
    assert captured["kwargs"]["params"] == {"include_cancelled": True}


def test_tasks_live(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_api(
        monkeypatch,
        {
            "count": 1,
            "tasks": [
                {
                    "task_id": "task-1",
                    "status": "running",
                    "command_type": "core.agent_turn",
                    "conversation_id": "conv-1",
                }
            ],
        },
    )
    result = _invoke(
        ["live", "--conversation-id", "conv-1", "--api-url", API]
    )
    assert result.exit_code == 0, result.output
    assert captured["args"][:2] == ("GET", f"{API}/api/v1/tasks/live")
    assert captured["kwargs"]["params"] == {"conversation_id": "conv-1"}
    assert "task-1" in result.output
    assert "core.agent_turn" in result.output


def test_tasks_list_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_api(monkeypatch, {"count": 0, "tasks": []})
    result = _invoke(["list", "--json-output", "--api-url", API])
    assert result.exit_code == 0, result.output
    assert captured["args"][:2] == ("GET", f"{API}/api/v1/tasks/live")
    assert '"count": 0' in result.output


def test_tasks_get(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_api(
        monkeypatch,
        {"task_id": "task-abc", "status": "running"},
    )
    result = _invoke(["get", "task-abc", "--api-url", API])
    assert result.exit_code == 0, result.output
    assert captured["args"][:2] == ("GET", f"{API}/api/v1/tasks/task-abc")
    assert "task-abc" in result.output


def test_tasks_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_api(
        monkeypatch,
        {
            "task_id": "task-abc",
            "status": "cancelled",
            "control": {"action": "cancel"},
        },
    )
    result = _invoke(
        ["cancel", "task-abc", "--reason", "user stop", "--api-url", API]
    )
    assert result.exit_code == 0, result.output
    assert captured["args"][:2] == (
        "POST",
        f"{API}/api/v1/tasks/task-abc/cancel",
    )
    assert captured["kwargs"]["json"] == {"reason": "user stop"}
    assert '"status": "cancelled"' in result.output


def test_tasks_cancel_without_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_api(
        monkeypatch,
        {"task_id": "task-xyz", "status": "cancelled", "control": {}},
    )
    result = _invoke(["cancel", "task-xyz", "--api-url", API])
    assert result.exit_code == 0, result.output
    assert captured["kwargs"]["json"] == {}
