"""
Motet - core.worker_exec workspace-mode dispatch tests (ADR-0106 Slice A)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-25

Pins the user-facing contract of the new ``workspace_mode`` parameter:

    * workspace_mode="workspace" + conversation context → run_in_workspace is called
      with (tenant_id, conversation_id, image_stack, mode, oci_image_ref) and
      bundle staging is skipped
    * workspace_mode="stateful" → loud rejection (Slice B)
    * unknown / malformed workspace_mode → structured err()
    * Pinned image stacks resolve to their oci_image_ref; unpinned stacks
      resolve to None so the manager falls back to its default
    * workspace_image_stack default flows through env then constant
    * The "downgrade when no conversation_id" path is provided by
      run_in_workspace itself (covered in environment_manager tests); this test
      verifies the tool plumbs the correct missing-context arguments through
      so the downgrade path is reachable.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from motet.core.execution.image_stacks import ImageStack
from motet.core.execution.models import ExecutionResult
from motet.core.tools.builtin import worker_exec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _success_result() -> ExecutionResult:
    return ExecutionResult(
        exit_code=0,
        stdout="ok",
        stderr="",
        timed_out=False,
        backend="workspace-container",
        backend_ref="abc123456789",
        oci_image_ref="python:3.11-slim",
    )


def _ctx(
    tenant_id: Optional[str] = "tenant-a",
    conversation_id: Optional[str] = "conv-1",
    command_id: Optional[str] = "corr-xyz",
) -> Any:
    ctx = MagicMock()
    ctx.tenant_id = tenant_id
    ctx.conversation_id = conversation_id
    ctx.command_id = command_id
    return ctx


@pytest.fixture
def patch_run_in_workspace():
    with patch.object(worker_exec, "run_in_workspace") as m:
        m.return_value = _success_result()
        yield m


# ---------------------------------------------------------------------------
# workspace_mode parameter normalization
# ---------------------------------------------------------------------------


def test_workspace_mode_stateful_is_loudly_rejected() -> None:
    out = worker_exec.run({"argv": ["echo", "hi"], "workspace_mode": "stateful"})
    assert "error" in out
    assert "stateful" in out["error"].lower()
    assert "lifetime: stateful" in out["error"].lower()


def test_workspace_mode_unknown_string_is_rejected() -> None:
    out = worker_exec.run({"argv": ["echo"], "workspace_mode": "kinda-workspace"})
    assert "error" in out
    assert "unknown workspace_mode" in out["error"]


def test_workspace_mode_non_string_is_rejected() -> None:
    out = worker_exec.run({"argv": ["echo"], "workspace_mode": 7})
    assert "error" in out
    assert "must be a string" in out["error"]


@pytest.mark.parametrize("falsy", [None, "", "none", "false", "off"])
def test_workspace_mode_falsy_falls_through_to_per_call(
    falsy: Optional[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When workspace_mode is None / falsy, _run_workspace is NOT called.

    We assert this by patching _run_workspace to fail loudly if invoked.
    """
    sentinel = MagicMock(side_effect=AssertionError("should not run workspace path"))
    with patch.object(worker_exec, "_run_workspace", sentinel):
        with patch.object(worker_exec, "_run_per_call", return_value={"ok": True}):
            params: Dict[str, Any] = {"argv": ["echo"]}
            if falsy is not None:
                params["workspace_mode"] = falsy
            out = worker_exec.run(params)
    assert out == {"ok": True}
    sentinel.assert_not_called()


# ---------------------------------------------------------------------------
# workspace_mode="workspace" dispatch — happy path
# ---------------------------------------------------------------------------


def test_workspace_mode_workspace_dispatches_to_run_in_workspace_with_context(
    patch_run_in_workspace: MagicMock,
) -> None:
    with patch.object(
        worker_exec, "_get_motet_context_optional", return_value=_ctx()
    ):
        with patch.object(
            worker_exec,
            "resolve_image_stack",
            return_value=ImageStack(
                name="python-minimal",
                oci_image_ref="python:3.11-slim",
                description="",
                builtin=True,
            ),
        ):
            out = worker_exec.run(
                {
                    "argv": ["python", "-c", "print('hi')"],
                    "workspace_mode": "workspace",
                }
            )

    assert "error" not in out
    assert out["returncode"] == 0
    assert out["workspace_mode"] == "workspace"
    assert out["workspace_image_stack"] == "python-minimal"
    assert out["workspace_conversation_id"] == "conv-1"

    patch_run_in_workspace.assert_called_once()
    kwargs = patch_run_in_workspace.call_args.kwargs
    assert kwargs["tenant_id"] == "tenant-a"
    assert kwargs["conversation_id"] == "conv-1"
    assert kwargs["image_stack"] == "python-minimal"
    assert kwargs["bundle_id"] == "__manual__"
    assert kwargs["skill_name"] == "__manual__"
    assert kwargs["mode"] == "cold"
    assert kwargs["oci_image_ref"] == "python:3.11-slim"

    # Request shaping: argv preserved, cwd forced to /scratch, context fields populated
    req = patch_run_in_workspace.call_args.args[0]
    assert req.argv == ["python", "-c", "print('hi')"]
    assert req.cwd == "/scratch"
    assert req.tenant_id == "tenant-a"
    assert req.correlation_id == "corr-xyz"
    assert req.input_files == []


def test_workspace_mode_workspace_with_explicit_image_stack_overrides_default(
    patch_run_in_workspace: MagicMock,
) -> None:
    with patch.object(
        worker_exec, "_get_motet_context_optional", return_value=_ctx()
    ):
        with patch.object(
            worker_exec,
            "resolve_image_stack",
            return_value=ImageStack(
                name="python-office",
                oci_image_ref="python-office:1.2@sha256:deadbeef",
                description="",
                builtin=True,
            ),
        ):
            out = worker_exec.run(
                {
                    "argv": ["python", "main.py"],
                    "workspace_mode": "workspace",
                    "workspace_image_stack": "python-office",
                }
            )

    assert out["workspace_image_stack"] == "python-office"
    kwargs = patch_run_in_workspace.call_args.kwargs
    assert kwargs["image_stack"] == "python-office"
    assert kwargs["oci_image_ref"] == "python-office:1.2@sha256:deadbeef"


def test_workspace_mode_workspace_unpinned_stack_passes_none_image_ref(
    patch_run_in_workspace: MagicMock,
) -> None:
    """Unpinned stacks → oci_image_ref=None so the manager falls back to its
    operator default (recoverable rather than fatal — see _resolve_workspace_image
    docstring)."""
    with patch.object(
        worker_exec, "_get_motet_context_optional", return_value=_ctx()
    ):
        with patch.object(
            worker_exec,
            "resolve_image_stack",
            return_value=ImageStack(
                name="python-office",
                oci_image_ref="",
                description="",
                builtin=True,
            ),
        ):
            worker_exec.run(
                {
                    "argv": ["python"],
                    "workspace_mode": "workspace",
                    "workspace_image_stack": "python-office",
                }
            )

    kwargs = patch_run_in_workspace.call_args.kwargs
    assert kwargs["oci_image_ref"] is None


def test_workspace_mode_workspace_unknown_stack_passes_none_image_ref(
    patch_run_in_workspace: MagicMock,
) -> None:
    with patch.object(
        worker_exec, "_get_motet_context_optional", return_value=_ctx()
    ):
        with patch.object(worker_exec, "resolve_image_stack", return_value=None):
            worker_exec.run(
                {
                    "argv": ["python"],
                    "workspace_mode": "workspace",
                    "workspace_image_stack": "no-such-stack",
                }
            )

    kwargs = patch_run_in_workspace.call_args.kwargs
    assert kwargs["oci_image_ref"] is None
    assert kwargs["image_stack"] == "no-such-stack"


def test_workspace_image_stack_default_from_env(
    patch_run_in_workspace: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOTET_WORKSPACE_CONTAINER_DEFAULT_IMAGE_STACK", "python-office")
    with patch.object(
        worker_exec, "_get_motet_context_optional", return_value=_ctx()
    ):
        with patch.object(
            worker_exec,
            "resolve_image_stack",
            return_value=ImageStack(
                name="python-office",
                oci_image_ref="python-office:1.2@sha256:deadbeef",
                description="",
                builtin=True,
            ),
        ):
            worker_exec.run({"argv": ["python"], "workspace_mode": "workspace"})
    kwargs = patch_run_in_workspace.call_args.kwargs
    assert kwargs["image_stack"] == "python-office"


def test_workspace_image_stack_invalid_type_rejected() -> None:
    out = worker_exec.run(
        {
            "argv": ["python"],
            "workspace_mode": "workspace",
            "workspace_image_stack": 7,
        }
    )
    assert "error" in out
    assert "workspace_image_stack must be a string" in out["error"]


def test_workspace_mode_workspace_no_context_passes_none_keys(
    patch_run_in_workspace: MagicMock,
) -> None:
    """The downgrade-on-missing-context behavior lives in run_in_workspace
    (covered there). This test pins that the tool *passes the right None
    values through* so the downgrade is reachable."""
    with patch.object(worker_exec, "_get_motet_context_optional", return_value=None):
        with patch.object(
            worker_exec,
            "resolve_image_stack",
            return_value=ImageStack(
                name="python-minimal",
                oci_image_ref="python:3.11-slim",
                description="",
                builtin=True,
            ),
        ):
            out = worker_exec.run(
                {"argv": ["echo"], "workspace_mode": "workspace"}
            )

    kwargs = patch_run_in_workspace.call_args.kwargs
    assert kwargs["tenant_id"] is None
    assert kwargs["conversation_id"] is None

    # workspace_conversation_id is omitted from output when context is missing.
    assert "workspace_conversation_id" not in out


def test_workspace_mode_workspace_skips_bundle_staging(
    patch_run_in_workspace: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0106 §"What this ADR is not": workspace mode does NOT stage the
    bundle. This is structural — a sentinel _stage_bundle_into_cwd that
    raises if invoked confirms the workspace path bypasses it.

    bundle_id is still surfaced in the output for observability.
    """
    fail = MagicMock(side_effect=AssertionError("workspace path must not stage bundles"))
    with patch.object(worker_exec, "_stage_bundle_into_cwd", fail):
        with patch.object(
            worker_exec, "_get_motet_context_optional", return_value=_ctx()
        ):
            with patch.object(
                worker_exec,
                "resolve_image_stack",
                return_value=ImageStack(
                    name="python-minimal",
                    oci_image_ref="python:3.11-slim",
                    description="",
                    builtin=True,
                ),
            ):
                out = worker_exec.run(
                    {
                        "argv": ["echo"],
                        "workspace_mode": "workspace",
                        "bundle_id": "acme.demo",
                    }
                )

    assert out["bundle_id"] == "acme"  # _coerce_bundle_slug strips the .skill suffix
    assert out["workspace_mode"] == "workspace"
    assert out["workspace_bundle_id"] == "acme"
    assert out["workspace_skill_name"] == "demo"
    fail.assert_not_called()
    kwargs = patch_run_in_workspace.call_args.kwargs
    assert kwargs["bundle_id"] == "acme"
    assert kwargs["skill_name"] == "demo"


def test_workspace_mode_workspace_accepts_internal_runner_scope(
    patch_run_in_workspace: MagicMock,
) -> None:
    with patch.object(
        worker_exec, "_get_motet_context_optional", return_value=_ctx()
    ):
        with patch.object(
            worker_exec,
            "resolve_image_stack",
            return_value=ImageStack(
                name="python-minimal",
                oci_image_ref="python:3.11-slim",
                description="",
                builtin=True,
            ),
        ):
            out = worker_exec.run(
                {
                    "argv": ["python"],
                    "workspace_mode": "workspace",
                    "bundle_id": "staging-slug",
                    "workspace_bundle_id": "real-bundle",
                    "workspace_skill_name": "pdf",
                }
            )

    kwargs = patch_run_in_workspace.call_args.kwargs
    assert kwargs["bundle_id"] == "real-bundle"
    assert kwargs["skill_name"] == "pdf"
    assert out["workspace_bundle_id"] == "real-bundle"
    assert out["workspace_skill_name"] == "pdf"


def test_workspace_mode_workspace_passes_materialized_files_to_workspace_request(
    patch_run_in_workspace: MagicMock,
) -> None:
    with patch.object(
        worker_exec, "_get_motet_context_optional", return_value=_ctx()
    ):
        worker_exec.run(
            {
                "argv": ["python3", "/scratch/skills/demo/run.py"],
                "workspace_mode": "workspace",
                "workspace_materialized_files": [
                    {
                        "path": "/scratch/skills/demo/run.py",
                        "content": b"print('hi')\n",
                    }
                ],
            }
        )

    req = patch_run_in_workspace.call_args.args[0]
    assert len(req.input_files) == 1
    assert req.input_files[0].path == "/scratch/skills/demo/run.py"
    assert req.input_files[0].content == b"print('hi')\n"
