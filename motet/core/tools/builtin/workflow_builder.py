"""
Motet - Workflow Builder Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Built-in tool ``core.workflow_builder`` that lets an agent author a workflow
    in bundle YAML shape, validate it, execute it ephemerally, register it as a
    callable ``workflow_<id>`` tool (``user.*`` namespace), or unregister a prior
    LLM-authored definition. Delegates to ``motet.core.workflow.builder``.

Dependencies:
    - pydantic: Parameter schema for the tool
    - motet.core.workflow.builder: Shared parse / allowlist / modes
    - motet.core.tools.protocol: ok/err response helpers
    - motet.core.commands.decorator.get_motet_context: MotetContext for execute

Usage:
    core.workflow_builder(
        mode="validate",
        yaml="workflow_id: brief\\nname: Brief\\nsteps:\\n  ...",
    )
    core.workflow_builder(mode="register", yaml=..., replace=False)
    core.workflow_builder(mode="unregister", workflow_id="user.acme.brief")

Notes:
    - Registered workflows become discoverable as ``workflow_user.<owner>.<name>``.
    - Allowlisted step command_types: core.tool_execution, core.transform,
      core.workflow_execution.
    - YAML contract and placeholders live in developer docs; call
      ``core.docs_read`` rather than stuffing the manual into this description.
    - Prefer ``core.command_describe`` / ``core.tools_search`` for deep schemas.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from ...workflow.builder import run_workflow_builder, sanitize_scope_slug
from ..protocol import err, ok
from ..registry import ToolRegistry

_YAML_TEMPLATE = """\
workflow_id: my_workflow
name: My workflow
description: Short description
required_inputs: [input_a]
input_parameters:
  input_a:
    type: string
    description: What input_a means
steps:
  step_one:
    step_id: step_one
    name: First step
    command_type: core.tool_execution
    command_data:
      tool_name: <discover with core.tools_search>
      parameters:
        some_param: "{input_a}"
    dependencies: []
  step_two:
    step_id: step_two
    name: Unwrap MCP tool text
    command_type: core.transform
    command_data:
      input: "{{step_one.result}}"
      operations:
        - type: mcp_text
          output_key: text
    dependencies: [step_one]
"""

_TOOL_DESCRIPTION = (
    "Author a multi-step Motet workflow from YAML (same shape as bundle "
    "workflows/*.yaml). "
    "Recipe: (1) mode=validate until ok, (2) mode=execute with context covering "
    "required_inputs to dry-run, (3) mode=register to persist as "
    "workflow_user.<owner>.<local>. Also: unregister, export (bundle YAML). "
    "Allowed command_type: core.tool_execution, core.transform, "
    "core.workflow_execution. Discover tools with core.tools_search; "
    "use core.command_describe on core.transform for operation types "
    "(mcp_text unwraps MCP envelopes; playwright_result then json_parse "
    "for Playwright browser_evaluate reports). "
    "If validate fails, do not guess — call core.docs_read: "
    "doc_id=11-workflow-system section='YAML structure' (contract, "
    "placeholders, required_inputs vs input_parameters); "
    "section='Runtime-authored workflows (`user.*`)' for the register path; "
    "doc_id=17-building-workflows for the authoring tutorial. "
    "Omit doc_id on core.docs_read to list the catalog. "
    "Minimal template:\n" + _YAML_TEMPLATE
)


class WorkflowBuilderParams(BaseModel):
    """Parameters for core.workflow_builder."""

    mode: str = Field(
        ...,
        description=(
            "Builder mode. Recommended order when authoring: "
            "'validate' → 'execute' (with context) → 'register'. "
            "Also: 'unregister' (remove user.*), 'export' (bundle-shaped YAML)."
        ),
    )
    yaml: Optional[str] = Field(
        default=None,
        description=(
            "Bundle-shaped workflow YAML. Required for validate/execute/register. "
            "Must include workflow_id; required_inputs as a string list; steps as a "
            "map with command_type + command_data. Read core.docs_read "
            "doc_id=11-workflow-system section='YAML structure' for the contract."
        ),
    )
    workflow_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional id override for validate/execute/register (rewritten to "
            "user.<owner>.<local>). Required for unregister/export of an existing id."
        ),
    )
    context: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Inputs for mode=execute. Keys must cover required_inputs "
            "(e.g. {\"query\": \"tesla\"})."
        ),
    )
    replace: bool = Field(
        default=False,
        description="When true, allow overwriting an existing user.* workflow on register.",
    )


def _get_motet_context_optional() -> Any:
    try:
        from motet.core.commands.decorator import get_motet_context

        return get_motet_context()
    except Exception:
        return None


def _scope_slug_from_motet(motet: Any) -> str:
    if motet is None:
        return "default"
    tenant = getattr(motet, "tenant_id", None) or ""
    principal = getattr(motet, "principal_id", None) or ""
    return sanitize_scope_slug(tenant or principal or "default")


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Author / validate / execute / register / unregister a workflow definition."""
    try:
        parsed = WorkflowBuilderParams(**(params or {}))
    except Exception as exc:
        return err(f"validation error: {exc}")

    motet = _get_motet_context_optional()
    result = run_workflow_builder(
        mode=parsed.mode,
        yaml_text=parsed.yaml,
        workflow_id=parsed.workflow_id,
        context=parsed.context,
        replace=bool(parsed.replace),
        scope_slug=_scope_slug_from_motet(motet),
        motet=motet,
    )
    payload = result.to_dict()
    if not result.ok:
        # Surface structured errors for LLM repair in both error + result.
        return err(
            "; ".join(e.message for e in result.errors) or "workflow builder failed",
            meta={"builder": payload},
        )
    return ok(payload)


def register(registry: ToolRegistry) -> None:
    """Register core.workflow_builder."""
    registry.register(
        name="core.workflow_builder",
        description=_TOOL_DESCRIPTION,
        func=run,
        tool_schema=WorkflowBuilderParams,
        triggers=["workflow_builder:", "build_workflow:"],
        category="system",
        default_timeout_seconds=120.0,
        suggested_max_calls=4,
        cost_class="medium",
    )


__all__ = ["register", "run", "WorkflowBuilderParams"]
