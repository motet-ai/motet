"""Tests for core.process_control and process control bridge client."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from motet.core.commands.capabilities import WorkerCapability
from motet.core.commands.builtin.tool import _infer_tool_capabilities
from motet.core.commands.command_data_classes import ToolExecutionData
from motet.core.tools.builtin.process_control import run as pc_run
from motet.core.tools.builtin.process_control_bridge_client import (
    list_processes_via_bridge,
    terminate_via_bridge,
)


def test_list_processes_via_bridge_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_PROCESS_CONTROL_BRIDGE_URL", "http://host.docker.internal:9999")
    monkeypatch.setenv("MOTET_PROCESS_CONTROL_BRIDGE_TOKEN", "tok")
    body = json.dumps({"processes": [{"pid": 1, "name": "python"}], "count": 1}).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    ctx = MagicMock()
    ctx.__enter__.return_value = mock_resp
    ctx.__exit__.return_value = None
    with patch(
        "motet.core.tools.builtin.process_control_bridge_client.urllib.request.urlopen",
        return_value=ctx,
    ):
        r = list_processes_via_bridge(10)
    assert r["count"] == 1
    assert r["processes"][0]["pid"] == 1


def test_terminate_via_bridge_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_PROCESS_CONTROL_BRIDGE_URL", "http://host.docker.internal:9999")
    monkeypatch.setenv("MOTET_PROCESS_CONTROL_BRIDGE_TOKEN", "tok")
    body = json.dumps({"ok": True, "pid": 42, "signal": "SIGTERM"}).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    ctx = MagicMock()
    ctx.__enter__.return_value = mock_resp
    ctx.__exit__.return_value = None
    with patch(
        "motet.core.tools.builtin.process_control_bridge_client.urllib.request.urlopen",
        return_value=ctx,
    ):
        r = terminate_via_bridge(42, "SIGTERM")
    assert r.get("ok") is True
    assert r.get("pid") == 42


def test_process_control_run_validation() -> None:
    r = pc_run({"operation": "terminate"})
    assert "error" in r
    r2 = pc_run({"operation": "list", "limit": "x"})
    assert "error" in r2


def test_process_control_infer_capabilities() -> None:
    caps = _infer_tool_capabilities(
        ToolExecutionData(
            tool_name="core.process_control",
            parameters={"operation": "list"},
        )
    )
    assert WorkerCapability.EDGE_PROCESS_CONTROL in caps
    assert WorkerCapability.EDGE_EXECUTION in caps
