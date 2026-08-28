"""
Motet - Workflow Builder Pipeline

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Shared author → validate → (execute | register | unregister) pipeline for
    LLM- and API-authored workflow definitions. Used by ``core.workflow_builder``
    and the HTTP twins under ``/api/v1/workflows``. Reuses ``Workflow.from_dict``,
    ``validate_workflow``, and ``WorkflowRegistry``, but fails closed on malformed
    steps (unlike bare ``from_dict``) and enforces command/tool allowlists plus
    the ``user.`` id namespace (user/agent/API-authored, not core/bundle).
    Execute mode surfaces step-level failures (not only a generic message).

Dependencies:
    - yaml: Safe load of workflow documents
    - motet.core.workflow: Workflow models, validate_workflow, WorkflowRegistry
    - motet.core.tools.meta_tool_policy: ToolFilter disclosure checks
    - motet.core.tools.registry: Tool existence / category lookup

Usage:
    from motet.core.workflow.builder import run_workflow_builder

    result = run_workflow_builder(
        mode="validate",
        yaml_text=yaml_doc,
        scope_slug="acme",
    )

Notes:
    - Register persists to Redis (``{tenant}:user_wf:{id}``) and fans
      out via ``core.sync_user_workflow``; workers also hydrate on startup.
      List / invoke of ``user.*`` is fail-closed on caller tenant (#234).
    - Unregister only allows ``user.*`` ids owned by the calling principal
      (when ownership metadata is present); core/bundle namespaces are denied.
    - Command allowlist: ``core.tool_execution``, ``core.transform``,
      ``core.workflow_execution``. Structural rules for foreach/until/handback/
      elicitation come from ``validate_workflow``.
    - Id shape: ``user.<owner>.<local>`` (owner = tenant/principal slug).
    - ``mode=export`` returns bundle-shaped YAML for promote-to-bundle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger(__name__)

BUILDER_MODES = frozenset(
    {"validate", "execute", "register", "unregister", "export"}
)

# Origin namespace for builder/API-authored workflows (not core / not bundle).
AUTHORING_NAMESPACE = "user"

ALLOWED_COMMAND_TYPES = frozenset(
    {
        "tool_execution",
        "core.tool_execution",
        "transform",
        "core.transform",
        "workflow_execution",
        "core.workflow_execution",
    }
)

MAX_YAML_BYTES = 64_000
MAX_STEPS = 50
MAX_LOCAL_NAME_LEN = 64
_LOCAL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SCOPE_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


@dataclass
class BuilderError:
    """Structured, repairable validation error."""

    path: str
    code: str
    message: str

    def to_dict(self) -> Dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


@dataclass
class BuilderResult:
    """Outcome of a builder mode."""

    ok: bool
    mode: str
    errors: List[BuilderError] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "ok": self.ok,
            "mode": self.mode,
            "errors": [e.to_dict() for e in self.errors],
        }
        out.update(self.data)
        return out


def sanitize_scope_slug(raw: Optional[str], *, fallback: str = "default") -> str:
    """Normalize a tenant/principal slug for ``user.<slug>.…`` ids."""
    token = (raw or "").strip().lower().replace(" ", "_").replace(".", "_")
    token = re.sub(r"[^a-z0-9_-]", "", token)
    if not token or not _SCOPE_SLUG_RE.match(token):
        return fallback
    return token[:64]


def ensure_user_namespace(
    workflow_id: str,
    *,
    scope_slug: str,
    override_id: Optional[str] = None,
) -> Tuple[Optional[str], List[BuilderError]]:
    """
    Force a ``user.<owner>.<local>`` workflow id.

    Returns ``(namespaced_id, errors)``.
    """
    errors: List[BuilderError] = []
    candidate = (override_id or workflow_id or "").strip()
    if not candidate:
        errors.append(
            BuilderError(
                path="workflow_id",
                code="missing_workflow_id",
                message="workflow_id is required",
            )
        )
        return None, errors

    slug = sanitize_scope_slug(scope_slug)
    prefix = f"{AUTHORING_NAMESPACE}."
    if candidate.startswith(prefix):
        parts = candidate.split(".")
        if len(parts) < 3:
            errors.append(
                BuilderError(
                    path="workflow_id",
                    code="invalid_user_id",
                    message=(
                        f"workflow_id {candidate!r} must be "
                        f"{AUTHORING_NAMESPACE}.<owner>.<local_name> "
                        "(at least three dot-separated segments)"
                    ),
                )
            )
            return None, errors
        local = parts[-1]
        # Keep author-provided full user id, but require local name shape.
    else:
        # Bare or other-qualified ids: take the last segment as local name.
        local = candidate.rsplit(".", 1)[-1]
        candidate = f"{AUTHORING_NAMESPACE}.{slug}.{local}"

    if not _LOCAL_NAME_RE.match(local) or len(local) > MAX_LOCAL_NAME_LEN:
        errors.append(
            BuilderError(
                path="workflow_id",
                code="invalid_local_name",
                message=(
                    f"local workflow name {local!r} must match "
                    f"[a-z][a-z0-9_]* and be <= {MAX_LOCAL_NAME_LEN} chars"
                ),
            )
        )
        return None, errors

    if not candidate.startswith(prefix):
        errors.append(
            BuilderError(
                path="workflow_id",
                code="namespace_required",
                message=(
                    f"builder-authored workflows must use the "
                    f"{AUTHORING_NAMESPACE}. namespace"
                ),
            )
        )
        return None, errors

    return candidate, errors


def _error(path: str, code: str, message: str) -> BuilderError:
    return BuilderError(path=path, code=code, message=message)


def parse_workflow_document(
    *,
    yaml_text: Optional[str] = None,
    workflow_dict: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], List[BuilderError]]:
    """Parse YAML string or accept a structured dict. Fail closed on bad input."""
    errors: List[BuilderError] = []
    if yaml_text is not None and workflow_dict is not None:
        errors.append(
            _error(
                "document",
                "ambiguous_document",
                "provide either yaml or workflow object, not both",
            )
        )
        return None, errors

    if yaml_text is None and workflow_dict is None:
        errors.append(
            _error(
                "document",
                "missing_document",
                "yaml or workflow object is required",
            )
        )
        return None, errors

    if yaml_text is not None:
        if not isinstance(yaml_text, str) or not yaml_text.strip():
            errors.append(_error("yaml", "empty_yaml", "yaml document is empty"))
            return None, errors
        encoded = yaml_text.encode("utf-8")
        if len(encoded) > MAX_YAML_BYTES:
            errors.append(
                _error(
                    "yaml",
                    "yaml_too_large",
                    f"yaml exceeds {MAX_YAML_BYTES} bytes",
                )
            )
            return None, errors
        try:
            import yaml  # type: ignore[import]

            raw = yaml.safe_load(yaml_text)
        except Exception as exc:
            errors.append(
                _error("yaml", "yaml_parse_error", f"failed to parse yaml: {exc}")
            )
            return None, errors
        if not isinstance(raw, dict):
            errors.append(
                _error(
                    "yaml",
                    "yaml_not_mapping",
                    "yaml root must be a mapping/object",
                )
            )
            return None, errors
        return raw, errors

    assert workflow_dict is not None
    if not isinstance(workflow_dict, dict):
        errors.append(
            _error(
                "workflow",
                "workflow_not_mapping",
                "workflow must be a JSON/dict object",
            )
        )
        return None, errors
    return dict(workflow_dict), errors


def _expected_step_ids(steps_in: Any) -> List[str]:
    if isinstance(steps_in, dict):
        return [str(k) for k in steps_in.keys()]
    if isinstance(steps_in, list):
        out: List[str] = []
        for i, item in enumerate(steps_in):
            if isinstance(item, dict):
                sid = item.get("step_id") or item.get("id")
                if sid:
                    out.append(str(sid))
                else:
                    out.append(f"[{i}]")
            else:
                out.append(f"[{i}]")
        return out
    return []


def _normalize_step_dict(sdata: Dict[str, Any], *, sid_hint: str) -> Tuple[str, Dict[str, Any], List[BuilderError]]:
    """
    Normalize one step mapping before WorkflowStep.from_dict.

    Accepts ``id`` as an alias for ``step_id``. Rejects common non-Motet shapes
    (``tool``/``params`` instead of ``command_type``/``command_data``) with
    repairable errors.
    """
    errors: List[BuilderError] = []
    out = dict(sdata)
    if not out.get("step_id") and out.get("id"):
        out["step_id"] = out["id"]
    sid = str(out.get("step_id") or sid_hint)
    out["step_id"] = sid

    has_command_type = bool(out.get("command_type"))
    looks_foreign = any(k in out for k in ("tool", "params", "parameters")) and not has_command_type
    if looks_foreign:
        errors.append(
            _error(
                f"steps.{sid}",
                "invalid_step_shape",
                (
                    "steps must use Motet fields: command_type (e.g. core.tool_execution) "
                    "and command_data (object). Do not use tool:/params:. Prefer a map "
                    "under steps keyed by step_id so placeholders like {{step_id.result}} match."
                ),
            )
        )
    return sid, out, errors


def _failed_step_summaries(step_results: Any) -> Dict[str, str]:
    """Return ``{step_id: error_message}`` for failed steps in an execution result."""
    if not isinstance(step_results, dict):
        return {}
    failed: Dict[str, str] = {}
    for step_id, payload in step_results.items():
        if not isinstance(payload, dict):
            continue
        status = str(payload.get("status") or "").lower()
        if status in ("failed", "error", "timeout"):
            err = payload.get("error") or payload.get("message") or status
            failed[str(step_id)] = str(err)
        elif payload.get("error") and status not in ("success", "completed", "ok"):
            failed[str(step_id)] = str(payload.get("error"))
    return failed


def _format_command_execution_error(exc: Any) -> Tuple[str, Dict[str, Any]]:
    """Build a repairable message + details payload from CommandExecutionError."""
    message = str(getattr(exc, "message", None) or exc)
    details = getattr(exc, "details", None)
    details_dict = dict(details) if isinstance(details, dict) else {}
    step_results = details_dict.get("step_results")
    if step_results is None and isinstance(details_dict.get("data"), dict):
        step_results = details_dict["data"].get("step_results")
    failed = _failed_step_summaries(step_results)
    if failed:
        parts = [f"{sid}: {err}" for sid, err in failed.items()]
        message = f"{message}; step failures: " + "; ".join(parts)
    elif details_dict and message in ("Command execution failed", "Command execution failed:"):
        # Generic message with no step map — include a compact details hint.
        hint = details_dict.get("message") or details_dict.get("error")
        if hint:
            message = f"{message}: {hint}"
        else:
            message = (
                f"{message} (see execution_error.details / step_results in builder meta)"
            )
    payload: Dict[str, Any] = {
        "error_type": getattr(exc, "error_type", None),
        "message": str(getattr(exc, "message", None) or exc),
        "details": details_dict,
        "command_type": getattr(exc, "command_type", None),
        "command_id": getattr(exc, "command_id", None),
    }
    if failed:
        payload["failed_steps"] = failed
    if step_results is not None:
        payload["step_results"] = step_results
    return message, payload


def parse_workflow_strict(
    raw: Dict[str, Any],
    *,
    workflow_id: str,
) -> Tuple[Optional[Any], List[BuilderError]]:
    """
    Build a Workflow, rejecting malformed steps instead of silently skipping.
    """
    from motet.core.workflow import Workflow, WorkflowStep

    errors: List[BuilderError] = []
    data = dict(raw)
    data["workflow_id"] = workflow_id
    if not data.get("name"):
        data["name"] = workflow_id

    required_inputs = data.get("required_inputs")
    if required_inputs is not None and not isinstance(required_inputs, list):
        errors.append(
            _error(
                "required_inputs",
                "invalid_required_inputs",
                (
                    "required_inputs must be a list of input name strings "
                    "(e.g. [query]), not a schema object. Put JSON schemas under "
                    "input_parameters."
                ),
            )
        )
        return None, errors
    if isinstance(required_inputs, list):
        for i, item in enumerate(required_inputs):
            if not isinstance(item, str):
                errors.append(
                    _error(
                        f"required_inputs[{i}]",
                        "invalid_required_inputs",
                        (
                            "each required_inputs entry must be a string name "
                            "(e.g. query). Use input_parameters for type/description schemas."
                        ),
                    )
                )
                return None, errors

    steps_in = data.get("steps") or {}
    if isinstance(steps_in, list):
        # Prefer map form; list is accepted but ids must be step_id (or id alias).
        pass
    expected_ids = _expected_step_ids(steps_in)
    # When list items use id: instead of step_id:, expected_ids used to be [0]-style;
    # recompute after normalization below for the final count check.
    if not expected_ids:
        errors.append(
            _error("steps", "no_steps", "workflow must define at least one step")
        )
        return None, errors
    if len(expected_ids) > MAX_STEPS:
        errors.append(
            _error(
                "steps",
                "too_many_steps",
                f"workflow exceeds max of {MAX_STEPS} steps",
            )
        )
        return None, errors

    built_steps: Dict[str, Any] = {}
    if isinstance(steps_in, dict):
        items = [(str(sid), dict(sdata or {})) for sid, sdata in steps_in.items()]
    else:
        items = []
        for i, sdata in enumerate(steps_in):
            if not isinstance(sdata, dict):
                errors.append(
                    _error(
                        f"steps[{i}]",
                        "malformed_step",
                        "step must be a mapping/object",
                    )
                )
                continue
            items.append((str(sdata.get("step_id") or sdata.get("id") or f"step_{i}"), dict(sdata)))

    for sid_hint, sdata in items:
        sid, sdata, shape_errors = _normalize_step_dict(sdata, sid_hint=sid_hint)
        errors.extend(shape_errors)
        if shape_errors:
            continue
        try:
            step = WorkflowStep.from_dict(sdata)
            built_steps[step.step_id] = step
        except Exception as exc:
            errors.append(
                _error(
                    f"steps.{sid}",
                    "malformed_step",
                    f"failed to parse step: {exc}",
                )
            )

    if errors:
        return None, errors

    try:
        workflow = Workflow(
            workflow_id=workflow_id,
            name=str(data.get("name") or workflow_id),
            description=str(data.get("description") or ""),
            steps=built_steps,
            required_inputs=data.get("required_inputs"),
            input_parameters=data.get("input_parameters"),            use_for=data.get("use_for"),
            output_field=data.get("output_field") or None,
            presentation=(
                dict(data["presentation"])
                if isinstance(data.get("presentation"), dict) and data.get("presentation")
                else None
            ),
            handback_tools=data.get("handback_tools"),
            durable=bool(data.get("durable", False)),
            max_nesting_depth=data.get("max_nesting_depth"),
            context=dict(data.get("context") or {}),
            metadata=dict(data.get("metadata") or {}),
        )
    except Exception as exc:
        errors.append(
            _error(
                "workflow",
                "workflow_construct_error",
                f"failed to build workflow: {exc}",
            )
        )
        return None, errors

    if len(workflow.steps) != len(expected_ids):
        errors.append(
            _error(
                "steps",
                "step_count_mismatch",
                f"expected {len(expected_ids)} steps, got {len(workflow.steps)}",
            )
        )
        return None, errors

    return workflow, errors


def _normalize_command_type(command_type: str) -> str:
    token = (command_type or "").strip()
    if token and "." not in token:
        return f"core.{token}"
    return token


def _step_tool_name(step: Any) -> Optional[str]:
    data = getattr(step, "command_data", None) or {}
    if isinstance(data, dict):
        name = data.get("tool_name") or (getattr(step, "parameters", None) or {}).get(
            "tool_name"
        )
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def validate_builder_constraints(
    workflow: Any,
    *,
    motet: Any = None,
    tool_registry: Any = None,
    check_tools: bool = True,
) -> List[BuilderError]:
    """Allowlist, size, and ToolFilter checks beyond structural validate_workflow."""
    from motet.core.workflow.utils import validate_workflow
    from motet.core.tools.meta_tool_policy import (
        tool_filter_metadata_from_context,
        tool_permitted_by_filter,
    )

    errors: List[BuilderError] = []

    try:
        validate_workflow(workflow)
    except ValueError as exc:
        errors.append(_error("workflow", "structural_invalid", str(exc)))
    except Exception as exc:
        errors.append(
            _error(
                "workflow",
                "structural_invalid",
                f"{type(exc).__name__}: {exc}",
            )
        )

    if tool_registry is None:
        try:
            from motet.core.tools import registry as tool_registry
        except Exception:
            tool_registry = None

    filter_meta = tool_filter_metadata_from_context(motet) if motet is not None else None

    for step_id, step in (workflow.steps or {}).items():
        # Structural foreach/until/handback/elicitation rules live in validate_workflow.
        cmd = _normalize_command_type(getattr(step, "command_type", "") or "")
        raw_cmd = (getattr(step, "command_type", "") or "").strip()
        step_type = getattr(step, "step_type", "command") or "command"
        if step_type == "elicitation":
            # Elicitation steps do not require a command_type allowlist entry.
            continue
        if not raw_cmd:
            errors.append(
                _error(
                    f"steps.{step_id}.command_type",
                    "missing_command_type",
                    "command_type is required",
                )
            )
            continue
        if cmd not in {_normalize_command_type(c) for c in ALLOWED_COMMAND_TYPES}:
            errors.append(
                _error(
                    f"steps.{step_id}.command_type",
                    "command_not_allowed",
                    (
                        f"command_type {raw_cmd!r} is not allowed; "
                        f"allowlist: core.tool_execution, core.transform, "
                        f"core.workflow_execution"
                    ),
                )
            )
            continue

        if check_tools and cmd in ("core.tool_execution",):
            tool_name = _step_tool_name(step)
            if not tool_name:
                errors.append(
                    _error(
                        f"steps.{step_id}.command_data.tool_name",
                        "missing_tool_name",
                        "tool_execution steps require command_data.tool_name",
                    )
                )
                continue

            if tool_name.startswith("workflow_"):
                from motet.core.workflow import WorkflowRegistry

                nested_id = tool_name.replace("workflow_", "", 1)
                if WorkflowRegistry.get(nested_id) is None:
                    errors.append(
                        _error(
                            f"steps.{step_id}.command_data.tool_name",
                            "unknown_workflow",
                            f"nested workflow {tool_name!r} is not registered",
                        )
                    )
                    continue
                permitted, reason = tool_permitted_by_filter(
                    tool_name, filter_meta, tool_category="workflow"
                )
                if not permitted:
                    errors.append(
                        _error(
                            f"steps.{step_id}.command_data.tool_name",
                            "tool_not_permitted",
                            reason or f"workflow {tool_name!r} denied by ToolFilter",
                        )
                    )
                continue

            if tool_registry is not None and not tool_name.startswith("mcp."):
                try:
                    exists = tool_registry.get(tool_name) is not None
                except Exception:
                    exists = False
                if not exists:
                    errors.append(
                        _error(
                            f"steps.{step_id}.command_data.tool_name",
                            "unknown_tool",
                            f"tool {tool_name!r} is not registered",
                        )
                    )
                    continue

            category = None
            if tool_registry is not None:
                try:
                    rt = tool_registry.get(tool_name)
                    category = getattr(rt, "category", None) if rt is not None else None
                except Exception:
                    category = None
            permitted, reason = tool_permitted_by_filter(
                tool_name, filter_meta, tool_category=category
            )
            if not permitted:
                errors.append(
                    _error(
                        f"steps.{step_id}.command_data.tool_name",
                        "tool_not_permitted",
                        reason or f"tool {tool_name!r} denied by ToolFilter",
                    )
                )

    return errors


def workflow_summary(workflow: Any) -> Dict[str, Any]:
    """Normalized summary for validate/register responses."""
    steps_out: Dict[str, Any] = {}
    for sid, step in (workflow.steps or {}).items():
        steps_out[sid] = {
            "step_id": step.step_id,
            "name": step.name,
            "command_type": step.command_type,
            "dependencies": list(step.dependencies or []),
            "tool_name": _step_tool_name(step),
        }
    return {
        "workflow_id": workflow.workflow_id,
        "name": workflow.name,
        "description": workflow.description or "",
        "tool_name": f"workflow_{workflow.workflow_id}",
        "step_count": len(workflow.steps or {}),
        "steps": steps_out,
        "execution_order": workflow.execution_order or [],
        "required_inputs": list(workflow.required_inputs or []),
        "use_for": workflow.get_use_for(),
    }


def _missing_required_inputs(
    workflow: Any, context: Optional[Dict[str, Any]]
) -> List[str]:
    required = list(workflow.required_inputs or [])
    ctx = context or {}
    return [key for key in required if key not in ctx]


def is_unregister_allowed(workflow_id: str) -> Tuple[bool, str]:
    """Only user.* definitions may be unregistered via the builder surfaces."""
    wid = (workflow_id or "").strip()
    if not wid:
        return False, "workflow_id is required"
    prefix = f"{AUTHORING_NAMESPACE}."
    if not wid.startswith(prefix):
        return False, (
            f"cannot unregister {wid!r}: only {AUTHORING_NAMESPACE}.* workflows "
            "may be removed via the builder (core/bundle workflows are protected)"
        )
    return True, ""


def check_ownership(
    workflow: Any,
    *,
    principal_id: Optional[str],
    tenant_id: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Enforce principal ownership when metadata is present.

    Workflows without ownership metadata (legacy / tests) are allowed.
    """
    meta = getattr(workflow, "metadata", None) or {}
    if not isinstance(meta, dict):
        return True, ""
    owner = str(meta.get("authored_by_principal_id") or "").strip()
    if not owner:
        return True, ""
    caller = str(principal_id or "").strip()
    if not caller:
        return False, (
            "workflow has ownership metadata but caller principal_id is missing"
        )
    if owner != caller:
        return False, (
            f"workflow is owned by a different principal "
            f"(owner={owner!r}, caller={caller!r})"
        )
    wf_tenant = str(meta.get("tenant_id") or "").strip()
    caller_tenant = str(tenant_id or "").strip()
    if wf_tenant and caller_tenant and wf_tenant != caller_tenant:
        return False, (
            f"workflow belongs to a different tenant "
            f"(workflow={wf_tenant!r}, caller={caller_tenant!r})"
        )
    return True, ""


def stamp_ownership_metadata(
    workflow: Any,
    *,
    principal_id: Optional[str],
    tenant_id: Optional[str],
    scope_slug: str,
) -> None:
    """Attach authorship metadata used for unregister/replace auth."""
    from datetime import datetime, timezone

    meta = dict(getattr(workflow, "metadata", None) or {})
    meta.update(
        {
            "source": "workflow_builder",
            "scope_slug": sanitize_scope_slug(scope_slug),
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    if principal_id:
        meta["authored_by_principal_id"] = str(principal_id).strip()
    if tenant_id:
        meta["tenant_id"] = str(tenant_id).strip()
    workflow.metadata = meta


def export_workflow_bundle_yaml(workflow: Any) -> str:
    """Project a Workflow to bundle-authoring YAML (no runtime state fields)."""
    import yaml  # type: ignore[import]

    steps_out: Dict[str, Any] = {}
    for sid, step in (workflow.steps or {}).items():
        step_doc: Dict[str, Any] = {
            "step_id": step.step_id,
            "name": step.name,
            "command_type": step.command_type,
            "command_data": dict(step.command_data or {}),
            "dependencies": list(step.dependencies or []),
        }
        if getattr(step, "ownership", None) and step.ownership != "motet":
            step_doc["ownership"] = step.ownership
        if getattr(step, "step_type", None) and step.step_type != "command":
            step_doc["type"] = step.step_type
        if getattr(step, "requires_confirmation", False):
            step_doc["requires_confirmation"] = True
        if getattr(step, "elicitation_schema", None):
            step_doc["schema"] = step.elicitation_schema
        if getattr(step, "elicitation_prompt", None):
            step_doc["prompt"] = step.elicitation_prompt
        if getattr(step, "foreach", None):
            step_doc["foreach"] = step.foreach
            step_doc["loop_var"] = step.loop_var
            step_doc["max_loop_iterations"] = step.max_loop_iterations
        if getattr(step, "until", None):
            step_doc["until"] = step.until
        if getattr(step, "skip_condition", None):
            step_doc["skip_condition"] = step.skip_condition
        steps_out[sid] = step_doc

    # Promote uses a bare local id (bundle load will re-namespace).
    local_id = str(workflow.workflow_id).rsplit(".", 1)[-1]
    doc: Dict[str, Any] = {
        "workflow_id": local_id,
        "name": workflow.name,
        "description": workflow.description or "",
        "steps": steps_out,
    }
    if workflow.required_inputs:
        doc["required_inputs"] = list(workflow.required_inputs)
    if workflow.input_parameters:
        doc["input_parameters"] = dict(workflow.input_parameters)
    if workflow.use_for:
        doc["use_for"] = list(workflow.use_for)
    if workflow.keywords:
        doc["keywords"] = list(workflow.keywords)
    if workflow.output_field:
        doc["output_field"] = workflow.output_field
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def _caller_ids(
    motet: Any,
    principal_id: Optional[str],
    tenant_id: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    pid = principal_id
    tid = tenant_id
    if motet is not None:
        pid = pid or getattr(motet, "principal_id", None)
        tid = tid or getattr(motet, "tenant_id", None)
    return (
        str(pid).strip() if pid else None,
        str(tid).strip() if tid else None,
    )


def run_workflow_builder(
    *,
    mode: str,
    yaml_text: Optional[str] = None,
    workflow_dict: Optional[Dict[str, Any]] = None,
    workflow_id: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    replace: bool = False,
    scope_slug: str = "default",
    motet: Any = None,
    tool_registry: Any = None,
    execute: bool = True,
    principal_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    persist: bool = True,
    fan_out: bool = True,
) -> BuilderResult:
    """
    Run one builder mode.

    When ``execute=False``, ``mode=execute`` validates only and returns the
    prepared execution payload without calling ``workflow_execution`` (useful
    for unit tests / HTTP dry paths).
    """
    mode_norm = (mode or "").strip().lower()
    if mode_norm not in BUILDER_MODES:
        return BuilderResult(
            ok=False,
            mode=mode_norm or "unknown",
            errors=[
                _error(
                    "mode",
                    "invalid_mode",
                    f"mode must be one of {sorted(BUILDER_MODES)}",
                )
            ],
        )

    caller_principal, caller_tenant = _caller_ids(motet, principal_id, tenant_id)

    if mode_norm == "unregister":
        target_id = (workflow_id or "").strip()
        allowed, reason = is_unregister_allowed(target_id)
        if not allowed:
            return BuilderResult(
                ok=False,
                mode=mode_norm,
                errors=[_error("workflow_id", "unregister_denied", reason)],
            )
        from motet.core.workflow import WorkflowRegistry
        from motet.core.workflow.user_catalog import (
            delete_user_workflow,
            fan_out_user_workflow_sync,
            resolve_visible_workflow,
        )

        existing = resolve_visible_workflow(target_id, caller_tenant)
        if existing is None:
            return BuilderResult(
                ok=False,
                mode=mode_norm,
                errors=[
                    _error(
                        "workflow_id",
                        "not_found",
                        f"workflow {target_id!r} not found in registry or catalog",
                    )
                ],
            )
        owned, own_reason = check_ownership(
            existing, principal_id=caller_principal, tenant_id=caller_tenant
        )
        if not owned:
            return BuilderResult(
                ok=False,
                mode=mode_norm,
                errors=[_error("workflow_id", "ownership_denied", own_reason)],
            )
        removed = WorkflowRegistry.unregister(target_id)
        if persist:
            delete_user_workflow(target_id, tenant_id=caller_tenant or None)
        fanout_result: Dict[str, Any] = {}
        if fan_out:
            fanout_result = fan_out_user_workflow_sync(
                op="unregister",
                workflow_id=target_id,
                motet=motet,
                tenant_id=caller_tenant or "default",
                principal_id=caller_principal or "",
            )
        logger.info(
            "workflow_builder_unregistered",
            workflow_id=target_id,
            removed=removed,
        )
        return BuilderResult(
            ok=True,
            mode=mode_norm,
            data={
                "workflow_id": target_id,
                "unregistered": bool(removed),
                "fan_out": fanout_result,
            },
        )

    if mode_norm == "export":
        from motet.core.workflow.user_catalog import resolve_visible_workflow

        target_id = (workflow_id or "").strip()
        if not target_id:
            return BuilderResult(
                ok=False,
                mode=mode_norm,
                errors=[
                    _error("workflow_id", "missing_workflow_id", "workflow_id is required")
                ],
            )
        existing = resolve_visible_workflow(target_id, caller_tenant)
        if existing is None:
            return BuilderResult(
                ok=False,
                mode=mode_norm,
                errors=[
                    _error(
                        "workflow_id",
                        "not_found",
                        f"workflow {target_id!r} not found",
                    )
                ],
            )
        owned, own_reason = check_ownership(
            existing, principal_id=caller_principal, tenant_id=caller_tenant
        )
        if not owned:
            return BuilderResult(
                ok=False,
                mode=mode_norm,
                errors=[_error("workflow_id", "ownership_denied", own_reason)],
            )
        yaml_out = export_workflow_bundle_yaml(existing)
        return BuilderResult(
            ok=True,
            mode=mode_norm,
            data={
                "workflow_id": existing.workflow_id,
                "yaml": yaml_out,
                "tool_name": f"workflow_{existing.workflow_id}",
            },
        )

    raw, parse_errors = parse_workflow_document(
        yaml_text=yaml_text, workflow_dict=workflow_dict
    )
    if parse_errors or raw is None:
        return BuilderResult(ok=False, mode=mode_norm, errors=parse_errors)

    namespaced_id, ns_errors = ensure_user_namespace(
        str(raw.get("workflow_id") or ""),
        scope_slug=scope_slug,
        override_id=workflow_id,
    )
    if ns_errors or not namespaced_id:
        return BuilderResult(ok=False, mode=mode_norm, errors=ns_errors)

    workflow, strict_errors = parse_workflow_strict(raw, workflow_id=namespaced_id)
    if strict_errors or workflow is None:
        return BuilderResult(ok=False, mode=mode_norm, errors=strict_errors)

    constraint_errors = validate_builder_constraints(
        workflow, motet=motet, tool_registry=tool_registry
    )
    if constraint_errors:
        return BuilderResult(ok=False, mode=mode_norm, errors=constraint_errors)

    summary = workflow_summary(workflow)

    if mode_norm == "validate":
        return BuilderResult(ok=True, mode=mode_norm, data={"workflow": summary})

    if mode_norm == "execute":
        missing = _missing_required_inputs(workflow, context)
        if missing:
            return BuilderResult(
                ok=False,
                mode=mode_norm,
                errors=[
                    _error(
                        "context",
                        "missing_required_inputs",
                        f"missing required inputs: {missing}",
                    )
                ],
            )
        execution_data = workflow.to_execution_data(context_overrides=context or {})
        if not execute:
            return BuilderResult(
                ok=True,
                mode=mode_norm,
                data={
                    "workflow": summary,
                    "execution_prepared": True,
                    "workflow_steps": list(execution_data.workflow_steps or []),
                },
            )
        if motet is None:
            return BuilderResult(
                ok=False,
                mode=mode_norm,
                errors=[
                    _error(
                        "motet",
                        "motet_required",
                        "mode=execute requires MotetContext (nested workflow_execution)",
                    )
                ],
            )
        from motet.core.commands.builtin.workflow import workflow_execution
        from motet.core.commands.response_models import CommandExecutionError

        try:
            result_payload = motet.do(workflow_execution, data=execution_data)
        except CommandExecutionError as exc:
            message, exec_payload = _format_command_execution_error(exc)
            return BuilderResult(
                ok=False,
                mode=mode_norm,
                errors=[_error("execute", "execution_failed", message)],
                data={"workflow": summary, "execution_error": exec_payload},
            )
        except Exception as exc:
            logger.error(
                "workflow_builder_execute_failed",
                workflow_id=namespaced_id,
                error=str(exc),
                error_type=type(exc).__name__,
                exc_info=True,
            )
            return BuilderResult(
                ok=False,
                mode=mode_norm,
                errors=[
                    _error(
                        "execute",
                        "execution_failed",
                        f"{type(exc).__name__}: {exc}",
                    )
                ],
                data={"workflow": summary},
            )

        step_results: Any = None
        status: Optional[str] = None
        if isinstance(result_payload, dict):
            status = str(result_payload.get("status") or "")
            step_results = result_payload.get("step_results")
            if status and status not in ("success", "completed", "ok", "partial_success"):
                err_info = result_payload.get("error") if isinstance(result_payload.get("error"), dict) else {}
                message = str(
                    (err_info or {}).get("message")
                    or result_payload.get("message")
                    or f"workflow execution status={status!r}"
                )
                failed = _failed_step_summaries(step_results)
                if failed:
                    message = (
                        f"{message}; step failures: "
                        + "; ".join(f"{sid}: {err}" for sid, err in failed.items())
                    )
                return BuilderResult(
                    ok=False,
                    mode=mode_norm,
                    errors=[_error("execute", "execution_failed", message)],
                    data={
                        "workflow": summary,
                        "execution_error": {
                            "status": status,
                            "error": err_info,
                            "failed_steps": failed,
                            "step_results": step_results,
                        },
                        "result": result_payload,
                    },
                )

        failed = _failed_step_summaries(step_results)
        if failed:
            message = "workflow steps failed: " + "; ".join(
                f"{sid}: {err}" for sid, err in failed.items()
            )
            return BuilderResult(
                ok=False,
                mode=mode_norm,
                errors=[_error("execute", "step_failed", message)],
                data={
                    "workflow": summary,
                    "failed_steps": failed,
                    "step_results": step_results,
                    "result": result_payload,
                },
            )

        return BuilderResult(
            ok=True,
            mode=mode_norm,
            data={"workflow": summary, "result": result_payload},
        )

    # mode == register
    from motet.core.workflow import WorkflowRegistry
    from motet.core.workflow.user_catalog import (
        fan_out_user_workflow_sync,
        persist_user_workflow,
        resolve_visible_workflow,
    )

    existing = resolve_visible_workflow(
        namespaced_id, caller_tenant, allow_catalog=persist
    )

    if existing is not None and not replace:
        return BuilderResult(
            ok=False,
            mode=mode_norm,
            errors=[
                _error(
                    "workflow_id",
                    "already_exists",
                    (
                        f"workflow {namespaced_id!r} already registered; "
                        f"pass replace=true to overwrite a {AUTHORING_NAMESPACE}.* "
                        "definition"
                    ),
                )
            ],
        )
    if existing is not None and replace:
        allowed, reason = is_unregister_allowed(namespaced_id)
        if not allowed:
            return BuilderResult(
                ok=False,
                mode=mode_norm,
                errors=[_error("workflow_id", "replace_denied", reason)],
            )
        owned, own_reason = check_ownership(
            existing, principal_id=caller_principal, tenant_id=caller_tenant
        )
        if not owned:
            return BuilderResult(
                ok=False,
                mode=mode_norm,
                errors=[_error("workflow_id", "ownership_denied", own_reason)],
            )
        WorkflowRegistry.unregister(namespaced_id)

    stamp_ownership_metadata(
        workflow,
        principal_id=caller_principal,
        tenant_id=caller_tenant,
        scope_slug=scope_slug,
    )
    if persist:
        persist_user_workflow(workflow)
    WorkflowRegistry.register(workflow)
    fanout_result = {}
    if fan_out and persist:
        fanout_result = fan_out_user_workflow_sync(
            op="register",
            workflow_id=namespaced_id,
            motet=motet,
            tenant_id=caller_tenant or "default",
            principal_id=caller_principal or "",
        )
    logger.info(
        "workflow_builder_registered",
        workflow_id=namespaced_id,
        replace=bool(replace and existing is not None),
        step_count=len(workflow.steps or {}),
        persisted=persist,
    )
    return BuilderResult(
        ok=True,
        mode=mode_norm,
        data={
            "workflow": summary,
            "workflow_id": namespaced_id,
            "tool_name": f"workflow_{namespaced_id}",
            "replaced": bool(replace and existing is not None),
            "fan_out": fanout_result,
        },
    )


__all__ = [
    "ALLOWED_COMMAND_TYPES",
    "AUTHORING_NAMESPACE",
    "BUILDER_MODES",
    "BuilderError",
    "BuilderResult",
    "MAX_STEPS",
    "MAX_YAML_BYTES",
    "check_ownership",
    "ensure_user_namespace",
    "export_workflow_bundle_yaml",
    "is_unregister_allowed",
    "parse_workflow_document",
    "parse_workflow_strict",
    "run_workflow_builder",
    "sanitize_scope_slug",
    "stamp_ownership_metadata",
    "validate_builder_constraints",
    "workflow_summary",
]
