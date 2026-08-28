"""
Motet — Skill runner runtime registration

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Turns each runner declared in ``skills/<dir>/runners.yaml`` into a
    namespaced tool the LLM can call directly. The tool name is

        ``{bundle_id}.{skill_name}.{runner_name}``

    Dispatch routing is decided by the runner's ``lifetime`` declaration:

    * ``lifetime: ephemeral`` — Hermetic per-call execution. The tool builds
        an argv (interpreter + script + author flags) and delegates to
        ``core.worker_exec``, which handles bundle staging, allowlisted
        cwds, container backends, and timeouts.

    * ``lifetime: workspace`` — Per-(tenant, conversation, bundle, skill,
        image_stack) container reuse. The tool delegates to ``core.worker_exec``
        with ``workspace_mode="workspace"`` and runner scope fields; the
        WorkspaceContainerManager owns the container lifecycle. ``/scratch``
        persists across calls but each call is a fresh process.

    * ``lifetime: stateful`` — Same persistent workspace container, but
        the runner author's skill module is *imported once* into a long-lived
        in-container Python process. Module-level globals (counters,
        loaded models, open connections) survive across calls. Wire
        format on the LLM side is unchanged: the tool takes a params
        dict and returns the supervisor envelope.

Dependencies:
    - motet.core.skills.runners: RunnerSpec / RunnersDoc parser
    - motet.core.tools.builtin.worker_exec: per-call + workspace execution
    - motet.core.execution.run_stateful_in_workspace: stateful execution
    - motet.core.tools.registry.ToolRegistry: registration target
    - pydantic: dynamic per-runner Params model for native function calling

Usage:
    from motet.core.skills.runtime import register_runners_for_skill

    registered = register_runners_for_skill(
        bundle_id="acme.demo",
        skill_name="basic-script-skill",
        skill_dir=Path("/var/motet/bundles/acme.demo/skills/basic-script-skill"),
        bundle_id_for_staging="acme.demo",
    )

Notes:
    - **Tool funcs are pure dispatchers.** Per-call paths defer staging to
      ``core.worker_exec``; workspace-backed paths read the script bytes at
      dispatch time and let the WorkspaceContainerManager materialize them
      into the reused container. A redeploy that changes the source forces
      the stateful container to be replaced (it diffs the script SHA-256 on the
      binding).
    - **Tool registration is idempotent.** Re-registering the same name
      overwrites the existing entry (this matches how `_load_bundle_tools`
      handles redeploys).
    - **Stateful runner author contract.** The script must define
      ``def handle(params: dict) -> Any`` at module scope. Any return value
      that isn't already a dict is wrapped as ``{"value":...}`` by the
      supervisor so the wire shape stays uniform on the manager side.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

import structlog
from pydantic import BaseModel, ConfigDict, Field, create_model

from motet.core.skills.runners import (
    RunnerArg,
    RunnerSpec,
    RunnersDoc,
    parse_runners_yaml,
)

logger = structlog.get_logger(__name__)

# Tool category mirrors `core.worker_exec` so capability inference + UI
# grouping work without bespoke handling.
_RUNNER_TOOL_CATEGORY = "shell"

_TYPE_MAP: Dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


def register_runners_for_skill(
    *,
    bundle_id: str,
    skill_name: str,
    skill_dir: Path,
    bundle_id_for_staging: Optional[str] = None,
) -> List[str]:
    """Register each runner in ``skill_dir/runners.yaml`` as a namespaced tool.

    Args:
        bundle_id: Owning bundle id; used as the first segment of the tool name.
        skill_name: Skill slug (matches the directory under ``skills/``);
            second segment of the tool name.
        skill_dir: Absolute path to the staged skill directory on this worker.
        bundle_id_for_staging: Bundle slug to pass to ``core.worker_exec`` for
            bundle staging. Defaults to ``bundle_id``; callers that already
            coerce ``bundle.skill`` shapes upstream can pass an explicit value.

    Returns:
        Sorted list of namespaced tool names that were registered. Empty
        list when ``runners.yaml`` is absent.

    Notes:
        Errors during parsing are *raised* — a malformed runners.yaml
        should fail bundle reload loudly so the operator sees the problem
        in the deploy stream. Per-runner registration errors (e.g. invalid
        Pydantic synthesis) are caught and logged so one bad runner does
        not block its siblings.
    """
    runners_path = skill_dir / "runners.yaml"
    if not runners_path.is_file():
        return []

    try:
        from motet.core.tools import registry as tool_registry
    except ImportError:
        logger.warning(
            "skill_runners.no_tool_registry",
            bundle_id=bundle_id,
            skill_name=skill_name,
        )
        return []

    try:
        doc = parse_runners_yaml(runners_path)
    except Exception as exc:
        logger.error(
            "skill_runners.parse_failed",
            bundle_id=bundle_id,
            skill_name=skill_name,
            path=str(runners_path),
            error=str(exc),
        )
        raise

    staging_bundle_id = bundle_id_for_staging or bundle_id
    registered: List[str] = []

    for runner in doc.runners:
        tool_name = runner.tool_name(bundle_id, skill_name)
        try:
            params_model = _build_params_model(runner)
            func = _make_runner_dispatch(
                runner=runner,
                bundle_id=bundle_id,
                bundle_id_for_staging=staging_bundle_id,
                skill_name=skill_name,
                skill_dir=skill_dir,
            )
            description = _describe_runner(runner, bundle_id=bundle_id, skill_name=skill_name)
            tool_registry.register(
                tool_name,
                description=description,
                func=func,
                tool_schema=params_model,
                category=_RUNNER_TOOL_CATEGORY,
                priority=4,
                required_capabilities=["TOOL_EXECUTION", "WORKER_SHELL_EXEC"],
                presentation={
                    "user_facing": True,
                    "requires_llm": True,
                    "content_kind": "text",
                },
            )
            registered.append(tool_name)
            logger.info(
                "skill_runners.registered",
                bundle_id=bundle_id,
                skill_name=skill_name,
                tool_name=tool_name,
                script=runner.script,
                interpreter=runner.interpreter,
                image_stack=runner.image_stack,
                lifetime=runner.lifetime,
            )
        except Exception as exc:
            logger.error(
                "skill_runners.register_failed",
                bundle_id=bundle_id,
                skill_name=skill_name,
                runner_name=runner.name,
                tool_name=tool_name,
                error=str(exc),
                exc_info=True,
            )

    return sorted(registered)


# ---------------------------------------------------------------------------
# Pydantic params model synthesis
# ---------------------------------------------------------------------------


def _build_params_model(runner: RunnerSpec) -> Type[BaseModel]:
    """Build a per-runner Params model so native function calling shows the
    runner's exact arg list to the LLM.

    Each declared arg becomes a field with the appropriate Python type.
    Unknown extra keys are rejected (``extra="forbid"``) so model-side typos
    surface as validation errors rather than silently no-ops.
    """
    fields: Dict[str, Tuple[type, Any]] = {}
    for arg in runner.args:
        py_type = _TYPE_MAP.get(arg.type, str)
        if arg.required and arg.default is None:
            field_info = Field(..., description=arg.description or arg.name)
            field_type: Any = py_type
        else:
            default = arg.default if arg.default is not None else None
            field_info = Field(default=default, description=arg.description or arg.name)
            # Optional fields are typed with their concrete type but allow None
            # via the default; we keep the type hint concrete so JSON Schema
            # generation produces the right "type" entry without an "anyOf".
            field_type = py_type if arg.default is not None else Optional[py_type]
        fields[arg.name] = (field_type, field_info)

    model_name = f"RunnerParams_{runner.name.replace('-', '_')}"
    Model = create_model(  # type: ignore[call-overload]
        model_name,
        __base__=_RunnerParamsBase,
        **fields,
    )
    return Model


class _RunnerParamsBase(BaseModel):
    """Base for runner Params models.

    `extra="forbid"` makes typos fail loudly. `populate_by_name=True` tolerates
    LLMs that produce snake_case vs hyphen variants (we still register the
    canonical name).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _make_runner_dispatch(
    *,
    runner: RunnerSpec,
    bundle_id: str,
    bundle_id_for_staging: str,
    skill_name: str,
    skill_dir: Path,
):
    """Build the synchronous tool function that runs the runner's script.

    The returned closure captures a small immutable record (no mutable
    state) so re-registering the same runner across bundle reloads always
    produces a fresh dispatcher pointed at the latest staged path. The
    script source is read on every stateful dispatch (cheap, and the
    WorkspaceContainerManager already diffs the SHA-256 on the binding).
    """
    # Per-call / workspace paths use the bundle-relative path because
    # ``core.worker_exec``'s _normalize_bundle_argv strips the absolute
    # bundle root and re-roots into the staged execution cwd. Warm path
    # reads from the staged absolute path on this worker.
    relative_under_bundle = f"skills/{skill_name}/{runner.script.lstrip('/')}"
    interpreter_argv = _interpreter_argv(runner.interpreter)
    timeout_s = runner.timeout_seconds
    absolute_script_path = (skill_dir / runner.script.lstrip("/")).resolve()

    def _dispatch(params: Dict[str, Any]) -> Dict[str, Any]:
        if runner.lifetime == "stateful":
            return _dispatch_stateful_runner(
                runner=runner,
                bundle_id=bundle_id,
                skill_name=skill_name,
                absolute_script_path=absolute_script_path,
                params=params,
                timeout_s=timeout_s,
            )

        # Lazy-import worker_exec to avoid an import cycle at module-load
        # time (worker_exec imports from motet.core.execution which is a
        # peer of skills/).
        from motet.core.tools.builtin.worker_exec import run as worker_exec_run

        flags = _coerce_args_to_flags(params, runner.args)
        argv = list(interpreter_argv) + [relative_under_bundle] + flags

        wexec_params: Dict[str, Any] = {
            "argv": argv,
            "bundle_id": bundle_id_for_staging,
        }
        if timeout_s is not None:
            wexec_params["timeout_seconds"] = timeout_s

        if runner.lifetime == "workspace":
            # Workspace-lifetime runners route through the existing
            # WorkspaceContainerManager via core.worker_exec's workspace_mode
            # parameter so all workspace-container guarantees (per-tenant
            # cap, idle reaping, registry routing) apply uniformly.
            try:
                script_source = absolute_script_path.read_bytes()
            except OSError as exc:
                return {
                    "runner": runner.name,
                    "runner_image_stack": runner.image_stack,
                    "runner_lifetime": runner.lifetime,
                    "error": (
                        f"workspace runner '{runner.name}': failed to read staged script "
                        f"{absolute_script_path}: {exc}"
                    ),
                }
            wexec_params["workspace_mode"] = "workspace"
            wexec_params["workspace_image_stack"] = runner.image_stack
            wexec_params["workspace_bundle_id"] = bundle_id
            wexec_params["workspace_skill_name"] = skill_name
            wexec_params["workspace_materialized_files"] = [
                {
                    "path": f"/scratch/{relative_under_bundle}",
                    "content_b64": base64.b64encode(script_source).decode("ascii"),
                    "mode": 0o600,
                }
            ]

        result = worker_exec_run(wexec_params)
        result.setdefault("runner", runner.name)
        result.setdefault("runner_image_stack", runner.image_stack)
        result.setdefault("runner_lifetime", runner.lifetime)
        return result

    _dispatch.__name__ = f"runner_{runner.name.replace('-', '_')}"
    return _dispatch


def _dispatch_stateful_runner(
    *,
    runner: RunnerSpec,
    bundle_id: str,
    skill_name: str,
    absolute_script_path: Path,
    params: Dict[str, Any],
    timeout_s: Optional[int],
) -> Dict[str, Any]:
    """Read the staged script and route the call to ``run_stateful_in_workspace``.

    Errors during script read or context resolution become a transport
    error envelope (``ok=False``, ``transport_error=True``) so the LLM
    sees a structured failure with the same shape it would see from the
    supervisor itself. We deliberately don't fall back to per-call
    execution: the runner author asked for stateful semantics and silently
    serving a hermetic call would corrupt the mental model (e.g. a
    counter that resets every other turn).
    """
    from motet.core.execution import run_stateful_in_workspace
    from motet.core.tools.builtin.worker_exec import (
        _get_motet_context_optional,
        _resolve_workspace_image,
    )

    motet = _get_motet_context_optional()
    tenant_id = getattr(motet, "tenant_id", None) if motet is not None else None
    conversation_id = (
        getattr(motet, "conversation_id", None) if motet is not None else None
    )
    correlation_id = getattr(motet, "command_id", None) if motet is not None else None

    try:
        script_source = absolute_script_path.read_bytes()
    except OSError as exc:
        return {
            "id": correlation_id or "",
            "ok": False,
            "error": (
                f"stateful runner '{runner.name}': failed to read staged script "
                f"{absolute_script_path}: {exc}"
            ),
            "traceback": "",
            "stdout": "",
            "stderr": "",
            "transport_error": True,
            "runner": runner.name,
            "runner_lifetime": "stateful",
            "runner_image_stack": runner.image_stack,
        }

    oci_image_ref = _resolve_workspace_image(runner.image_stack)

    envelope = run_stateful_in_workspace(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        image_stack=runner.image_stack,
        oci_image_ref=oci_image_ref,
        bundle_id=bundle_id,
        skill_name=skill_name,
        script_source=script_source,
        script_logical_name=Path(runner.script).name,
        params=dict(params),
        timeout_seconds=timeout_s,
        request_id=correlation_id,
    )
    envelope.setdefault("runner", runner.name)
    envelope.setdefault("runner_lifetime", "stateful")
    envelope.setdefault("runner_image_stack", runner.image_stack)
    envelope.setdefault("bundle_id", bundle_id)
    envelope.setdefault("skill_name", skill_name)
    return envelope


def _interpreter_argv(interpreter: str) -> Tuple[str, ...]:
    """Map declared interpreter to argv prefix.

    Today this is a one-element prefix; left as a tuple so future
    interpreters that need flags (e.g. ``("node", "--experimental-vm-modules")``)
    can be added without changing call sites.
    """
    if interpreter == "python":
        return ("python3",)
    return (interpreter,)


def _coerce_args_to_flags(params: Dict[str, Any], declared: Tuple[RunnerArg, ...]) -> List[str]:
    """Map ``{name: value}`` params to ``--name=value`` flags.

    Booleans use the bare ``--flag`` form when True and are omitted when
    False. None values are always omitted (caller did not provide them
    and the schema permits it). Order matches the runner's declaration
    order so script CLIs that care about positional flag ordering get
    deterministic invocations.
    """
    flags: List[str] = []
    for arg in declared:
        if arg.name not in params:
            continue
        value = params[arg.name]
        if value is None:
            continue
        if arg.type == "boolean":
            if bool(value):
                flags.append(f"--{arg.name}")
            continue
        flags.append(f"--{arg.name}={_stringify(value)}")
    return flags


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _describe_runner(runner: RunnerSpec, *, bundle_id: str, skill_name: str) -> str:
    """Compose a tool description that includes runner metadata.

    The first line is the author-supplied description (what the LLM sees
    in tool catalogs); subsequent lines are operator-relevant context so
    `motet-cli tools describe` and the ops UI can show the wiring
    without an extra catalog lookup.
    """
    base = (runner.description or f"Runner {runner.name} from {bundle_id}.{skill_name}").strip()
    suffix_lines = [
        f"[bundle:{bundle_id} skill:{skill_name} runner:{runner.name}]",
        f"image_stack={runner.image_stack} lifetime={runner.lifetime} "
        f"interpreter={runner.interpreter}",
    ]
    return base + "\n\n" + "\n".join(suffix_lines)


__all__ = ["register_runners_for_skill"]
