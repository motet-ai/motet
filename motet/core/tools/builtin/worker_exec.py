"""
Motet - Worker exec tool (worker/container domain)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Runs argv in the worker execution domain via motet.core.execution.run_execution.
    Phase 1 uses a subprocess backend with MOTET_WORKER_EXEC_CWD_ALLOWLIST; later phases
    swap backends (container / microVM) without changing the tool contract.
    Optional bundle_id merges oci_image_ref from published catalog exec block (Phase 3).
    Workspace-mode callers may also attach manager-owned input files that are
    materialized into the existing container before exec begins.

    Slice A adds an opt-in ``workspace_mode`` parameter. When set to
    ``"workspace"`` and the call carries a ``conversation_id``, the tool dispatches
    via the WorkspaceContainerManager so ``/scratch`` (the container's working
    directory) persists across calls in the conversation. Bundle staging is
    skipped in workspace mode — authors that want to pre-stage scripts must
    write them into ``/scratch`` themselves on a prior call.

Dependencies:
    - motet.core.execution: canonical request/result types and execution facade
    - motet.core.execution.image_stacks
    - Tool registry, protocol

Notes:
    - cwd is system-determined; callers do not provide cwd.
    - ``bundle_id`` may be a skill id (`bundle.skill`); the tool uses the bundle slug for staging and catalog merge.
    - Worker attribution for commands is in ``metadata.worker_id``; tool payloads only add execution-domain fields (backend, image, etc.).
    - ``workspace_mode="workspace"`` silently downgrades to per-call execution when
      the call is missing ``conversation_id`` or when the
      master kill-switch ``MOTET_WORKSPACE_CONTAINER_ENABLED`` is off.
    - Internal callers may pass ``workspace_bundle_id``, ``workspace_skill_name``,
      and ``workspace_materialized_files`` to scope and populate runner-owned
      conversation workspaces before argv runs.
    - ``MOTET_WORKER_EXEC_REUSE_ALLOWLIST_AS_CWD`` runs at the allowlist
      root (no ``runs/`` subdir) so git / compose in the app-builder clone work.
"""

from __future__ import annotations

import base64
import datetime
import os
import shutil
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from motet.core.distributed.workspace_container_registry import (
    DEFAULT_WORKSPACE_SCOPE_BUNDLE_ID,
    DEFAULT_WORKSPACE_SCOPE_SKILL_NAME,
)
from motet.core.execution import (
    ExecutionInputFile,
    ExecutionRequest,
    ExecutionResult,
    run_execution,
    run_in_workspace,
)
from motet.core.execution.image_stacks import resolve_image_stack

from ..protocol import err
from ..registry import ToolRegistry

_DEFAULT_WORKSPACE_IMAGE_STACK = "python-minimal"


class Params(BaseModel):
    argv: List[str] = Field(
        ...,
        description=(
            "Executable and arguments only (argv, not a shell command string). "
            "Prefer direct argv like [\"python3\", \"script.py\", \"input.pdf\"]. "
            "If you need shell features such as pipes, &&, redirection, or variable expansion, "
            "invoke a shell explicitly, e.g. [\"bash\", \"-lc\", \"python foo.py && python bar.py\"]."
        ),
    )
    timeout_seconds: Optional[int] = Field(
        default=None,
        description="Subprocess timeout (seconds); omit for MOTET_WORKER_EXEC_DEFAULT_TIMEOUT",
    )
    max_output_bytes: Optional[int] = Field(
        default=None,
        description=(
            "Max combined stdout+stderr capture before truncation "
            "(default ExecutionRequest.max_output_bytes = 1 MiB). "
            "Raise for long suites (e.g. app-builder.run_tests)."
        ),
    )
    bundle_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional bundle manifest name (directory under MOTET_PLUGIN_ROOT). "
            "If a skill id `bundle.skill` is passed, the bundle slug prefix is used. "
            "When provided, argv may reference bundle-relative script paths such as "
            "`skills/pdf/scripts/fill_fillable_fields.py`. "
            "With MOTET_EXEC_BACKEND=docker, fills oci_image_ref from Redis catalog config/exec.yaml."
        ),
    )
    workspace_mode: Optional[str] = Field(
        default=None,
        description=(
            " opt-in. When 'workspace' and the call has a conversation_id, "
            "argv runs inside a per-(tenant, conversation, bundle, skill, image_stack) "
            "container where the working directory ('/scratch') persists across calls. "
            "Silently downgrades to per-call execution when conversation_id is "
            "missing or MOTET_WORKSPACE_CONTAINER_ENABLED is off."
        ),
    )
    workspace_image_stack: Optional[str] = Field(
        default=None,
        description=(
            "Image stack id used to key the workspace container. "
            "Two calls in the same conversation for the same bundle skill and stack "
            "share /scratch; different skills or stacks each get their own container. "
            "Defaults to MOTET_WORKSPACE_CONTAINER_DEFAULT_IMAGE_STACK or 'python-minimal'."
        ),
    )
    workspace_bundle_id: Optional[str] = Field(
        default=None,
        description=(
            "Internal runner scope override for workspace containers. Defaults to "
            "the bundle_id slug when present, or a manual worker_exec scope."
        ),
    )
    workspace_skill_name: Optional[str] = Field(
        default=None,
        description=(
            "Internal runner scope override for workspace containers. Defaults to "
            "the skill suffix from bundle_id when bundle_id is `bundle.skill`, "
            "or a manual worker_exec scope."
        ),
    )


def _get_motet_context_optional() -> Any:
    try:
        from motet.core.commands.decorator import get_motet_context

        return get_motet_context()
    except Exception:
        return None


def _fmt(res: Dict[str, Any]) -> str:
    if "error" in res:
        return f"worker_exec(error={res['error']})"
    return (
        f"worker_exec(rc={res.get('returncode')}, "
        f"timed_out={res.get('timed_out')}, "
        f"out_len={len(res.get('stdout') or '')})"
    )


def _execution_troubleshooting(r: ExecutionResult) -> Dict[str, Any]:
    """Fields for support: backend id, container prefix, image, engine runtime."""
    meta: Dict[str, Any] = {
        "backend": r.backend,
    }
    if r.backend_ref:
        meta["backend_ref"] = r.backend_ref
    if r.oci_image_ref:
        meta["oci_image_ref"] = r.oci_image_ref
    if r.engine_runtime:
        meta["engine_runtime"] = r.engine_runtime
    return meta


def _result_to_tool_dict(r: ExecutionResult) -> Dict[str, Any]:
    """Map ExecutionResult to the same key shape as the host bridge success payload."""
    if r.error:
        return err(r.error, meta=_execution_troubleshooting(r))
    out: Dict[str, Any] = {
        "returncode": int(r.exit_code),
        "stdout": str(r.stdout or ""),
        "stderr": str(r.stderr or ""),
        "timed_out": bool(r.timed_out),
        "stdout_truncated": bool(r.stdout_truncated),
        "stderr_truncated": bool(r.stderr_truncated),
    }
    out.update(_execution_troubleshooting(r))
    return out


def _first_allowlist_prefix() -> Optional[str]:
    raw = (os.getenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST") or "").strip()
    if not raw:
        return None
    for part in raw.split(","):
        p = part.strip()
        if p:
            return os.path.abspath(p)
    return None


def _default_worker_exec_root() -> Optional[str]:
    # Keep default path aligned with docker-compose defaults.
    configured = (os.getenv("MOTET_WORKER_EXEC_DEFAULT_CWD_ROOT") or "").strip()
    if configured:
        return os.path.abspath(configured)
    return _first_allowlist_prefix() or "/var/motet/worker-exec"


def _resolve_effective_cwd() -> tuple[Optional[str], bool, Optional[str]]:
    root = _default_worker_exec_root()
    if not root:
        return (
            None,
            True,
            "cwd is required unless MOTET_WORKER_EXEC_DEFAULT_CWD_ROOT "
            "or MOTET_WORKER_EXEC_CWD_ALLOWLIST is configured",
        )
    # ADR-0122 app-builder worker: run directly in the isolated clone so git /
    # docker-compose see the repo root (docker backend bind-mounts this cwd).
    reuse = (os.getenv("MOTET_WORKER_EXEC_REUSE_ALLOWLIST_AS_CWD") or "").strip().lower()
    if reuse in {"1", "true", "yes"}:
        return os.path.abspath(root), False, None
    now = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    token = uuid.uuid4().hex[:12]
    generated = os.path.join(root, "runs", f"{now}-{token}")
    return os.path.abspath(generated), True, None


def _coerce_bundle_slug(bundle_id: str) -> str:
    """Resolve staging/catalog slug when callers pass skill id `bundle.skill`."""
    token = bundle_id.strip()
    if "." in token:
        prefix = token.split(".", 1)[0].strip()
        if prefix:
            return prefix
    return token


def _split_bundle_skill_id(bundle_id: str) -> tuple[str, Optional[str]]:
    """Return (bundle_slug, skill_suffix) for ids shaped like `bundle.skill`."""
    token = bundle_id.strip()
    if "." in token:
        prefix, suffix = token.split(".", 1)
        prefix = prefix.strip()
        suffix = suffix.strip()
        if prefix:
            return prefix, suffix or None
    return token, None


def _coerce_workspace_scope_value(raw: Any, *, field_name: str) -> Optional[str]:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{field_name} must be a string when provided")
    value = raw.strip()
    return value or None


def _plugin_root() -> str:
    return (os.getenv("MOTET_PLUGIN_ROOT", "/tmp/imf_bundles") or "/tmp/imf_bundles").rstrip("/")


def _find_under_skills(cwd_abs: str, rel_path: str) -> Optional[str]:
    """Search staged ``skills/*/`` directories for a file matching *rel_path*."""
    skills_dir = os.path.join(cwd_abs, "skills")
    if not os.path.isdir(skills_dir):
        return None
    for skill_name in os.listdir(skills_dir):
        candidate = os.path.join(skills_dir, skill_name, rel_path)
        if os.path.exists(candidate):
            return f"skills/{skill_name}/{rel_path}"
    return None


def _normalize_bundle_argv(
    argv: List[str],
    bundle_id: Optional[str],
    cwd_abs: Optional[str] = None,
) -> List[str]:
    """
    Normalize bundle-local script arguments to paths under the generated cwd.

    For container backends, only the generated cwd is mounted to /work, so script
    paths must be relative to that cwd (e.g. ``skills/...``), not host absolute.

    Also handles SKILL.md-relative paths (e.g. ``scripts/echo_payload.py``) by
    searching ``skills/*/`` in the staged cwd when the literal path doesn't exist.
    """
    if not bundle_id:
        return argv

    plugin_root = _plugin_root()
    bundle_root = f"{plugin_root}/{bundle_id}/"
    normalized: List[str] = []

    for arg in argv:
        token = arg.strip()
        rel_path: Optional[str] = None
        if token.startswith("/work/"):
            rel_path = token[len("/work/") :]
        elif token.startswith(bundle_root):
            rel_path = token[len(bundle_root) :]
        elif token.startswith("skills/"):
            rel_path = token

        if not rel_path:
            normalized.append(arg)
            continue

        if rel_path == "skills" or rel_path.startswith("skills/"):
            normalized.append(rel_path)
        elif cwd_abs and os.path.exists(os.path.join(cwd_abs, rel_path)):
            normalized.append(rel_path)
        else:
            normalized.append(f"skills/{rel_path}")
    return normalized


def _fixup_skill_relative_argv(argv: List[str], cwd_abs: str) -> List[str]:
    """
    Post-staging pass: resolve SKILL.md-relative paths that don't exist at the
    bundle root by searching ``skills/*/`` in the staged cwd.

    SKILL.md documents paths relative to the skill directory (e.g.
    ``scripts/echo_payload.py``), but after staging the file lives at
    ``skills/<skill_name>/scripts/echo_payload.py``.  This heuristic fixes
    argv entries that look like file paths but don't exist at the cwd root.
    """
    fixed: List[str] = []
    for arg in argv:
        if (
            not arg.startswith("-")
            and "/" in arg
            and not arg.startswith("skills/")
            and not os.path.exists(os.path.join(cwd_abs, arg))
        ):
            resolved = _find_under_skills(cwd_abs, arg)
            if resolved:
                fixed.append(resolved)
                continue
        fixed.append(arg)
    return fixed


def _stage_bundle_into_cwd(bundle_id: Optional[str], cwd_abs: str) -> Optional[str]:
    """Copy deployed bundle files into execution cwd for worker-side runs."""
    if not bundle_id:
        return None

    source_root = os.path.join(_plugin_root(), bundle_id)
    if not os.path.isdir(source_root):
        return f"bundle files not found: expected deployed bundle at {source_root!r}"

    try:
        os.makedirs(cwd_abs, mode=0o700, exist_ok=True)
        for name in os.listdir(source_root):
            src = os.path.join(source_root, name)
            dst = os.path.join(cwd_abs, name)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
            else:
                shutil.copy2(src, dst)
    except OSError as e:
        return f"failed staging bundle {bundle_id!r} into execution cwd: {e}"
    return None


def _normalize_workspace_mode(raw: Any) -> Optional[str]:
    """Map the user-facing ``workspace_mode`` parameter to ADR-0106 modes.

    Accepts:
      * ``None`` / missing / ``""`` / ``"none"`` / ``"false"`` → per-call
        (ADR-0106 baseline; no workspace container)
      * ``"workspace"`` → ADR-0106 Slice A path
      * ``"stateful"`` → reserved for stateful runners; rejected here because
        misuse is loud rather than a silent downgrade.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("workspace_mode must be a string when provided")
    token = raw.strip().lower()
    if token in ("", "none", "false", "off"):
        return None
    if token == "workspace":
        return "workspace"
    if token == "stateful":
        raise NotImplementedError(
            "workspace_mode='stateful' is reserved for runners with lifetime: stateful; "
            "use workspace_mode='workspace' for argv execution with persistent /scratch"
        )
    raise ValueError(f"unknown workspace_mode: {raw!r}")


def _resolve_workspace_image_stack(raw: Optional[str]) -> str:
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    env_default = (os.getenv("MOTET_WORKSPACE_CONTAINER_DEFAULT_IMAGE_STACK") or "").strip()
    return env_default or _DEFAULT_WORKSPACE_IMAGE_STACK


def _resolve_workspace_image(image_stack: str) -> Optional[str]:
    """Resolve the OCI image for a workspace container from the stack registry.

    Returns ``None`` when the stack is unknown OR registered-but-unpinned;
    the WorkspaceContainerManager falls back to ``MOTET_WORKSPACE_CONTAINER_DEFAULT_IMAGE``
    in that case so an operator's misconfiguration is recoverable rather
    than fatal.
    """
    stack = resolve_image_stack(image_stack)
    if stack is None or not stack.is_pinned:
        return None
    return stack.oci_image_ref


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    argv = params.get("argv")
    if not isinstance(argv, list) or not argv:
        return err("argv must be a non-empty list")
    str_argv: List[str] = []
    for i, a in enumerate(argv):
        if not isinstance(a, str):
            return err(f"argv[{i}] must be a string")
        if "\x00" in a:
            return err("argv must not contain null bytes")
        str_argv.append(a)

    timeout = params.get("timeout_seconds")
    if timeout is not None:
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            return err("timeout_seconds must be an integer")

    max_output_bytes = params.get("max_output_bytes")
    if max_output_bytes is not None:
        try:
            max_output_bytes = int(max_output_bytes)
        except (TypeError, ValueError):
            return err("max_output_bytes must be an integer")
        if max_output_bytes < 1024:
            return err("max_output_bytes must be >= 1024")

    bundle_raw = params.get("bundle_id")
    bundle_id_s: Optional[str] = None
    skill_name_from_bundle: Optional[str] = None
    if isinstance(bundle_raw, str) and bundle_raw.strip():
        bundle_id_s, skill_name_from_bundle = _split_bundle_skill_id(bundle_raw)

    try:
        workspace_mode = _normalize_workspace_mode(params.get("workspace_mode"))
    except (ValueError, NotImplementedError) as exc:
        return err(str(exc))

    try:
        workspace_input_files = _coerce_workspace_input_files(
            params.get("workspace_materialized_files")
        )
        workspace_bundle_id = _coerce_workspace_scope_value(
            params.get("workspace_bundle_id"), field_name="workspace_bundle_id"
        )
        workspace_skill_name = _coerce_workspace_scope_value(
            params.get("workspace_skill_name"), field_name="workspace_skill_name"
        )
    except ValueError as exc:
        return err(str(exc))

    if workspace_mode is not None:
        effective_workspace_bundle_id = (
            workspace_bundle_id
            or bundle_id_s
            or DEFAULT_WORKSPACE_SCOPE_BUNDLE_ID
        )
        effective_workspace_skill_name = (
            workspace_skill_name
            or skill_name_from_bundle
            or DEFAULT_WORKSPACE_SCOPE_SKILL_NAME
        )
        return _run_workspace(
            str_argv=str_argv,
            workspace_mode=workspace_mode,
            workspace_image_stack_raw=params.get("workspace_image_stack"),
            timeout=timeout,
            max_output_bytes=max_output_bytes,
            bundle_id=bundle_id_s,
            workspace_bundle_id=effective_workspace_bundle_id,
            workspace_skill_name=effective_workspace_skill_name,
            input_files=workspace_input_files,
        )

    return _run_per_call(
        str_argv=str_argv,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
        bundle_id=bundle_id_s,
    )


def _run_per_call(
    *,
    str_argv: List[str],
    timeout: Optional[int],
    max_output_bytes: Optional[int],
    bundle_id: Optional[str],
) -> Dict[str, Any]:
    cwd_s, cwd_generated, cwd_error = _resolve_effective_cwd()
    if cwd_error:
        return err(cwd_error)
    assert cwd_s is not None

    if not bundle_id and any(
        isinstance(a, str) and a.strip().startswith("skills/") for a in str_argv
    ):
        return err(
            "argv references bundle skill paths (skills/...) but no bundle_id was provided; "
            "set bundle_id to the bundle manifest name so the tool can stage files into the execution directory"
        )

    staged_err = _stage_bundle_into_cwd(bundle_id, cwd_s)
    if staged_err:
        return err(staged_err)

    final_argv = _normalize_bundle_argv(str_argv, bundle_id, cwd_s)
    if bundle_id and cwd_s:
        final_argv = _fixup_skill_relative_argv(final_argv, cwd_s)

    req = ExecutionRequest(
        argv=final_argv,
        cwd=cwd_s,
        timeout_seconds=timeout,
        bundle_id=bundle_id,
        **({"max_output_bytes": max_output_bytes} if max_output_bytes is not None else {}),
    )
    backend = (os.getenv("MOTET_EXEC_BACKEND") or "subprocess").strip().lower()
    if backend in ("docker", "container", "kata", "kata-fc") and bundle_id:
        motet = _get_motet_context_optional()
        if motet is not None and getattr(motet, "redis", None) is not None:
            from motet.core.execution.bundle_exec import merge_exec_catalog_into_request

            req = merge_exec_catalog_into_request(req, redis_client=motet.redis)
    out = _result_to_tool_dict(run_execution(req))
    out["effective_cwd"] = cwd_s
    out["cwd_generated"] = cwd_generated
    if bundle_id:
        out["bundle_id"] = bundle_id
    return out


def _run_workspace(
    *,
    str_argv: List[str],
    workspace_mode: str,
    workspace_image_stack_raw: Any,
    timeout: Optional[int],
    max_output_bytes: Optional[int],
    bundle_id: Optional[str],
    workspace_bundle_id: str,
    workspace_skill_name: str,
    input_files: List[ExecutionInputFile],
) -> Dict[str, Any]:
    """ADR-0106 dispatch path. Bundle staging is intentionally skipped.

    Authors that need bundle scripts inside ``/scratch`` should write them
    via a prior call (e.g. ``python -c "open('/scratch/foo.py','w').write(...)"``);
    integration with ``runners.yaml``-declared scripts now uses the internal
    ``workspace_materialized_files`` parameter so the manager can ship the files
    into the workspace container before exec.
    """
    if not isinstance(workspace_image_stack_raw, (str, type(None))):
        return err("workspace_image_stack must be a string when provided")
    image_stack = _resolve_workspace_image_stack(workspace_image_stack_raw)

    motet = _get_motet_context_optional()
    tenant_id = getattr(motet, "tenant_id", None) if motet is not None else None
    conversation_id = (
        getattr(motet, "conversation_id", None) if motet is not None else None
    )
    correlation_id = getattr(motet, "command_id", None) if motet is not None else None

    req_kwargs: Dict[str, Any] = dict(
        argv=list(str_argv),
        cwd="/scratch",
        timeout_seconds=timeout,
        bundle_id=bundle_id,
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        input_files=input_files,
    )
    if max_output_bytes is not None:
        req_kwargs["max_output_bytes"] = max_output_bytes
    req = ExecutionRequest(**req_kwargs)

    oci_image_ref = _resolve_workspace_image(image_stack)

    result = run_in_workspace(
        req,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        image_stack=image_stack,
        bundle_id=workspace_bundle_id,
        skill_name=workspace_skill_name,
        mode="cold",
        oci_image_ref=oci_image_ref,
    )
    out = _result_to_tool_dict(result)
    out["workspace_mode"] = workspace_mode
    out["workspace_image_stack"] = image_stack
    out["workspace_bundle_id"] = workspace_bundle_id
    out["workspace_skill_name"] = workspace_skill_name
    if conversation_id:
        out["workspace_conversation_id"] = conversation_id
    if bundle_id:
        out["bundle_id"] = bundle_id
    return out


def _coerce_workspace_input_files(raw: Any) -> List[ExecutionInputFile]:
    """Decode internal ``workspace_materialized_files`` payloads into models."""

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("workspace_materialized_files must be a list when provided")

    files: List[ExecutionInputFile] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"workspace_materialized_files[{idx}] must be an object")
        path = item.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError(
                f"workspace_materialized_files[{idx}].path must be a non-empty string"
            )

        content_raw = item.get("content")
        content_b64 = item.get("content_b64")
        content: bytes
        if isinstance(content_raw, bytes):
            content = content_raw
        elif isinstance(content_raw, str):
            content = content_raw.encode("utf-8")
        elif isinstance(content_b64, str):
            try:
                content = base64.b64decode(content_b64.encode("ascii"))
            except Exception as exc:
                raise ValueError(
                    f"workspace_materialized_files[{idx}].content_b64 is not valid base64: {exc}"
                ) from exc
        else:
            raise ValueError(
                f"workspace_materialized_files[{idx}] must provide content or content_b64"
            )

        mode_raw = item.get("mode", 0o600)
        try:
            mode = int(mode_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"workspace_materialized_files[{idx}].mode must be an integer"
            ) from exc

        files.append(ExecutionInputFile(path=path, content=content, mode=mode))
    return files


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.worker_exec",
        description=(
            "Run one-shot argv in the **worker** execution domain (no shell by default). "
            "Prefer explicit argv like [\"python3\", \"script.py\", \"input.pdf\"] rather than a single shell string. "
            "If shell features are required (pipes, &&, redirection, variable expansion), invoke a shell explicitly, "
            "for example [\"bash\", \"-lc\", \"python foo.py && python bar.py\"]. "
            "Requires MOTET_WORKER_EXEC_CWD_ALLOWLIST with allowed cwd prefixes. "
            "The tool always determines cwd from MOTET_WORKER_EXEC_DEFAULT_CWD_ROOT "
            "(or the first allowlist prefix) and generates a unique run directory. "
            "MOTET_EXEC_BACKEND=subprocess runs in-process; docker/kata/kata-fc run in a disposable "
            "container via Docker Engine (Kata uses HostConfig.Runtime; requires daemon + allowlisted cwd). "
            "Optional bundle_id: container backends may set the image from the bundle catalog exec block, and "
            "bundle-relative script paths such as `skills/<skill>/scripts/foo.py` are staged and executable. "
            "This is the fallback / escape hatch for bundle skill scripts when no `runners.yaml` exists or when a "
            "vendored script's CLI is too complex for generic runner registration. "
            "Optional workspace_mode='workspace': dispatch into a per-(tenant, conversation, bundle, skill, image_stack) "
            "container whose /scratch persists across calls for that skill in the conversation."
        ),
        func=run,
        tool_schema=Params,
        triggers=[],
        priority=3,
        estimate_tokens=lambda _: 20,
        parse_params=None,
        observation_formatter=_fmt,
        category="shell",
        # Programmatic callers (e.g. app-builder.run_tests) need intact
        # stdout/stderr/returncode; context truncation would drop the pytest log.
        contextualize_observation=False,
        required_capabilities=[
            "TOOL_EXECUTION",
            "WORKER_SHELL_EXEC",
        ],
    )


__all__ = ["register", "run"]
