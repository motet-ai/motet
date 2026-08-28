"""
Motet - run_in_workspace downgrade tests (ADR-0106 §rule 2)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-25

Pins the silent-downgrade contract:

    * Master kill-switch off → run_one_shot
    * Missing tenant_id / conversation_id / image_stack → run_one_shot
    * Unknown mode → run_one_shot (loud warning)
    * Otherwise → WorkspaceContainerManager.dispatch with all keys forwarded
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from motet.core.execution.environment_manager import (
    run_in_workspace,
    run_stateful_in_workspace,
)
from motet.core.execution.models import ExecutionRequest, ExecutionResult


def _req() -> ExecutionRequest:
    return ExecutionRequest(argv=["echo"], cwd="/scratch", timeout_seconds=5)


def _success() -> ExecutionResult:
    return ExecutionResult(exit_code=0, backend="workspace-container")


def test_downgrades_when_kill_switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_WORKSPACE_CONTAINER_ENABLED", "false")
    with patch(
        "motet.core.execution.environment_manager.run_execution",
        return_value=ExecutionResult(exit_code=0, backend="subprocess"),
    ) as one_shot:
        with patch(
            "motet.core.execution.environment_manager.get_workspace_container_manager"
        ) as get_mgr:
            res = run_in_workspace(
                _req(),
                tenant_id="t1",
                conversation_id="c1",
                image_stack="python-minimal",
            )
    assert res.backend == "subprocess"
    one_shot.assert_called_once()
    get_mgr.assert_not_called()


@pytest.mark.parametrize(
    "tenant,conv,stack",
    [
        (None, "c1", "python-minimal"),
        ("t1", None, "python-minimal"),
        ("t1", "c1", None),
        ("t1", "c1", ""),
    ],
)
def test_downgrades_when_keys_missing(
    monkeypatch: pytest.MonkeyPatch, tenant, conv, stack
) -> None:
    monkeypatch.setenv("MOTET_WORKSPACE_CONTAINER_ENABLED", "true")
    with patch(
        "motet.core.execution.environment_manager.run_execution",
        return_value=ExecutionResult(exit_code=0, backend="subprocess"),
    ) as one_shot:
        with patch(
            "motet.core.execution.environment_manager.get_workspace_container_manager"
        ) as get_mgr:
            res = run_in_workspace(
                _req(),
                tenant_id=tenant,
                conversation_id=conv,
                image_stack=stack,
            )
    assert res.backend == "subprocess"
    one_shot.assert_called_once()
    get_mgr.assert_not_called()


def test_downgrades_on_unknown_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_WORKSPACE_CONTAINER_ENABLED", "true")
    with patch(
        "motet.core.execution.environment_manager.run_execution",
        return_value=ExecutionResult(exit_code=0, backend="subprocess"),
    ) as one_shot:
        with patch(
            "motet.core.execution.environment_manager.get_workspace_container_manager"
        ) as get_mgr:
            res = run_in_workspace(
                _req(),
                tenant_id="t1",
                conversation_id="c1",
                image_stack="python-minimal",
                mode="lukewarm",
            )
    assert res.backend == "subprocess"
    one_shot.assert_called_once()
    get_mgr.assert_not_called()


def test_dispatches_to_manager_when_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_WORKSPACE_CONTAINER_ENABLED", "true")
    fake_mgr = MagicMock()
    fake_mgr.dispatch.return_value = _success()

    with patch(
        "motet.core.execution.environment_manager.run_execution"
    ) as one_shot:
        with patch(
            "motet.core.execution.environment_manager.get_workspace_container_manager",
            return_value=fake_mgr,
        ):
            res = run_in_workspace(
                _req(),
                tenant_id="t1",
                conversation_id="c1",
                image_stack="python-minimal",
                mode="cold",
                oci_image_ref="python:3.11-slim",
            )
    assert res.backend == "workspace-container"
    one_shot.assert_not_called()
    fake_mgr.dispatch.assert_called_once()
    kwargs = fake_mgr.dispatch.call_args.kwargs
    assert kwargs == {
        "tenant_id": "t1",
        "conversation_id": "c1",
        "image_stack": "python-minimal",
        "bundle_id": "__manual__",
        "skill_name": "__manual__",
        "mode": "cold",
        "oci_image_ref": "python:3.11-slim",
    }


# ---------------------------------------------------------------------------
# run_stateful_in_workspace — silent downgrade + happy-path delegation
# ---------------------------------------------------------------------------


def _stateful_kwargs(**overrides):
    base = dict(
        tenant_id="t1",
        conversation_id="c1",
        image_stack="python-minimal",
        oci_image_ref="python:3.11-slim",
        script_source=b"def handle(p):\n    return p\n",
        script_logical_name="counter.py",
        params={"label": "x"},
        timeout_seconds=10,
        request_id="req-1",
    )
    base.update(overrides)
    return base


def test_stateful_returns_disabled_envelope_when_kill_switch_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stateful has no per-call fallback (it's all-or-nothing on state).

    When workspace containers are disabled, the runtime returns a structured
    ok=False envelope so the caller (and the LLM) sees an explicit signal
    instead of silently losing module-level state.
    """
    monkeypatch.setenv("MOTET_WORKSPACE_CONTAINER_ENABLED", "false")
    with patch(
        "motet.core.execution.environment_manager.get_workspace_container_manager"
    ) as get_mgr:
        env = run_stateful_in_workspace(**_stateful_kwargs())
    assert env["ok"] is False
    assert env["transport_error"] is True
    assert "workspace containers" in env["error"].lower() or "disabled" in env["error"].lower()
    get_mgr.assert_not_called()


@pytest.mark.parametrize(
    "missing",
    ["tenant_id", "conversation_id", "image_stack"],
)
def test_stateful_returns_disabled_envelope_when_keying_inputs_missing(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setenv("MOTET_WORKSPACE_CONTAINER_ENABLED", "true")
    overrides = {missing: None}
    with patch(
        "motet.core.execution.environment_manager.get_workspace_container_manager"
    ) as get_mgr:
        env = run_stateful_in_workspace(**_stateful_kwargs(**overrides))
    assert env["ok"] is False
    assert env["transport_error"] is True
    get_mgr.assert_not_called()


def test_stateful_dispatches_to_manager_when_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOTET_WORKSPACE_CONTAINER_ENABLED", "true")
    monkeypatch.setenv("MOTET_WORKSPACE_STATEFUL_MODE_ENABLED", "true")
    fake_mgr = MagicMock()
    fake_mgr.dispatch_warm.return_value = {
        "ok": True,
        "result": {"label": "x"},
        "workspace_mode": "stateful",
    }
    with patch(
        "motet.core.execution.environment_manager.get_workspace_container_manager",
        return_value=fake_mgr,
    ):
        env = run_stateful_in_workspace(**_stateful_kwargs())
    assert env["ok"] is True
    assert env["workspace_mode"] == "stateful"
    fake_mgr.dispatch_warm.assert_called_once()
    kwargs = fake_mgr.dispatch_warm.call_args.kwargs
    assert kwargs["tenant_id"] == "t1"
    assert kwargs["conversation_id"] == "c1"
    assert kwargs["image_stack"] == "python-minimal"
    assert kwargs["bundle_id"] == "__manual__"
    assert kwargs["skill_name"] == "__manual__"
    assert kwargs["oci_image_ref"] == "python:3.11-slim"
    assert kwargs["params"] == {"label": "x"}
    assert kwargs["timeout_seconds"] == 10
    assert kwargs["request_id"] == "req-1"
    # WarmBootstrapPlan must wrap the script source for the long-lived process.
    plan = kwargs["warm_plan"]
    assert plan.script_source == b"def handle(p):\n    return p\n"
    assert plan.script_logical_name == "counter.py"
    assert plan.script_sha256  # populated by __post_init__


# ---------------------------------------------------------------------------
# MOTET_WORKSPACE_STATEFUL_MODE_ENABLED — stateful→workspace downgrade gate
# ---------------------------------------------------------------------------


def test_stateful_downgrades_to_workspace_when_stateful_mode_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0106 §Configuration: with the stateful-mode operator gate off,
    stateful declarations MUST run via the workspace pipeline (same workspace,
    just without long-lived module-level state). The returned envelope
    keeps the stateful shape but flags ``downgraded_from='stateful'`` so the
    dashboard and the LLM can both see that stateful semantics weren't
    honored."""
    monkeypatch.setenv("MOTET_WORKSPACE_CONTAINER_ENABLED", "true")
    monkeypatch.setenv("MOTET_WORKSPACE_STATEFUL_MODE_ENABLED", "false")

    cold_result = ExecutionResult(
        exit_code=0,
        stdout='{"ok": true, "result": {"label": "x"}, "stdout": "", "stderr": ""}',
        stderr="",
        backend="workspace-container",
        backend_ref="deadbeef0001",
    )
    fake_mgr = MagicMock()
    fake_mgr.dispatch.return_value = cold_result

    with patch(
        "motet.core.execution.environment_manager.get_workspace_container_manager",
        return_value=fake_mgr,
    ):
        env = run_stateful_in_workspace(**_stateful_kwargs())

    # The stateful path MUST NOT have been called — that's the whole point.
    fake_mgr.dispatch_warm.assert_not_called()
    # Workspace path MUST have been called via run_in_workspace → manager.dispatch.
    fake_mgr.dispatch.assert_called_once()
    cold_kwargs = fake_mgr.dispatch.call_args.kwargs
    assert cold_kwargs["mode"] == "cold"
    assert cold_kwargs["tenant_id"] == "t1"
    assert cold_kwargs["conversation_id"] == "c1"
    assert cold_kwargs["image_stack"] == "python-minimal"
    cold_req = fake_mgr.dispatch.call_args.args[0]
    assert cold_req.argv == ["python3", "/motet/_cold_handle_once.py"]
    assert [f.path for f in cold_req.input_files] == [
        "/motet/_cold_handle_once.py",
        "/motet/skill_module.py",
    ]
    assert cold_req.input_files[1].content == b"def handle(p):\n    return p\n"

    # Stateful-shaped envelope so the runner runtime's consumer is unchanged,
    # but with explicit downgrade markers.
    assert env["workspace_mode"] == "stateful"
    assert env["downgraded_from"] == "stateful"
    assert env["downgraded_to"] == "workspace"
    assert "MOTET_WORKSPACE_STATEFUL_MODE_ENABLED" in env["downgraded_reason"]
    assert env["ok"] is True
    assert env["result"] == {"label": "x"}
    assert env["stdout"] == ""
    assert env["stderr"] == ""
    assert env["container_id"] == "deadbeef0001"


def test_stateful_dispatches_stateful_when_stateful_mode_default_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (env unset) MUST keep stateful semantics — operators don't
    have to opt in to stateful mode. Asymmetric default with the kill switch is
    intentional: ADR-0106 ships stateful mode enabled."""
    monkeypatch.setenv("MOTET_WORKSPACE_CONTAINER_ENABLED", "true")
    monkeypatch.delenv("MOTET_WORKSPACE_STATEFUL_MODE_ENABLED", raising=False)

    fake_mgr = MagicMock()
    fake_mgr.dispatch_warm.return_value = {"ok": True, "workspace_mode": "stateful"}

    with patch(
        "motet.core.execution.environment_manager.get_workspace_container_manager",
        return_value=fake_mgr,
    ):
        env = run_stateful_in_workspace(**_stateful_kwargs())

    fake_mgr.dispatch_warm.assert_called_once()
    fake_mgr.dispatch.assert_not_called()
    assert env["workspace_mode"] == "stateful"
    assert "downgraded_from" not in env
