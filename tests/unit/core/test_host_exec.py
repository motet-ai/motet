"""Tests for core.host_exec (host shell bridge) and shell bridge client."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from motet.core.commands.capabilities import WorkerCapability
from motet.core.commands.builtin.tool import _infer_tool_capabilities
from motet.core.commands.command_data_classes import ToolExecutionData
from motet.core.tools.builtin.shell_bridge_client import exec_via_bridge
from motet.core.tools.builtin.host_exec import run as host_run


def test_exec_via_bridge_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_SHELL_BRIDGE_URL", "http://host.docker.internal:7777")
    monkeypatch.setenv("MOTET_SHELL_BRIDGE_TOKEN", "tok")
    body = json.dumps(
        {
            "returncode": 0,
            "stdout": "ok\n",
            "stderr": "",
            "timed_out": False,
            "stdout_truncated": False,
            "stderr_truncated": False,
        }
    ).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    ctx = MagicMock()
    ctx.__enter__.return_value = mock_resp
    ctx.__exit__.return_value = None
    with patch(
        "motet.core.tools.builtin.shell_bridge_client.urllib.request.urlopen",
        return_value=ctx,
    ):
        r = exec_via_bridge(["echo", "hi"], "/tmp", 5)
    assert r["returncode"] == 0
    assert "ok" in r["stdout"]


def test_host_exec_run_validation() -> None:
    r = host_run({"argv": []})
    assert "error" in r


def test_host_exec_generates_cwd_when_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_HOST_EXEC_DEFAULT_CWD_ROOT", "/tmp/imf-host-exec")
    with patch(
        "motet.core.tools.builtin.host_exec.exec_via_bridge",
        return_value={"returncode": 0, "stdout": "ok", "stderr": "", "timed_out": False},
    ) as mocked:
        r = host_run({"argv": ["echo", "hi"]})
    assert r.get("returncode") == 0
    assert r.get("cwd_generated") is True
    effective = r.get("effective_cwd") or ""
    assert effective.startswith("/tmp/imf-host-exec/runs/")
    called_cwd = mocked.call_args.args[1]
    assert called_cwd == effective


def test_host_exec_ignores_provided_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_HOST_EXEC_DEFAULT_CWD_ROOT", "/tmp/imf-host-exec")
    with patch(
        "motet.core.tools.builtin.host_exec.exec_via_bridge",
        return_value={"returncode": 0, "stdout": "ok", "stderr": "", "timed_out": False},
    ) as mocked:
        r = host_run({"argv": ["echo", "hi"], "cwd": "/tmp/provided"})
    assert r.get("returncode") == 0
    assert r.get("cwd_generated") is True
    effective = r.get("effective_cwd") or ""
    assert effective.startswith("/tmp/imf-host-exec/runs/")
    called_cwd = mocked.call_args.args[1]
    assert called_cwd == effective


def test_host_exec_infer_capabilities() -> None:
    caps = _infer_tool_capabilities(
        ToolExecutionData(
            tool_name="core.host_exec",
            parameters={"argv": ["true"]},
        )
    )
    assert WorkerCapability.EDGE_SHELL_EXEC in caps
    assert WorkerCapability.EDGE_EXECUTION in caps
