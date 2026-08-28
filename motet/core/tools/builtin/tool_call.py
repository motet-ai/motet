"""
Motet - Generic tool / workflow dispatch (core.tool_call)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Permanently-resident meta-tool that executes any authorized registry tool
    or agent-callable workflow by canonical name without requiring that
    capability's schema in the tools array. Closes the discovery loop opened
    by ``core.tools_search``:
    search returns schemas in the observation tail; ``core.tool_call`` invokes
    without a prefix change.

    Tools dispatch via nested ``tool_execution`` (MCP-safe). Workflows
    (``workflow_<id>``) dispatch via ``workflow_execution``, matching the
    agentic-loop branch.

    Authorization uses the calling agent's ToolFilter metadata (exclude /
    prefix / category / no_workflows). Validation errors echo the target
    JSON schema so the model can repair arguments in the message tail without
    invalidating the tools prefix cache.

    The target's result is returned **verbatim** so a dispatched call is
    observationally identical to having that tool in this turn's tool list
    (same payload shape, same observation text). ``tool_call`` only reports its
    own dispatch-phase failures — unknown name, ToolFilter denial, recursion,
    parameter validation — tagged ``meta.phase = "dispatch"``. It never
    re-interprets the target's status, so soft outcomes (``ok``, ``not_ready``)
    and target errors reach the model exactly as the tool wrote them.

Dependencies:
    - pydantic: parameter schema
    - motet.core.tools.meta_tool_policy: ToolFilter enforcement
    - motet.core.commands.builtin.tool.tool_execution: nested tool dispatch
    - motet.core.commands.builtin.workflow.workflow_execution: workflow dispatch
    - WorkflowRegistry.prepare_workflow_for_execution: LLM params → WorkflowExecutionData

Usage:
    # After tools_search returned mcp.google_workspace.list_events + schema:
    core.tool_call(
        tool_name="mcp.google_workspace.list_events",
        parameters={"calendar_id": "primary", "max_results": 10},
    )

    # After tools_search returned a workflow:
    core.tool_call(
        tool_name="workflow_web_research",
        parameters={"query": "..."},
    )

Notes:
    - Tool names must already be canonical (inbound convert lives in
      ``inbound_tool_call_request``). Wire-format names fail registry lookup.
    - Refuses to dispatch ``core.tool_call`` itself (recursion).
    - Nested tool execution goes through ``motet.do(tool_execution)`` so MCP
      proxy tools and invocation persistence match model-driven calls.
    - Target attribution lives on the nested ``ToolInvocation`` record, not in
      the returned payload — the payload belongs to the target tool.
    - Workflow dispatch keeps an ``ok()`` envelope: ``workflow_execution``
      returns step results, not a tool payload, and has no direct-call twin.
    - Workflow dispatch requires MotetContext (no offline in-process path).
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from pydantic import BaseModel, Field, ValidationError

from ..meta_tool_policy import (
    tool_filter_metadata_from_context,
    tool_permitted_by_filter,
)
from ..protocol import err, ok
from ..registry import ToolRegistry


def _dispatch_err(message: str, **meta: Any) -> Dict[str, Any]:
    """Error raised by core.tool_call itself, before the target ever ran."""
    return err(message, meta={"phase": "dispatch", **meta})


class ToolCallParams(BaseModel):
    """Input for core.tool_call."""

    tool_name: str = Field(
        ...,
        description=(
            "Canonical Motet tool or workflow name to execute "
            "(e.g. 'core.schedule_command', 'mcp.google_workspace.list_events', "
            "'workflow_web_research'). Use the name returned by "
            "core.tools_search. Wire-format names with double underscores are "
            "accepted and normalized."
        ),
    )
    parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Arguments for the target tool or workflow, matching the JSON "
            "schema returned by core.tools_search / core.tool_describe."
        ),
    )


def _get_motet_context_optional() -> Any:
    try:
        from motet.core.commands.decorator import get_motet_context

        return get_motet_context()
    except Exception:
        return None


def _fmt(res: Dict[str, Any]) -> str:
    """Summarize a dispatch. Target payloads pass through, so report their status."""
    raw_meta = res.get("meta")
    meta: Dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    if meta.get("phase") == "dispatch":
        return f"tool_call(dispatch_error={str(res.get('error') or '')[:160]})"
    if meta.get("kind") == "workflow":
        return f"tool_call(ok, workflow={meta.get('tool_name') or '?'})"
    status = res.get("status")
    if res.get("error"):
        return f"tool_call(target_error={str(res.get('error'))[:160]})"
    return f"tool_call(target_status={status or 'ok'})"


def _normalize_name(raw: str) -> str:
    name = (raw or "").strip()
    for prefix in ("functions.", "function.", "tools.", "tool."):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    return name


def _is_workflow_name(name: str) -> bool:
    return (name or "").startswith("workflow_")


def _workflow_id_from_name(name: str) -> str:
    return name[9:] if name.startswith("workflow_") else name


def _schema_for(registry: ToolRegistry, name: str) -> Optional[Dict[str, Any]]:
    reg = registry.get(name)
    if not reg or reg.tool_schema is None:
        return None
    schema = reg.tool_schema
    try:
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            return schema.model_json_schema()
        if isinstance(schema, dict):
            return dict(schema)
    except Exception:
        return None
    return None


def _workflow_schema_for(name: str) -> Optional[Dict[str, Any]]:
    """Return the JSON schema object for a workflow_* tool name, if known."""
    try:
        from ...workflow import WorkflowRegistry
    except Exception:
        return None
    try:
        for schema in WorkflowRegistry.export_canonical_schemas() or []:
            if getattr(schema, "name", None) == name:
                json_schema = getattr(schema, "json_schema", None) or {}
                if hasattr(json_schema, "model_dump"):
                    json_schema = json_schema.model_dump()
                return dict(json_schema) if isinstance(json_schema, dict) else {}
    except Exception:
        return None
    return None


def _hidden_from_agents(registry: ToolRegistry, name: str) -> bool:
    """
    True when the target is registered with ``expose_to_agents=False``.

    ``registry.describe(audience="agent")`` applies this gate, so core.tools_search
    never discloses such tools; generic dispatch must honour the same gate or it
    becomes a bypass for anything deliberately hidden from agents.
    """
    reg = registry.get(name)
    if reg is None:
        return False
    return not bool(getattr(reg, "expose_to_agents", True))


def _category_for(registry: ToolRegistry, name: str) -> Optional[str]:
    if _is_workflow_name(name):
        return "workflow"
    reg = registry.get(name)
    if not reg:
        return None
    cat = getattr(reg, "category", None)
    return str(cat) if cat is not None else None


def _validate_params(
    registry: ToolRegistry, name: str, parameters: Dict[str, Any]
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Validate *parameters* against the target tool's Pydantic schema.

    Returns ``(validated_params, error_payload)``. On success error_payload is
    None; on failure validated_params is None and error_payload is ready to
    return via ``err(...)``.
    """
    reg = registry.get(name)
    if not reg or reg.tool_schema is None:
        return dict(parameters or {}), None
    schema = reg.tool_schema
    if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
        return dict(parameters or {}), None
    try:
        validated = schema(**(parameters or {}))
        return validated.model_dump(), None
    except ValidationError as exc:
        return None, {
            "message": (
                f"parameters for {name!r} failed validation; "
                "see expected_schema and repair the call"
            ),
            "validation_errors": exc.errors(),
            "expected_schema": schema.model_json_schema(),
            "tool_name": name,
        }


def _validate_workflow_params(
    name: str, parameters: Dict[str, Any]
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Soft-validate workflow params against the exported JSON schema required list."""
    params = dict(parameters or {})
    schema = _workflow_schema_for(name)
    if schema is None:
        # Unknown workflow — let prepare_workflow_for_execution raise a clear error.
        return params, None
    required = schema.get("required") or []
    if not isinstance(required, list):
        return params, None
    missing = [r for r in required if isinstance(r, str) and r not in params]
    if missing:
        return None, {
            "message": (
                f"parameters for {name!r} missing required fields "
                f"{missing}; see expected_schema and repair the call"
            ),
            "validation_errors": [
                {"type": "missing", "loc": (field,), "msg": "Field required"}
                for field in missing
            ],
            "expected_schema": schema,
            "tool_name": name,
        }
    return params, None


def _passthrough_target_result(result: Any) -> Dict[str, Any]:
    """
    Strip ``tool_execution``'s command envelope and return the target payload as-is.

    ``tool_execution`` returns ``{tool_name, result, executed}`` (plus a top-level
    ``artifact_id`` when the raw payload was offloaded — that belongs to the nested
    execution and is re-derived for this call by the outer ``tool_execution``).
    The inner ``result`` is whatever the target tool produced, and is returned
    untouched so the observation matches a direct call to that tool.
    """
    payload = result["result"] if isinstance(result, dict) and "result" in result else result
    # Registry-executed tools always yield a dict; wrap anything else so the
    # tool contract (Dict[str, Any]) holds for non-registry paths.
    return payload if isinstance(payload, dict) else ok(payload)


def _dispatch_tool(motet: Any, tool_name: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
    """Execute via nested tool_execution (MCP-safe, invocation records)."""
    from motet.core.commands.command_data_classes import ToolExecutionData
    from motet.core.commands.response_models import CommandExecutionError
    from motet.core.commands.builtin.tool import tool_execution

    try:
        result = motet.do(
            tool_execution,
            data=ToolExecutionData(tool_name=tool_name, parameters=parameters),
        )
    except CommandExecutionError as exc:
        return _dispatch_err(
            str(exc.message or exc),
            tool_name=tool_name,
            kind="tool",
            error_type=type(exc).__name__,
        )
    except Exception as exc:
        return _dispatch_err(
            f"{type(exc).__name__}: {exc}", tool_name=tool_name, kind="tool"
        )

    return _passthrough_target_result(result)


def _dispatch_workflow(
    motet: Any, tool_name: str, parameters: Dict[str, Any]
) -> Dict[str, Any]:
    """Execute via nested workflow_execution (same path as the agentic loop)."""
    from motet.core.commands.response_models import CommandExecutionError
    from motet.core.commands.builtin.workflow import workflow_execution
    from ...workflow import WorkflowRegistry

    workflow_id = _workflow_id_from_name(tool_name)
    try:
        workflow_data = WorkflowRegistry.prepare_workflow_for_execution(
            workflow_id=workflow_id,
            llm_parameters=parameters,
            motet=motet,
        )
    except ValueError as exc:
        return _dispatch_err(
            str(exc), tool_name=tool_name, kind="workflow", workflow_id=workflow_id
        )
    except Exception as exc:
        return _dispatch_err(
            f"Error preparing workflow: {exc}",
            tool_name=tool_name,
            kind="workflow",
            workflow_id=workflow_id,
            error_type=type(exc).__name__,
        )

    try:
        result = motet.do(workflow_execution, data=workflow_data)
    except CommandExecutionError as exc:
        return err(
            str(exc.message or exc),
            meta={
                "tool_name": tool_name,
                "kind": "workflow",
                "workflow_id": workflow_id,
                "error_type": type(exc).__name__,
            },
        )
    except Exception as exc:
        return err(
            f"{type(exc).__name__}: {exc}",
            meta={
                "tool_name": tool_name,
                "kind": "workflow",
                "workflow_id": workflow_id,
            },
        )

    return ok(
        result,
        meta={
            "tool_name": tool_name,
            "kind": "workflow",
            "workflow_id": workflow_id,
        },
    )


def run(registry: ToolRegistry, params: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch an authorized registry tool or workflow by canonical name."""
    try:
        parsed = ToolCallParams(**(params or {}))
    except Exception as exc:
        return _dispatch_err(f"validation error: {exc}")

    tool_name = _normalize_name(parsed.tool_name)
    if not tool_name:
        return _dispatch_err("tool_name is required")

    motet = _get_motet_context_optional()
    filter_meta = tool_filter_metadata_from_context(motet)
    permitted, reason = tool_permitted_by_filter(
        tool_name,
        filter_meta,
        tool_category=_category_for(registry, tool_name),
    )
    if not permitted:
        return _dispatch_err(reason, tool_name=tool_name, denied=True)

    if _hidden_from_agents(registry, tool_name):
        return _dispatch_err(
            f"tool {tool_name!r} is not exposed to agents and cannot be invoked "
            "via core.tool_call",
            tool_name=tool_name,
            denied=True,
        )

    if _is_workflow_name(tool_name):
        validated, validation_error = _validate_workflow_params(
            tool_name, parsed.parameters or {}
        )
        if validation_error is not None:
            return _dispatch_err(
                validation_error["message"],
                tool_name=tool_name,
                kind="workflow",
                validation_errors=validation_error["validation_errors"],
                expected_schema=validation_error["expected_schema"],
            )
        assert validated is not None
        if motet is None:
            return _dispatch_err(
                f"{tool_name!r} is a workflow and requires MotetContext "
                "(nested workflow_execution); cannot run offline",
                tool_name=tool_name,
                kind="workflow",
            )
        return _dispatch_workflow(motet, tool_name, validated)

    # Registry presence for non-MCP tools; MCP names are three-part and may
    # only exist as proxies on the worker that runs tool_execution.
    is_mcp = tool_name.startswith("mcp.")
    if not is_mcp and registry.get(tool_name) is None:
        return _dispatch_err(
            f"tool not found: {tool_name!r}. "
            "Search with core.tools_search, then retry with the exact name.",
            tool_name=tool_name,
            kind="tool",
        )

    validated, validation_error = _validate_params(
        registry, tool_name, parsed.parameters or {}
    )
    if validation_error is not None:
        return _dispatch_err(
            validation_error["message"],
            tool_name=tool_name,
            kind="tool",
            validation_errors=validation_error["validation_errors"],
            expected_schema=validation_error["expected_schema"],
        )
    assert validated is not None

    if motet is None:
        # Offline / direct registry call (unit tests): in-process path only.
        return registry._execute_tool_only(tool_name, validated)

    return _dispatch_tool(motet, tool_name, validated)


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.tool_call",
        description=(
            "Execute any authorized Motet tool or workflow by canonical name "
            "without that schema being resident in this turn's tool list. Use "
            "after core.tools_search (or core.tool_describe) has returned the "
            "JSON schema in the observation. Pass 'tool_name' (canonical name "
            "from search, including workflow_<id>) and 'parameters' matching "
            "that schema. The target tool's result is returned unchanged, so "
            "read it exactly as that tool's schema describes. Prefer a direct "
            "tool call when the capability is already in this turn's tool list."
        ),
        func=lambda p, _r=registry: run(_r, p),
        tool_schema=ToolCallParams,
        triggers=["tool_call:"],
        observation_formatter=_fmt,
        category="system",
        contextualize_observation=False,
        default_timeout_seconds=120.0,
        suggested_max_calls=8,
        cost_class="low",
        keywords=["invoke", "dispatch", "call", "tool", "mcp", "discover", "workflow"],
    )


__all__ = ["register", "run", "ToolCallParams"]
