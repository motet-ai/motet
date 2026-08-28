"""
Motet - Workspace shell execution tool for Agent Skills

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Provides the model-facing shell affordance for activated Agent
    Skills. The tool runs ``bash -lc <command>`` inside the per-skill workspace
    container, materializes activated skill files and selected Artifact Store
    inputs into ``/scratch``, and can persist declared output files back as
    artifacts.

Dependencies:
    - motet.core.skills.registry for resolving activated skill metadata
    - motet.core.tools.builtin.worker_exec for workspace-container dispatch
    - motet.core.artifacts for scoped input/output artifact materialization
    - Docker Engine archive APIs for output capture from workspace containers

Usage:
    The model receives this tool only after skill activation and calls it with
    a shell command documented by the skill's SKILL.md.

Notes:
    - The model never sees host paths. Skill files and input artifacts are
      copied into validated ``/scratch`` paths.
    - ``lifetime="stateful"`` runs through the existing warm
      supervisor with a Motet-owned shell dispatcher module.
"""

from __future__ import annotations

import io
import base64
import hashlib
import mimetypes
import os
import posixpath
import tarfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, model_validator

from motet.core.artifacts import ArtifactKind
from motet.core.distributed.workspace_container_registry import (
    DEFAULT_WORKSPACE_SCOPE_BUNDLE_ID,
)
from motet.core.execution import run_stateful_in_workspace
from motet.core.execution.image_stacks import (
    resolve_image_stack,
    resolve_image_stack_for_capabilities,
)
from motet.core.skills.assembly import find_skill_by_name_or_id
from motet.core.skills.registry import RegisteredSkill
from motet.core.tools.protocol import err, ok

from ..registry import ToolRegistry

_TOOL_NAME = "core.workspace_shell_exec"
_SCRATCH_PREFIX = "/scratch"
_DEFAULT_IMAGE_STACK = "python-minimal"
_STATEFUL_SCRIPT_LOGICAL_NAME = "motet_workspace_shell_dispatcher.py"
_OUTPUT_PREVIEW_MAX_BYTES = 4096
_BOOTSTRAP_ENV = "MOTET_WORKSPACE_SHELL_BOOTSTRAP_ENABLED"


def _default_artifact_path_from_filename(filename: str) -> str:
    leaf = posixpath.basename(str(filename or "").strip())
    if not leaf or leaf in {".", ".."}:
        raise ValueError("input_artifacts[].filename must include a filename")
    return f"{_SCRATCH_PREFIX}/{leaf}"


class ArtifactInput(BaseModel):
    artifact_id: str = Field(..., description="Artifact id visible to the current request.")
    path: Optional[str] = Field(
        default=None,
        description=(
            "Destination path under /scratch. If omitted, filename is used to create "
            "a default /scratch/<filename> path."
        ),
    )
    filename: Optional[str] = Field(
        default=None,
        description="Optional original filename used to infer path when path is omitted.",
    )

    @model_validator(mode="after")
    def _fill_path_from_filename(self) -> "ArtifactInput":
        if self.path:
            return self
        if self.filename:
            self.path = _default_artifact_path_from_filename(self.filename)
            return self
        raise ValueError("input_artifacts[] must include path or filename")


class Params(BaseModel):
    command: str = Field(
        ...,
        description="Shell command to run inside the activated skill workspace.",
    )
    skill_id: Optional[str] = Field(
        default=None,
        description="Activated skill id, e.g. 'skills-vendor-demo.pdf'.",
    )
    lifetime: str = Field(
        default="workspace",
        description="workspace (default) or stateful.",
    )
    input_artifacts: List[ArtifactInput] = Field(
        default_factory=list,
        description=(
            "Artifacts to materialize into /scratch before command execution. Each item "
            "may provide path explicitly, or filename to default to /scratch/<filename>."
        ),
    )
    output_paths: List[str] = Field(
        default_factory=list,
        description="Files under /scratch to persist back as artifacts after successful execution.",
    )
    timeout_seconds: Optional[int] = Field(
        default=None,
        description="Command timeout in seconds.",
    )
    image_stack: Optional[str] = Field(
        default=None,
        description="Workspace image stack. Defaults to platform config or python-minimal.",
    )


def _get_motet_context_optional() -> Any:
    try:
        from motet.core.commands.decorator import get_motet_context

        return get_motet_context()
    except Exception:
        return None


def _fmt(result: Dict[str, Any]) -> str:
    payload = result.get("result") if isinstance(result, dict) else None
    if isinstance(payload, dict):
        return (
            f"workspace_shell_exec(process_status={payload.get('process_status')}, "
            f"rc={payload.get('returncode')}, "
            f"outputs={len(payload.get('output_artifacts') or [])})"
        )
    if isinstance(result, dict) and result.get("error"):
        return f"workspace_shell_exec(error={result.get('error')})"
    return "workspace_shell_exec"


def _resolve_skill(params: Dict[str, Any], motet: Any) -> Optional[RegisteredSkill]:
    skill_id = str(params.get("skill_id") or "").strip()
    if skill_id:
        return find_skill_by_name_or_id(skill_id=skill_id)

    metadata = getattr(motet, "metadata", None) if motet is not None else None
    if isinstance(metadata, dict):
        refs = metadata.get("skill_refs") or []
        if isinstance(refs, list) and len(refs) == 1 and isinstance(refs[0], dict):
            inferred = str(refs[0].get("skill_id") or "").strip()
            if inferred:
                return find_skill_by_name_or_id(skill_id=inferred)
    return None


def _normalize_scratch_path(raw: str, *, field_name: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if "\x00" in raw:
        raise ValueError(f"{field_name} must not contain null bytes")

    token = raw.strip()
    if not token.startswith("/"):
        token = posixpath.join(_SCRATCH_PREFIX, token)
    normalized = posixpath.normpath(token)
    if normalized == _SCRATCH_PREFIX or not normalized.startswith(f"{_SCRATCH_PREFIX}/"):
        raise ValueError(f"{field_name} must resolve under /scratch")
    return normalized


def _payload_to_bytes(payload: Any) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, bytearray):
        return bytes(payload)
    if isinstance(payload, str):
        return payload.encode("utf-8")
    import json

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")


def _skill_materialized_files(rec: RegisteredSkill) -> List[Dict[str, Any]]:
    root = rec.skill_md_path.parent
    files: List[Dict[str, Any]] = []
    try:
        paths = sorted(path for path in root.rglob("*") if path.is_file())
    except Exception:
        return files

    for path in paths:
        try:
            rel = path.relative_to(root).as_posix()
            content = path.read_bytes()
        except OSError:
            continue
        files.append(
            {
                "path": f"{_SCRATCH_PREFIX}/skills/{rec.name}/{rel}",
                "content": content,
                "mode": 0o600,
            }
        )
    return files


def _safe_bundle_relative_path(raw: Any) -> Optional[str]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    token = raw.strip().replace("\\", "/")
    if token.startswith("/") or "\x00" in token:
        return None
    parts = [part for part in token.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def _bundle_root_for_skill(rec: RegisteredSkill) -> Optional[Path]:
    skill_dir = rec.skill_md_path.parent
    skills_dir = skill_dir.parent
    if skills_dir.name != "skills":
        return None
    return skills_dir.parent


def _read_bundle_exec_config(bundle_root: Path) -> Dict[str, Any]:
    for name in ("exec.yaml", "exec.yml"):
        path = bundle_root / "config" / name
        if not path.is_file():
            continue
        try:
            import yaml  # type: ignore[import]

            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _exec_config_for_skill(rec: RegisteredSkill) -> Dict[str, Any]:
    bundle_root = _bundle_root_for_skill(rec)
    if bundle_root is None:
        return {}
    return _read_bundle_exec_config(bundle_root)


def _requirements_materialized_file(
    rec: RegisteredSkill,
    exec_config: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    bundle_root = _bundle_root_for_skill(rec)
    if bundle_root is None:
        return None
    req_path = _safe_bundle_relative_path(
        (exec_config or _read_bundle_exec_config(bundle_root)).get("requirements_path")
    )
    if not req_path:
        return None
    source = bundle_root / req_path
    try:
        content = source.read_bytes()
    except OSError:
        return None
    return {
        "path": f"{_SCRATCH_PREFIX}/{req_path}",
        "content": content,
        "mode": 0o600,
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _artifact_materialized_files(
    artifact_store: Any,
    inputs: List[ArtifactInput],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[str]]:
    files: List[Dict[str, Any]] = []
    materialized: List[Dict[str, Any]] = []
    for item in inputs:
        try:
            dest = _normalize_scratch_path(str(item.path or ""), field_name="input_artifacts[].path")
        except ValueError as exc:
            return [], [], str(exc)

        meta = artifact_store.get_metadata(item.artifact_id)
        if meta is None:
            return [], [], f"artifact not found or not visible: {item.artifact_id}"
        payload = artifact_store.get(item.artifact_id)
        if payload is None:
            return [], [], f"artifact payload missing or not visible: {item.artifact_id}"
        content = _payload_to_bytes(payload)
        files.append({"path": dest, "content": content, "mode": 0o600})
        materialized.append(
            {
                "artifact_id": item.artifact_id,
                "path": dest,
                "bytes": len(content),
                "content_type": getattr(meta, "content_type", None),
            }
        )
    return files, materialized, None


def _resolve_image_stack(raw: Optional[str]) -> str:
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    env_default = (os.getenv("MOTET_WORKSPACE_CONTAINER_DEFAULT_IMAGE_STACK") or "").strip()
    return env_default or _DEFAULT_IMAGE_STACK


def _runtime_capabilities(raw: Any) -> Tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    out: List[str] = []
    seen: set[str] = set()
    for item in raw:
        token = str(item or "").strip().lower().replace("_", "-")
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return tuple(out)


def _resolve_runtime_image_stack(
    *,
    explicit_image_stack: Any,
    exec_config: Dict[str, Any],
) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
    if isinstance(explicit_image_stack, str) and explicit_image_stack.strip():
        stack = explicit_image_stack.strip()
        return (
            stack,
            {
                "source": "tool_param",
                "image_stack": stack,
                "runtime_capabilities": [],
            },
            None,
        )

    configured_stack = exec_config.get("base_image_stack")
    if isinstance(configured_stack, str) and configured_stack.strip():
        stack = configured_stack.strip()
        return (
            stack,
            {
                "source": "config/exec.yaml:base_image_stack",
                "image_stack": stack,
                "runtime_capabilities": _runtime_capabilities(exec_config.get("runtime_capabilities")),
            },
            None,
        )

    required = _runtime_capabilities(exec_config.get("runtime_capabilities"))
    if required:
        resolution = resolve_image_stack_for_capabilities(required)
        payload: Dict[str, Any] = {
            "source": "config/exec.yaml:runtime_capabilities",
            "runtime_capabilities": list(required),
            "matched": resolution.matched,
            "missing_capabilities": list(resolution.missing_capabilities),
        }
        if resolution.stack is not None:
            payload["image_stack"] = resolution.stack.name
            payload["oci_image_ref"] = resolution.stack.oci_image_ref
        if not resolution.matched or resolution.stack is None:
            return (
                None,
                payload,
                (
                    "No pinned image stack satisfies runtime_capabilities "
                    f"{list(required)}. Configure MOTET_IMAGE_STACK_<NAME> and "
                    "MOTET_IMAGE_STACK_<NAME>_CAPABILITIES, or set base_image_stack."
                ),
            )
        return resolution.stack.name, payload, None

    stack = _resolve_image_stack(None)
    return (
        stack,
        {
            "source": "default",
            "image_stack": stack,
            "runtime_capabilities": [],
        },
        None,
    )


def _coerce_lifetime(raw: Any) -> str:
    token = str(raw or "workspace").strip().lower()
    if token in ("", "workspace"):
        return "workspace"
    if token == "stateful":
        return "stateful"
    raise ValueError("lifetime must be 'workspace' or 'stateful'")


def _source_artifact_id(inputs: List[ArtifactInput]) -> Optional[str]:
    return inputs[0].artifact_id if len(inputs) == 1 else None


def _process_status(result: Dict[str, Any]) -> str:
    if bool(result.get("timed_out")):
        return "timed_out"
    try:
        returncode = int(result.get("returncode", -1))
    except (TypeError, ValueError):
        return "failed"
    return "succeeded" if returncode == 0 else "failed"


def _command_succeeded(result: Dict[str, Any]) -> bool:
    return _process_status(result) == "succeeded"


def _text_preview(content: bytes, content_type: str) -> Dict[str, Any]:
    is_text_like = (
        content_type.startswith("text/")
        or content_type in {"application/json", "application/xml"}
        or content_type.endswith("+json")
        or content_type.endswith("+xml")
    )
    if not is_text_like:
        return {}
    preview_bytes = content[:_OUTPUT_PREVIEW_MAX_BYTES]
    try:
        preview = preview_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    return {
        "preview": preview,
        "preview_truncated": len(content) > len(preview_bytes),
    }


def _read_output_from_workspace(
    *,
    tenant_id: str,
    conversation_id: str,
    bundle_id: str,
    skill_name: str,
    image_stack: str,
    path: str,
) -> Tuple[Optional[bytes], Optional[str]]:
    from motet.core.execution import docker_client
    from motet.core.execution.workspace_container_manager import get_workspace_container_manager

    manager = get_workspace_container_manager()
    binding = manager.registry.lookup(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        bundle_id=bundle_id,
        skill_name=skill_name,
        image_stack=image_stack,
    )
    if binding is None:
        return None, "workspace container binding not found"

    sock_path, sock_err = docker_client.docker_socket_path()
    if sock_err:
        return None, sock_err
    assert sock_path is not None
    prefix = docker_client.api_prefix()
    status, body = docker_client.docker_get_archive(
        sock_path,
        prefix,
        binding.container_id,
        path=path,
    )
    if status != 200:
        return None, docker_client.daemon_error(status, body)

    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:*") as tf:
            for member in tf.getmembers():
                if member.isfile():
                    extracted = tf.extractfile(member)
                    if extracted is None:
                        continue
                    return extracted.read(), None
    except tarfile.TarError as exc:
        return None, f"failed to read output archive: {exc}"
    return None, "output path did not contain a regular file"


def _capture_output_artifacts(
    *,
    artifact_store: Any,
    output_paths: List[str],
    params_inputs: List[ArtifactInput],
    rec: RegisteredSkill,
    image_stack: str,
    motet: Any,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    tenant_id = str(getattr(motet, "tenant_id", "") or "")
    conversation_id = str(getattr(motet, "conversation_id", "") or "")
    if not tenant_id or not conversation_id:
        return [], [{"path": "", "error": "tenant_id and conversation_id are required for output capture"}]

    output_artifacts: List[Dict[str, Any]] = []
    output_errors: List[Dict[str, str]] = []
    bundle_id = rec.bundle_id or DEFAULT_WORKSPACE_SCOPE_BUNDLE_ID
    source_id = _source_artifact_id(params_inputs)
    source_ids = [item.artifact_id for item in params_inputs]
    for raw_path in output_paths:
        try:
            path = _normalize_scratch_path(raw_path, field_name="output_paths[]")
        except ValueError as exc:
            output_errors.append({"path": str(raw_path), "error": str(exc)})
            continue
        content, read_error = _read_output_from_workspace(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            bundle_id=bundle_id,
            skill_name=rec.name,
            image_stack=image_stack,
            path=path,
        )
        if read_error or content is None:
            output_errors.append({"path": path, "error": read_error or "empty output"})
            continue

        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        metadata = {
            "path": path,
            "filename": posixpath.basename(path),
            "skill_id": rec.skill_id,
            "bundle_id": rec.bundle_id,
            "source_artifact_ids": source_ids,
            "tool_name": _TOOL_NAME,
            "conversation_id": conversation_id,
            "command_id": getattr(motet, "command_id", None),
        }
        artifact_id = artifact_store.put(
            payload=content,
            content_type=content_type,
            kind=ArtifactKind.TOOL_ARTIFACT,
            source_artifact_id=source_id,
            metadata=metadata,
        )
        output_artifacts.append(
            {
                "artifact_id": artifact_id,
                "path": path,
                "content_type": content_type,
                "bytes": len(content),
                **_text_preview(content, content_type),
            }
        )
    return output_artifacts, output_errors


def _workspace_files_for_stateful_params(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in files:
        content = item.get("content")
        if isinstance(content, str):
            content_bytes = content.encode("utf-8")
        elif isinstance(content, bytearray):
            content_bytes = bytes(content)
        elif isinstance(content, bytes):
            content_bytes = content
        else:
            content_bytes = _payload_to_bytes(content)
        out.append(
            {
                "path": str(item.get("path") or ""),
                "content_b64": base64.b64encode(content_bytes).decode("ascii"),
                "mode": int(item.get("mode", 0o600)),
            }
        )
    return out


def _resolve_workspace_image(image_stack: str) -> Optional[str]:
    stack = resolve_image_stack(image_stack)
    if stack is None or not stack.is_pinned:
        return None
    return stack.oci_image_ref


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _command_with_requirements_setup(
    command: str,
    requirements_file: Optional[Dict[str, Any]],
) -> str:
    if not requirements_file:
        return command
    req_path = str(requirements_file.get("path") or "")
    digest = str(requirements_file.get("sha256") or "")
    if not req_path or not digest:
        return command
    sentinel = f"{_SCRATCH_PREFIX}/.motet/requirements-{digest}.installed"
    log_path = f"{_SCRATCH_PREFIX}/.motet/requirements-{digest}.log"
    setup = (
        "mkdir -p /scratch/.motet && "
        f"if [ ! -f {_shell_quote(sentinel)} ]; then "
        f"echo '[workspace_shell_exec] installing Python requirements from {req_path}' >&2; "
        "if "
        f"python3 -m pip install --disable-pip-version-check -r {_shell_quote(req_path)} "
        f"> {_shell_quote(log_path)} 2>&1; then "
        f"touch {_shell_quote(sentinel)}; "
        "else "
        "echo '[workspace_shell_exec] Python requirements install failed; pip log follows:' >&2; "
        f"cat {_shell_quote(log_path)} >&2; "
        "exit 1; "
        "fi; "
        "fi"
    )
    return f"{setup} && {command}"


def _bootstrap_enabled() -> bool:
    raw = os.getenv(_BOOTSTRAP_ENV, "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _bootstrap_command(exec_config: Dict[str, Any]) -> Optional[str]:
    raw = exec_config.get("bootstrap_command")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()


def _command_with_bootstrap_setup(
    command: str,
    bootstrap_command: Optional[str],
) -> str:
    if not bootstrap_command or not _bootstrap_enabled():
        return command
    log_path = f"{_SCRATCH_PREFIX}/.motet/bootstrap.log"
    setup = (
        "mkdir -p /scratch/.motet && "
        "echo '[workspace_shell_exec] running dev-only bootstrap_command' >&2; "
        "if "
        f"( {bootstrap_command} ) > {_shell_quote(log_path)} 2>&1; then "
        "true; "
        "else "
        "echo '[workspace_shell_exec] bootstrap_command failed; log follows:' >&2; "
        f"cat {_shell_quote(log_path)} >&2; "
        "exit 1; "
        "fi"
    )
    return f"{setup} && {command}"


def _bootstrap_setup_metadata(bootstrap_command: str) -> Dict[str, Any]:
    enabled = _bootstrap_enabled()
    return {
        "type": "bootstrap_command",
        "enabled": enabled,
        "env_flag": _BOOTSTRAP_ENV,
        "log_path": f"{_SCRATCH_PREFIX}/.motet/bootstrap.log",
        "command": bootstrap_command if enabled else None,
        "ignored_reason": None if enabled else f"{_BOOTSTRAP_ENV} is not enabled",
    }


def _requirements_setup_metadata(requirements_file: Dict[str, Any]) -> Dict[str, Any]:
    digest = str(requirements_file.get("sha256") or "")
    return {
        "type": "python_requirements",
        "requirements_path": requirements_file.get("path"),
        "sentinel_path": f"{_SCRATCH_PREFIX}/.motet/requirements-{digest}.installed",
        "log_path": f"{_SCRATCH_PREFIX}/.motet/requirements-{digest}.log",
        "success_output": "pip output is written to log_path to keep command stdout focused.",
    }


def _stateful_shell_dispatcher_source() -> bytes:
    return b'''\
import base64
import json
import os
import posixpath
import subprocess

SCRATCH = "/scratch"
CALL_COUNT = 0


def _normalize_scratch_path(raw):
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("workspace materialized file path must be a non-empty string")
    token = raw.strip()
    if not token.startswith("/"):
        token = posixpath.join(SCRATCH, token)
    normalized = posixpath.normpath(token)
    if normalized == SCRATCH or not normalized.startswith(SCRATCH + "/"):
        raise ValueError("workspace materialized file path must resolve under /scratch")
    return normalized


def _materialize(files):
    materialized = []
    for item in files or []:
        path = _normalize_scratch_path(item.get("path"))
        content_b64 = item.get("content_b64")
        if not isinstance(content_b64, str):
            raise ValueError("workspace materialized file must provide content_b64")
        content = base64.b64decode(content_b64.encode("ascii"))
        mode = int(item.get("mode", 0o600))
        os.makedirs(posixpath.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content)
        os.chmod(path, mode)
        materialized.append({"path": path, "bytes": len(content), "mode": mode})
    return materialized


def handle(params):
    global CALL_COUNT
    CALL_COUNT += 1

    command = params.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string")
    if "\\x00" in command:
        raise ValueError("command must not contain null bytes")

    materialized = _materialize(params.get("workspace_materialized_files") or [])
    timeout = params.get("timeout_seconds")
    if timeout is not None:
        timeout = int(timeout)

    completed = subprocess.run(
        ["bash", "-lc", command],
        cwd=SCRATCH,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return {
        "returncode": int(completed.returncode),
        "stdout": completed.stdout or "",
        "stderr": completed.stderr or "",
        "timed_out": False,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "materialized_files": materialized,
        "stateful_call_count": CALL_COUNT,
    }
'''


def _result_to_stateful_tool_dict(envelope: Dict[str, Any]) -> Dict[str, Any]:
    result = envelope.get("result")
    if not bool(envelope.get("ok")):
        return {
            "returncode": -1,
            "stdout": str(envelope.get("stdout") or ""),
            "stderr": str(envelope.get("stderr") or ""),
            "timed_out": bool(envelope.get("timed_out", False)),
            "error": str(envelope.get("error") or "stateful workspace shell execution failed"),
            "traceback": str(envelope.get("traceback") or ""),
            "workspace_mode": str(envelope.get("workspace_mode") or "stateful"),
            "transport_error": bool(envelope.get("transport_error", False)),
        }
    if isinstance(result, dict):
        out = dict(result)
    else:
        out = {"returncode": 0, "stdout": "", "stderr": "", "result": result}
    out.setdefault("returncode", 0)
    out.setdefault("stdout", str(envelope.get("stdout") or ""))
    out.setdefault("stderr", str(envelope.get("stderr") or ""))
    out.setdefault("timed_out", bool(envelope.get("timed_out", False)))
    out.setdefault("stdout_truncated", False)
    out.setdefault("stderr_truncated", False)
    out["workspace_mode"] = str(envelope.get("workspace_mode") or "stateful")
    if envelope.get("container_id"):
        out["backend_ref"] = envelope.get("container_id")
    if envelope.get("oci_image_ref"):
        out["oci_image_ref"] = envelope.get("oci_image_ref")
    for key in ("downgraded_from", "downgraded_to", "downgraded_reason"):
        if key in envelope:
            out[key] = envelope[key]
    return out


def _run_workspace_lifetime(
    *,
    command: str,
    timeout_seconds: Any,
    image_stack: str,
    bundle_id: str,
    skill_name: str,
    workspace_files: List[Dict[str, Any]],
) -> Dict[str, Any]:
    from motet.core.tools.builtin.worker_exec import run as worker_exec_run

    worker_params: Dict[str, Any] = {
        "argv": ["bash", "-lc", command],
        "bundle_id": bundle_id,
        "workspace_mode": "workspace",
        "workspace_image_stack": image_stack,
        "workspace_bundle_id": bundle_id,
        "workspace_skill_name": skill_name,
        "workspace_materialized_files": workspace_files,
    }
    if timeout_seconds is not None:
        worker_params["timeout_seconds"] = timeout_seconds
    return worker_exec_run(worker_params)


def _run_stateful_lifetime(
    *,
    command: str,
    timeout_seconds: Any,
    image_stack: str,
    bundle_id: str,
    skill_name: str,
    workspace_files: List[Dict[str, Any]],
    motet: Any,
) -> Dict[str, Any]:
    envelope = run_stateful_in_workspace(
        tenant_id=getattr(motet, "tenant_id", None),
        conversation_id=getattr(motet, "conversation_id", None),
        image_stack=image_stack,
        oci_image_ref=_resolve_workspace_image(image_stack),
        bundle_id=bundle_id,
        skill_name=skill_name,
        script_source=_stateful_shell_dispatcher_source(),
        script_logical_name=_STATEFUL_SCRIPT_LOGICAL_NAME,
        params={
            "command": command,
            "timeout_seconds": timeout_seconds,
            "workspace_materialized_files": _workspace_files_for_stateful_params(workspace_files),
        },
        timeout_seconds=timeout_seconds,
        request_id=getattr(motet, "command_id", None),
    )
    return _result_to_stateful_tool_dict(envelope)


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    command = params.get("command")
    if not isinstance(command, str) or not command.strip():
        return err("command must be a non-empty string")
    if "\x00" in command:
        return err("command must not contain null bytes")

    try:
        effective_lifetime = _coerce_lifetime(params.get("lifetime"))
    except ValueError as exc:
        return err(str(exc))

    motet = _get_motet_context_optional()
    if motet is None:
        return err("workspace shell execution requires a Motet command context")

    rec = _resolve_skill(params, motet)
    if rec is None:
        return err("activated skill not found; pass skill_id or activate one skill first")

    artifact_store = getattr(motet, "artifact_store", None)
    if artifact_store is None:
        return err("artifact store is unavailable in this command context")

    exec_config = _exec_config_for_skill(rec)
    image_stack, runtime_resolution, runtime_error = _resolve_runtime_image_stack(
        explicit_image_stack=params.get("image_stack"),
        exec_config=exec_config,
    )
    if runtime_error or image_stack is None:
        return err(runtime_error or "unable to resolve workspace image stack")

    try:
        input_models = [
            item if isinstance(item, ArtifactInput) else ArtifactInput(**item)
            for item in list(params.get("input_artifacts") or [])
        ]
        output_paths = [
            _normalize_scratch_path(str(path), field_name="output_paths[]")
            for path in list(params.get("output_paths") or [])
        ]
    except Exception as exc:
        return err(str(exc))

    artifact_files, materialized_inputs, artifact_error = _artifact_materialized_files(
        artifact_store,
        input_models,
    )
    if artifact_error:
        return err(artifact_error)

    requirements_file = _requirements_materialized_file(rec, exec_config)
    bootstrap_command = _bootstrap_command(exec_config)
    effective_command = _command_with_requirements_setup(command, requirements_file)
    effective_command = _command_with_bootstrap_setup(effective_command, bootstrap_command)
    bundle_id = rec.bundle_id or DEFAULT_WORKSPACE_SCOPE_BUNDLE_ID
    workspace_files = _skill_materialized_files(rec) + artifact_files
    if requirements_file:
        workspace_files.append(requirements_file)

    if effective_lifetime == "stateful":
        result = _run_stateful_lifetime(
            command=effective_command,
            timeout_seconds=params.get("timeout_seconds"),
            image_stack=image_stack,
            bundle_id=bundle_id,
            skill_name=rec.name,
            workspace_files=workspace_files,
            motet=motet,
        )
    else:
        result = _run_workspace_lifetime(
            command=effective_command,
            timeout_seconds=params.get("timeout_seconds"),
            image_stack=image_stack,
            bundle_id=bundle_id,
            skill_name=rec.name,
            workspace_files=workspace_files,
        )
    result["skill_id"] = rec.skill_id
    result["skill_directory"] = f"{_SCRATCH_PREFIX}/skills/{rec.name}"
    result["effective_lifetime"] = effective_lifetime
    result["materialized_inputs"] = materialized_inputs
    result["runtime_resolution"] = runtime_resolution
    result["process_status"] = _process_status(result)
    if bootstrap_command:
        result["bootstrap"] = _bootstrap_setup_metadata(bootstrap_command)
    if requirements_file:
        result["requirements_path"] = requirements_file["path"]
        result["setup"] = _requirements_setup_metadata(requirements_file)

    output_artifacts: List[Dict[str, Any]] = []
    output_errors: List[Dict[str, str]] = []
    command_succeeded = _command_succeeded(result)
    if command_succeeded and output_paths:
        output_artifacts, output_errors = _capture_output_artifacts(
            artifact_store=artifact_store,
            output_paths=output_paths,
            params_inputs=input_models,
            rec=rec,
            image_stack=image_stack,
            motet=motet,
        )
    result["output_artifacts"] = output_artifacts
    if output_errors:
        result["output_errors"] = output_errors
    if output_paths and not output_artifacts and not command_succeeded:
        result["output_capture_skipped"] = "command did not succeed"

    return ok(result)


def register(registry: ToolRegistry) -> None:
    registry.register(
        name=_TOOL_NAME,
        description=(
            "Run a shell command inside an activated Agent Skill workspace container. "
            "Use this after core.activate_skill when SKILL.md instructs you to run bundled scripts. "
            "Input artifacts can be materialized into /scratch using path or filename; declared output paths "
            "can be saved back as artifacts with text previews. Check process_status and returncode after each call."
        ),
        func=run,
        tool_schema=Params,
        category="shell",
        keywords=["skill", "workspace", "shell", "bash", "artifact"],
        observation_formatter=_fmt,
        contextualize_observation=True,
        required_capabilities=["TOOL_EXECUTION", "WORKER_SHELL_EXEC"],
    )


__all__ = ["register", "run"]
