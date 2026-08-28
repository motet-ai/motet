"""
Motet - Execution environment manager (Phase 2 facade)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Single orchestration-facing entry for argv-style execution. Three flavors:

    * ``run_one_shot``: disposable per-call container (or subprocess), maps
    to ``MOTET_EXEC_BACKEND``. The baseline.

    * ``run_in_workspace``: per-workspace container.
    Delegates to the WorkspaceContainerManager when workspace containers are enabled and
    the call carries a ``conversation_id``; downgrades to ``run_one_shot``
    otherwise.

    * ``run_stateful_in_workspace``: per-workspace container
    that hosts a long-lived in-container Python process. The caller
    supplies the runner's skill module source and a ``params`` dict;
    module-level globals survive between calls within a workspace. When
    stateful mode is operator-disabled, the module is still shipped into the
    workspace container and invoked once through a manager-owned
    ``handle(params)`` shim so the author contract remains correct.

    Long-lived MCP servers use the same Docker Engine socket when
    ``MOTET_MCP_EXEC_BACKEND=docker`` (or when ``MOTET_EXEC_BACKEND=docker``
    is inherited); see ``mcp_docker_stdio`` / ``mcp_docker_http`` in the MCP
    proxy package.

Dependencies:
    - motet.core.execution.runner
    - motet.core.execution.workspace_container_manager

Notes:
    - Prefer calling these helpers from new orchestration code; existing
      ``core.worker_exec`` callers continue to use ``run_execution`` directly
      for the per-call path and opt into the workspace path via the
      ``workspace_mode`` tool parameter. Warm dispatch is only available
      through ``run_stateful_in_workspace`` (the wire shape is fundamentally
      different — params instead of argv — so we don't pretend it fits the
      ExecutionRequest envelope).
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, Optional

import structlog

from motet.core.distributed.workspace_container_registry import (
    DEFAULT_WORKSPACE_SCOPE_BUNDLE_ID,
    DEFAULT_WORKSPACE_SCOPE_SKILL_NAME,
)

from .models import ExecutionInputFile, ExecutionRequest, ExecutionResult
from .runner import run_execution
from .workspace_container_manager import (
    WarmBootstrapPlan,
    get_workspace_container_manager,
    is_workspace_container_enabled,
    is_stateful_mode_enabled,
)

logger = structlog.get_logger(__name__)


def run_one_shot(request: ExecutionRequest) -> ExecutionResult:
    """Execute a one-shot argv run (maps to MOTET_EXEC_BACKEND)."""
    return run_execution(request)


def run_in_workspace(
    request: ExecutionRequest,
    *,
    tenant_id: Optional[str],
    conversation_id: Optional[str],
    image_stack: Optional[str],
    mode: str = "cold",
    oci_image_ref: Optional[str] = None,
    bundle_id: str = DEFAULT_WORKSPACE_SCOPE_BUNDLE_ID,
    skill_name: str = DEFAULT_WORKSPACE_SCOPE_SKILL_NAME,
) -> ExecutionResult:
    """ADR-0106 workspace execution entry.

    Resolves (or lazily creates) the workspace container for
    ``(tenant_id, conversation_id, image_stack)`` and dispatches argv into
    it via ``docker exec``. ADR-0106 §rule 2 (silent downgrade) is enforced
    here: when any of the keying inputs is missing, or when the master
    kill-switch is off, the call falls back to ``run_one_shot``.

    The image stack must be a known platform stack (ADR-0101 Slice A); the
    caller is responsible for resolving it ahead of this call so the
    routing key dimension is stable.
    """
    if not is_workspace_container_enabled():
        logger.debug(
            "execution.workspace_disabled_downgrade",
            reason="MOTET_WORKSPACE_CONTAINER_ENABLED",
            tenant_id=tenant_id,
            conversation_id=conversation_id,
        )
        return run_one_shot(request)

    if not tenant_id or not conversation_id or not image_stack:
        logger.debug(
            "execution.workspace_missing_key_downgrade",
            tenant_id=bool(tenant_id),
            conversation_id=bool(conversation_id),
            image_stack=bool(image_stack),
        )
        return run_one_shot(request)

    if mode not in ("cold", "warm"):
        logger.warning(
            "execution.workspace_unknown_mode_downgrade",
            mode=mode,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
        )
        return run_one_shot(request)

    manager = get_workspace_container_manager()
    return manager.dispatch(
        request,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        image_stack=image_stack,
        bundle_id=bundle_id,
        skill_name=skill_name,
        mode=mode,
        oci_image_ref=oci_image_ref,
    )


def run_stateful_in_workspace(
    *,
    tenant_id: Optional[str],
    conversation_id: Optional[str],
    image_stack: Optional[str],
    oci_image_ref: Optional[str],
    script_source: bytes,
    script_logical_name: str,
    params: Dict[str, Any],
    timeout_seconds: Optional[int] = None,
    request_id: Optional[str] = None,
    bundle_id: str = DEFAULT_WORKSPACE_SCOPE_BUNDLE_ID,
    skill_name: str = DEFAULT_WORKSPACE_SCOPE_SKILL_NAME,
) -> Dict[str, Any]:
    """ADR-0106 stateful workspace dispatch entry.

    The caller supplies the *exact bytes* of the runner's skill module
    (as it lives in the staged bundle). The manager hashes those bytes
    onto the binding so a redeploy that changes the source forces a
    clean container; identical sources reuse the existing supervisor and
    its loaded module-level state.

    Returns the supervisor envelope shaped as::

        {
          "id": "<request_id>",
          "ok": True / False,
          "result": {...},          # present when ok=True
          "error": "...",           # present when ok=False
          "traceback": "...",       # present when ok=False
          "stdout": "...",
          "stderr": "...",
          "workspace_mode": "stateful",
          "workspace_image_stack": "...",
          "container_id": "...",
          ...
        }

    Per ADR-0106 §rule 2 (silent downgrade), missing keying inputs or a
    disabled master kill-switch produce a transport-error envelope (with
    ``ok=False`` and ``transport_error=True``) rather than raising; this
    keeps the caller's branchless "consume the envelope" path intact.
    """
    if not is_workspace_container_enabled():
        logger.debug(
            "execution.stateful_workspace_disabled",
            reason="MOTET_WORKSPACE_CONTAINER_ENABLED",
            tenant_id=tenant_id,
            conversation_id=conversation_id,
        )
        return _stateful_disabled_envelope(
            request_id=request_id or "",
            reason="workspace container support is disabled (MOTET_WORKSPACE_CONTAINER_ENABLED)",
        )

    if not tenant_id or not conversation_id or not image_stack:
        logger.debug(
            "execution.stateful_workspace_missing_key",
            tenant_id=bool(tenant_id),
            conversation_id=bool(conversation_id),
            image_stack=bool(image_stack),
        )
        return _stateful_disabled_envelope(
            request_id=request_id or "",
            reason=(
                "stateful workspace dispatch requires tenant_id, conversation_id, and image_stack; "
                "one or more were missing"
            ),
        )

    # ADR-0106 §Configuration: ``MOTET_WORKSPACE_STATEFUL_MODE_ENABLED`` is the
    # operator gate for stateful mode specifically. When off,
    # ``lifetime: stateful`` declarations downgrade to ``lifetime: workspace`` —
    # same workspace container, just no long-lived in-process state. This is *not* a
    # transport error; the runner still produces a useful result envelope
    # (loses module-level globals, keeps ``/scratch``).
    if not is_stateful_mode_enabled():
        logger.info(
            "execution.stateful_mode_disabled_downgrade_to_workspace",
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            image_stack=image_stack,
        )
        return _stateful_to_workspace_downgrade(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            image_stack=image_stack,
            oci_image_ref=oci_image_ref,
            bundle_id=bundle_id,
            skill_name=skill_name,
            script_source=script_source,
            script_logical_name=script_logical_name,
            params=params,
            timeout_seconds=timeout_seconds,
            request_id=request_id,
        )

    plan = WarmBootstrapPlan(
        script_source=script_source,
        script_logical_name=script_logical_name,
    )
    manager = get_workspace_container_manager()
    return manager.dispatch_warm(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        image_stack=image_stack,
        oci_image_ref=oci_image_ref,
        bundle_id=bundle_id,
        skill_name=skill_name,
        warm_plan=plan,
        params=params,
        timeout_seconds=timeout_seconds,
        request_id=request_id,
    )


def _stateful_disabled_envelope(*, request_id: str, reason: str) -> Dict[str, Any]:
    return {
        "id": request_id,
        "ok": False,
        "error": reason,
        "traceback": "",
        "stdout": "",
        "stderr": "",
        "transport_error": True,
        "workspace_mode": "stateful",
        "downgraded": True,
    }


def _stateful_to_workspace_downgrade(
    *,
    tenant_id: str,
    conversation_id: str,
    image_stack: str,
    oci_image_ref: Optional[str],
    bundle_id: str,
    skill_name: str,
    script_source: bytes,
    script_logical_name: str,
    params: Dict[str, Any],
    timeout_seconds: Optional[int],
    request_id: Optional[str],
) -> Dict[str, Any]:
    """Run a stateful runner against the workspace pipeline.

    Used when ``MOTET_WORKSPACE_STATEFUL_MODE_ENABLED=false``. Builds an argv
    that materializes the skill module plus a one-shot shim inside the
    workspace container, then calls ``handle(params)`` exactly once. Callers
    get an envelope shaped like the stateful one for code-path uniformity,
    with ``downgraded_from="stateful"`` so observability can surface that
    stateful semantics are off.
    """
    cold_request = ExecutionRequest(
        argv=["python3", "/motet/_cold_handle_once.py"],
        cwd="/scratch",
        timeout_seconds=timeout_seconds or 60,
        input_files=[
            ExecutionInputFile(
                path="/motet/_cold_handle_once.py",
                content=_cold_handle_once_source(
                    params=params,
                    script_logical_name=script_logical_name,
                ),
            ),
            ExecutionInputFile(
                path="/motet/skill_module.py",
                content=script_source,
            ),
        ],
    )
    result = run_in_workspace(
        cold_request,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        image_stack=image_stack,
        bundle_id=bundle_id,
        skill_name=skill_name,
        mode="cold",
        oci_image_ref=oci_image_ref,
    )
    shim_envelope: Dict[str, Any] = {
        "ok": result.exit_code == 0,
        "result": {"stdout": result.stdout, "stderr": result.stderr},
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
    }
    if result.stdout:
        try:
            decoded = json.loads(result.stdout)
            if isinstance(decoded, dict):
                shim_envelope = decoded
        except Exception:
            pass
    # Surface as a stateful-shaped envelope with explicit downgrade marker
    # so the dashboard and the LLM can both notice that stateful mode wasn't
    # honored. Module-level state is gone but the workspace path still
    # delivered the runner's stdout, which is the next-best truth.
    return {
        "id": request_id or "",
        "ok": bool(shim_envelope.get("ok", result.exit_code == 0)),
        "result": shim_envelope.get("result", {"stdout": result.stdout, "stderr": result.stderr}),
        "stdout": str(shim_envelope.get("stdout") or ""),
        "stderr": str(shim_envelope.get("stderr") or ""),
        "error": shim_envelope.get("error"),
        "traceback": shim_envelope.get("traceback", ""),
        "workspace_mode": "stateful",
        "downgraded_from": "stateful",
        "downgraded_to": "workspace",
        "downgraded_reason": "MOTET_WORKSPACE_STATEFUL_MODE_ENABLED=false",
        "container_id": result.backend_ref or "",
    }


def _cold_handle_once_source(*, params: Dict[str, Any], script_logical_name: str) -> bytes:
    """Build a stdlib-only one-shot ``handle(params)`` shim for workspace execution."""

    params_b64 = base64.b64encode(
        json.dumps(params, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii")
    return f"""\
import base64
import importlib.util
import io
import json
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout

PARAMS = json.loads(base64.b64decode("{params_b64}").decode("utf-8"))
MODULE_PATH = "/motet/skill_module.py"
MODULE_NAME = "motet_cold_skill_module"

def _json_default(value):
    return repr(value)

def _load_handle():
    spec = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import skill module from {{MODULE_PATH!r}}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    handle = getattr(module, "handle", None)
    if handle is None or not callable(handle):
        raise RuntimeError(
            f"skill module {script_logical_name!r} does not export a callable named 'handle'"
        )
    return handle

def main():
    out_buf, err_buf = io.StringIO(), io.StringIO()
    try:
        handle = _load_handle()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            result = handle(PARAMS)
        if not isinstance(result, dict):
            result = {{"value": result}}
        sys.stdout.write(
            json.dumps(
                {{
                    "ok": True,
                    "result": result,
                    "stdout": out_buf.getvalue(),
                    "stderr": err_buf.getvalue(),
                }},
                default=_json_default,
            )
        )
        return 0
    except Exception as exc:
        sys.stdout.write(
            json.dumps(
                {{
                    "ok": False,
                    "error": f"{{type(exc).__name__}}: {{exc}}",
                    "traceback": traceback.format_exc(),
                    "stdout": out_buf.getvalue(),
                    "stderr": err_buf.getvalue(),
                }},
                default=_json_default,
            )
        )
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
""".encode("utf-8")


__all__ = ["run_in_workspace", "run_one_shot", "run_stateful_in_workspace"]
