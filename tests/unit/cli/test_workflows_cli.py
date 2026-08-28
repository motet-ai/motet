"""
Motet - Workflows CLI Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-07

Description:
    Unit tests for ``motet-cli workflows`` template and run-control commands
    (list/validate/register/unregister/execute and runs list/get/pause/cancel/resume).

Dependencies:
    - pytest: Test framework
    - click.testing: CliRunner
    - motet.cli.workflows: workflows_group (SDK re-export)
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest
from click.testing import CliRunner

from motet.cli.workflows import workflows_group

API = "http://localhost:8000"


class _Resp:
    """Simple response stub with JSON payload."""

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
        "motet_sdk.cli.workflows.get_api_headers",
        lambda: {"Authorization": "Bearer t"},
    )
    monkeypatch.setattr("motet_sdk.cli.workflows.api_request", _fake_api_request)
    return captured


def _invoke(args: List[str]) -> Any:
    return CliRunner().invoke(workflows_group, args)


# --- Help / validation --------------------------------------------------------


def test_workflows_runs_help_lists_control_commands() -> None:
    result = _invoke(["runs", "--help"])
    assert result.exit_code == 0
    for name in ("list", "get", "pause", "cancel", "resume"):
        assert name in result.output


def test_workflows_runs_resume_requires_kind_or_payload() -> None:
    result = _invoke(["runs", "resume", "wfrun-abc", "--api-url", API])
    assert result.exit_code != 0
    assert "--kind is required" in (result.output + str(result.exception))


def test_workflows_runs_resume_rejects_bad_observations_json() -> None:
    result = _invoke(
        [
            "runs",
            "resume",
            "wfrun-abc",
            "--kind",
            "handback_tools",
            "--observations",
            "{not-json",
            "--api-url",
            API,
        ]
    )
    assert result.exit_code != 0
    assert "Invalid JSON" in (result.output + str(result.exception))


def test_workflows_runs_resume_rejects_non_array_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_api(monkeypatch, {})
    result = _invoke(
        [
            "runs",
            "resume",
            "wfrun-abc",
            "--kind",
            "handback_tools",
            "--observations",
            '{"tool_call_id": "x"}',
            "--api-url",
            API,
        ]
    )
    assert result.exit_code != 0
    assert "JSON array" in (result.output + str(result.exception))


# --- Template commands --------------------------------------------------------


def test_workflows_list_templates(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_api(
        monkeypatch,
        {
            "registered_workflows": [
                {"workflow_id": "navigate_screenshot", "name": "Navigate", "step_count": 2}
            ]
        },
    )
    result = _invoke(["list", "--api-url", API])
    assert result.exit_code == 0, result.output
    assert captured["args"][:2] == ("GET", f"{API}/api/v1/workflows")
    assert "navigate_screenshot" in result.output
    assert "steps=2" in result.output


def test_workflows_validate_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_api(monkeypatch, {"ok": True, "mode": "validate"})
    result = _invoke(
        [
            "validate",
            "--yaml",
            "workflow_id: demo\nname: Demo\nsteps: {}\n",
            "--api-url",
            API,
        ]
    )
    assert result.exit_code == 0, result.output
    assert captured["args"][:2] == ("POST", f"{API}/api/v1/workflows/validate")
    assert "workflow_id: demo" in captured["kwargs"]["json"]["yaml"]


def test_workflows_register_yaml_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    captured = _patch_api(
        monkeypatch,
        {"ok": True, "workflow_id": "user.acme.demo", "tool_name": "workflow_user.acme.demo"},
    )
    path = tmp_path / "wf.yaml"
    path.write_text("workflow_id: demo\nname: Demo\nsteps: {}\n", encoding="utf-8")
    result = _invoke(
        ["register", "--yaml-file", str(path), "--replace", "--api-url", API]
    )
    assert result.exit_code == 0, result.output
    assert captured["args"][:2] == ("POST", f"{API}/api/v1/workflows/register")
    body = captured["kwargs"]["json"]
    assert body["replace"] is True
    assert "workflow_id: demo" in body["yaml"]


def test_workflows_unregister(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_api(monkeypatch, {"ok": True, "unregistered": True})
    result = _invoke(
        ["unregister", "user.acme.demo", "--api-url", API]
    )
    assert result.exit_code == 0, result.output
    assert captured["args"][:2] == (
        "DELETE",
        f"{API}/api/v1/workflows/user.acme.demo",
    )


def test_workflows_execute_with_steps_json(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_api(
        monkeypatch,
        {"status": "completed", "workflow_id": "demo", "step_results": {}},
    )
    steps = [
        {
            "step_id": "step1",
            "name": "Step 1",
            "command_type": "core.tool_execution",
            "command_data": {"tool_name": "core.math_eval", "parameters": {"expression": "1+1"}},
            "dependencies": [],
        }
    ]
    result = _invoke(
        [
            "execute",
            "--workflow-id",
            "demo",
            "--workflow-name",
            "Demo",
            "--steps",
            json.dumps(steps),
            "--context",
            '{"user": "u1"}',
            "--api-url",
            API,
        ]
    )
    assert result.exit_code == 0, result.output
    assert captured["args"][:2] == ("POST", f"{API}/api/v1/workflows/execute")
    body = captured["kwargs"]["json"]
    assert body["workflow_id"] == "demo"
    assert body["workflow_name"] == "Demo"
    assert body["steps"] == steps
    assert body["context"] == {"user": "u1"}
    assert '"status": "completed"' in result.output


def test_workflows_execute_steps_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_api(monkeypatch, {"status": "completed", "workflow_id": "demo"})
    steps = [{"step_id": "a", "name": "A", "command_type": "core.transform", "command_data": {}, "dependencies": []}]
    result = CliRunner().invoke(
        workflows_group,
        [
            "execute",
            "--workflow-id",
            "demo",
            "--workflow-name",
            "Demo",
            "--steps",
            "-",
            "--api-url",
            API,
        ],
        input=json.dumps(steps),
    )
    assert result.exit_code == 0, result.output
    assert captured["args"][:2] == ("POST", f"{API}/api/v1/workflows/execute")
    assert captured["kwargs"]["json"]["steps"] == steps
    assert captured["kwargs"]["json"]["context"] == {}


# --- Run control --------------------------------------------------------------


def test_workflows_runs_list(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_api(
        monkeypatch,
        {
            "status": "paused",
            "count": 1,
            "runs": [
                {
                    "workflow_run_id": "wfrun-1",
                    "workflow_id": "research",
                    "suspend_reason": "operator",
                }
            ],
        },
    )
    result = _invoke(
        ["runs", "list", "--status", "paused", "--limit", "10", "--api-url", API]
    )
    assert result.exit_code == 0, result.output
    assert captured["args"][:2] == ("GET", f"{API}/api/v1/workflows/runs")
    assert captured["kwargs"]["params"] == {
        "status": "paused",
        "limit": 10,
        "offset": 0,
    }
    assert "wfrun-1" in result.output
    assert "suspend_reason=operator" in result.output


def test_workflows_runs_get(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_api(
        monkeypatch,
        {
            "workflow_run_id": "wfrun-1",
            "status": "paused",
            "suspend_reason": "handback_tools",
        },
    )
    result = _invoke(["runs", "get", "wfrun-1", "--api-url", API])
    assert result.exit_code == 0, result.output
    assert captured["args"][:2] == ("GET", f"{API}/api/v1/workflows/runs/wfrun-1")
    assert '"suspend_reason": "handback_tools"' in result.output


def test_workflows_runs_pause_with_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_api(
        monkeypatch,
        {"workflow_run_id": "wfrun-1", "action": "pause", "applied": True},
    )
    result = _invoke(
        [
            "runs",
            "pause",
            "wfrun-1",
            "--reason",
            "hold for review",
            "--api-url",
            API,
        ]
    )
    assert result.exit_code == 0, result.output
    assert captured["args"][:2] == ("POST", f"{API}/api/v1/workflows/runs/wfrun-1/pause")
    assert captured["kwargs"]["json"] == {"reason": "hold for review"}


def test_workflows_runs_cancel_without_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_api(
        monkeypatch,
        {"workflow_run_id": "wfrun-1", "action": "cancel", "applied": True},
    )
    result = _invoke(["runs", "cancel", "wfrun-1", "--api-url", API])
    assert result.exit_code == 0, result.output
    assert captured["args"][:2] == ("POST", f"{API}/api/v1/workflows/runs/wfrun-1/cancel")
    assert captured["kwargs"]["json"] == {}


def test_workflows_runs_resume_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_api(
        monkeypatch,
        {"status": "completed", "workflow_run_id": "wfrun-1"},
    )
    result = _invoke(
        [
            "runs",
            "resume",
            "wfrun-1",
            "--kind",
            "operator",
            "--resume-epoch",
            "2",
            "--api-url",
            API,
        ]
    )
    assert result.exit_code == 0, result.output
    method, url = captured["args"][:2]
    assert method == "POST"
    assert url == f"{API}/api/v1/workflows/runs/wfrun-1/resume"
    assert captured["kwargs"]["json"] == {"kind": "operator", "resume_epoch": 2}
    assert captured["kwargs"]["timeout"] == 7200


def test_workflows_runs_resume_handback_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_api(monkeypatch, {"status": "completed"})
    observations = [{"tool_call_id": "call_1", "content": "ok"}]
    result = _invoke(
        [
            "runs",
            "resume",
            "wfrun-1",
            "--kind",
            "handback_tools",
            "--observations",
            json.dumps(observations),
            "--api-url",
            API,
        ]
    )
    assert result.exit_code == 0, result.output
    assert captured["kwargs"]["json"] == {
        "kind": "handback_tools",
        "observations": observations,
    }


def test_workflows_runs_resume_confirmation_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_api(monkeypatch, {"status": "completed"})
    result = _invoke(
        [
            "runs",
            "resume",
            "wfrun-1",
            "--kind",
            "confirmation",
            "--decision",
            "approve",
            "--api-url",
            API,
        ]
    )
    assert result.exit_code == 0, result.output
    assert captured["kwargs"]["json"] == {
        "kind": "confirmation",
        "decision": "approve",
    }


def test_workflows_runs_resume_payload_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_api(monkeypatch, {"status": "completed"})
    payload = {"kind": "elicitation", "answers": {"section": "intro"}}
    result = _invoke(
        [
            "runs",
            "resume",
            "wfrun-1",
            "--payload",
            json.dumps(payload),
            "--api-url",
            API,
        ]
    )
    assert result.exit_code == 0, result.output
    assert captured["kwargs"]["json"] == payload


def test_workflows_runs_resume_payload_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_api(monkeypatch, {"status": "completed"})
    payload = {"kind": "oauth", "auth_status": "completed"}
    result = CliRunner().invoke(
        workflows_group,
        ["runs", "resume", "wfrun-1", "--payload", "-", "--api-url", API],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0, result.output
    assert captured["kwargs"]["json"] == payload


def test_workflows_runs_resume_payload_overrides_kind_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When --payload is set, field flags are ignored (payload is authoritative)."""
    captured = _patch_api(monkeypatch, {"status": "completed"})
    payload = {"kind": "operator"}
    result = _invoke(
        [
            "runs",
            "resume",
            "wfrun-1",
            "--kind",
            "confirmation",
            "--decision",
            "reject",
            "--payload",
            json.dumps(payload),
            "--api-url",
            API,
        ]
    )
    assert result.exit_code == 0, result.output
    assert captured["kwargs"]["json"] == payload
