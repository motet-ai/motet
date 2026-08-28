"""
Motet - Workflow Builder Unit Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Unit tests for ``motet.core.workflow.builder`` — parse, allowlist, user.
    namespace, register/unregister, and fail-closed malformed steps.

Dependencies:
    - pytest
    - motet.core.workflow.builder
    - motet.core.tools.registry.ToolRegistry
"""

from __future__ import annotations

import pytest

# Ensure command types exist for structural validate_workflow.
import motet.core.commands.builtin.tool  # noqa: F401
import motet.core.commands.builtin.transform  # noqa: F401
import motet.core.commands.builtin.memory  # noqa: F401

from motet.core.tools.registry import ToolRegistry
from motet.core.workflow import WorkflowRegistry
from motet.core.workflow.builder import (
    ensure_user_namespace,
    run_workflow_builder,
    sanitize_scope_slug,
)


VALID_YAML = """
workflow_id: competitor_brief
name: Competitor brief
description: Simple math step workflow for builder tests
required_inputs: [topic]
use_for: [tool]
steps:
  calc:
    step_id: calc
    name: Calc
    command_type: core.tool_execution
    command_data:
      tool_name: core.math_eval
      parameters:
        expression: "1+1"
    dependencies: []
"""


@pytest.fixture()
def tool_registry() -> ToolRegistry:
    from motet.core.tools.builtin.math_eval import register as register_math

    reg = ToolRegistry()
    register_math(reg)
    return reg


@pytest.fixture(autouse=True)
def _clean_user_workflows():
    """Remove any user.* workflows left by a test."""
    yield
    for wf in list(WorkflowRegistry.list_all()):
        if wf.workflow_id.startswith("user."):
            WorkflowRegistry.unregister(wf.workflow_id)


def test_sanitize_scope_slug() -> None:
    assert sanitize_scope_slug("Acme Corp") == "acme_corp"
    assert sanitize_scope_slug("!!!") == "default"


def test_ensure_user_namespace_rewrites_bare_id() -> None:
    wid, errors = ensure_user_namespace("competitor_brief", scope_slug="acme")
    assert errors == []
    assert wid == "user.acme.competitor_brief"


def _run(**kwargs):
    kwargs.setdefault("persist", False)
    kwargs.setdefault("fan_out", False)
    return run_workflow_builder(**kwargs)


def test_validate_good_yaml(tool_registry: ToolRegistry) -> None:
    result = _run(
        mode="validate",
        yaml_text=VALID_YAML,
        scope_slug="acme",
        tool_registry=tool_registry,
    )
    assert result.ok, result.to_dict()
    assert result.data["workflow"]["workflow_id"] == "user.acme.competitor_brief"
    assert result.data["workflow"]["tool_name"] == "workflow_user.acme.competitor_brief"
    assert result.data["workflow"]["step_count"] == 1


def test_validate_rejects_unknown_tool(tool_registry: ToolRegistry) -> None:
    yaml_text = VALID_YAML.replace("core.math_eval", "core.definitely_missing_tool")
    result = _run(
        mode="validate",
        yaml_text=yaml_text,
        scope_slug="acme",
        tool_registry=tool_registry,
    )
    assert not result.ok
    assert any(e.code == "unknown_tool" for e in result.errors)


def test_validate_rejects_disallowed_command(tool_registry: ToolRegistry) -> None:
    # memory_store is a real command but outside the builder allowlist.
    yaml_text = VALID_YAML.replace("core.tool_execution", "core.memory_store")
    result = _run(
        mode="validate",
        yaml_text=yaml_text,
        scope_slug="acme",
        tool_registry=tool_registry,
    )
    assert not result.ok
    assert any(e.code == "command_not_allowed" for e in result.errors)


def test_validate_rejects_malformed_step(tool_registry: ToolRegistry) -> None:
    # steps as a list with a non-object entry must fail closed
    result = _run(
        mode="validate",
        workflow_dict={
            "workflow_id": "bad",
            "name": "Bad",
            "steps": ["not-a-step"],
        },
        scope_slug="acme",
        tool_registry=tool_registry,
    )
    assert not result.ok
    assert any(e.code == "malformed_step" for e in result.errors)


def test_validate_allows_foreach(tool_registry: ToolRegistry) -> None:
    result = _run(
        mode="validate",
        workflow_dict={
            "workflow_id": "looped",
            "name": "Looped",
            "steps": {
                "calc": {
                    "step_id": "calc",
                    "command_type": "core.tool_execution",
                    "command_data": {
                        "tool_name": "core.math_eval",
                        "parameters": {"expression": "1"},
                    },
                    "foreach": "items",
                    "loop_var": "item",
                    "dependencies": [],
                }
            },
        },
        scope_slug="acme",
        tool_registry=tool_registry,
    )
    assert result.ok, result.to_dict()


def test_register_and_unregister(tool_registry: ToolRegistry) -> None:
    reg = _run(
        mode="register",
        yaml_text=VALID_YAML,
        scope_slug="acme",
        tool_registry=tool_registry,
        principal_id="user-1",
        tenant_id="acme",
    )
    assert reg.ok, reg.to_dict()
    wid = reg.data["workflow_id"]
    assert WorkflowRegistry.get(wid) is not None

    # Second register without replace → conflict
    again = _run(
        mode="register",
        yaml_text=VALID_YAML,
        scope_slug="acme",
        tool_registry=tool_registry,
        principal_id="user-1",
        tenant_id="acme",
    )
    assert not again.ok
    assert any(e.code == "already_exists" for e in again.errors)

    replaced = _run(
        mode="register",
        yaml_text=VALID_YAML,
        scope_slug="acme",
        replace=True,
        tool_registry=tool_registry,
        principal_id="user-1",
        tenant_id="acme",
    )
    assert replaced.ok, replaced.to_dict()
    assert replaced.data.get("replaced") is True

    denied = _run(
        mode="unregister",
        workflow_id=wid,
        principal_id="other-user",
        tenant_id="acme",
    )
    assert not denied.ok
    assert any(e.code == "ownership_denied" for e in denied.errors)

    unreg = _run(mode="unregister", workflow_id=wid, principal_id="user-1", tenant_id="acme")
    assert unreg.ok, unreg.to_dict()
    assert WorkflowRegistry.get(wid) is None


def test_unregister_denies_core_namespace() -> None:
    result = _run(mode="unregister", workflow_id="navigate_screenshot")
    assert not result.ok
    assert any(e.code == "unregister_denied" for e in result.errors)


def test_export_bundle_yaml(tool_registry: ToolRegistry) -> None:
    reg = _run(
        mode="register",
        yaml_text=VALID_YAML,
        scope_slug="acme",
        tool_registry=tool_registry,
        principal_id="user-1",
    )
    assert reg.ok, reg.to_dict()
    exported = _run(
        mode="export",
        workflow_id=reg.data["workflow_id"],
        principal_id="user-1",
    )
    assert exported.ok, exported.to_dict()
    assert "workflow_id: competitor_brief" in exported.data["yaml"]
    assert "core.math_eval" in exported.data["yaml"]


def test_execute_dry_prepare(tool_registry: ToolRegistry) -> None:
    result = _run(
        mode="execute",
        yaml_text=VALID_YAML,
        scope_slug="acme",
        context={"topic": "robots"},
        tool_registry=tool_registry,
        execute=False,
    )
    assert result.ok, result.to_dict()
    assert result.data.get("execution_prepared") is True
    assert result.data.get("workflow_steps")


def test_execute_missing_required_inputs(tool_registry: ToolRegistry) -> None:
    result = _run(
        mode="execute",
        yaml_text=VALID_YAML,
        scope_slug="acme",
        context={},
        tool_registry=tool_registry,
        execute=False,
    )
    assert not result.ok
    assert any(e.code == "missing_required_inputs" for e in result.errors)


def test_tool_run_validate_wrapper(tool_registry: ToolRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    from motet.core.tools.builtin import workflow_builder as wb

    monkeypatch.setattr(wb, "_get_motet_context_optional", lambda: None)
    monkeypatch.setattr(wb, "_scope_slug_from_motet", lambda _m: "acme")
    original = wb.run_workflow_builder

    def _wrapped(**kwargs):
        kwargs["tool_registry"] = tool_registry
        return original(**kwargs)

    monkeypatch.setattr(wb, "run_workflow_builder", _wrapped)
    out = wb.run({"mode": "validate", "yaml": VALID_YAML})
    assert out["status"] == "success"
    assert out["result"]["workflow"]["workflow_id"] == "user.acme.competitor_brief"


def test_validate_rejects_required_inputs_schema_object(tool_registry: ToolRegistry) -> None:
    result = _run(
        mode="validate",
        workflow_dict={
            "workflow_id": "bad_inputs",
            "name": "Bad inputs",
            "required_inputs": {"query": {"type": "string"}},
            "steps": {
                "calc": {
                    "step_id": "calc",
                    "command_type": "core.tool_execution",
                    "command_data": {
                        "tool_name": "core.math_eval",
                        "parameters": {"expression": "1"},
                    },
                    "dependencies": [],
                }
            },
        },
        scope_slug="acme",
        tool_registry=tool_registry,
    )
    assert not result.ok
    assert any(e.code == "invalid_required_inputs" for e in result.errors)


def test_validate_rejects_foreign_step_shape(tool_registry: ToolRegistry) -> None:
    result = _run(
        mode="validate",
        workflow_dict={
            "workflow_id": "foreign",
            "name": "Foreign",
            "required_inputs": ["query"],
            "steps": {
                "search": {
                    "id": "search",
                    "tool": "core.tool_execution",
                    "params": {"tool_name": "core.math_eval"},
                }
            },
        },
        scope_slug="acme",
        tool_registry=tool_registry,
    )
    assert not result.ok
    assert any(e.code == "invalid_step_shape" for e in result.errors)


def test_validate_list_steps_accept_id_alias(tool_registry: ToolRegistry) -> None:
    result = _run(
        mode="validate",
        workflow_dict={
            "workflow_id": "list_ids",
            "name": "List ids",
            "required_inputs": ["query"],
            "steps": [
                {
                    "id": "calc",
                    "command_type": "core.tool_execution",
                    "command_data": {
                        "tool_name": "core.math_eval",
                        "parameters": {"expression": "1+1"},
                    },
                    "dependencies": [],
                }
            ],
        },
        scope_slug="acme",
        tool_registry=tool_registry,
    )
    assert result.ok, result.to_dict()
    assert "calc" in result.data["workflow"]["steps"]


def test_execute_surfaces_step_failures(tool_registry: ToolRegistry) -> None:
    class _FakeMotet:
        def do(self, _cmd, data=None, **_kwargs):
            return {
                "status": "completed",
                "workflow_id": "user.acme.competitor_brief",
                "step_results": {
                    "calc": {
                        "status": "failed",
                        "error": "2 validation errors for TransformData",
                    }
                },
            }

    result = _run(
        mode="execute",
        yaml_text=VALID_YAML,
        scope_slug="acme",
        context={"topic": "robots"},
        tool_registry=tool_registry,
        motet=_FakeMotet(),
        execute=True,
    )
    assert not result.ok
    assert any(e.code == "step_failed" for e in result.errors)
    assert "calc" in (result.data.get("failed_steps") or {})


def test_execute_accepts_completed_status_without_failures(
    tool_registry: ToolRegistry,
) -> None:
    class _FakeMotet:
        def do(self, _cmd, data=None, **_kwargs):
            return {
                "status": "completed",
                "workflow_id": "user.acme.competitor_brief",
                "step_results": {
                    "calc": {"status": "success", "data": {"result": 2}},
                },
            }

    result = _run(
        mode="execute",
        yaml_text=VALID_YAML,
        scope_slug="acme",
        context={"topic": "robots"},
        tool_registry=tool_registry,
        motet=_FakeMotet(),
        execute=True,
    )
    assert result.ok, result.to_dict()
    assert result.data["result"]["step_results"]["calc"]["status"] == "success"


def test_tool_description_includes_template_and_docs_read_pointer() -> None:
    from motet.core.tools.builtin import workflow_builder as wb
    from motet.core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    wb.register(reg)
    desc = reg.get("core.workflow_builder").description
    assert "validate" in desc and "register" in desc
    assert "core.docs_read" in desc
    assert "11-workflow-system" in desc
    assert "YAML structure" in desc
    assert "command_type" in desc and "command_data" in desc
    assert "workflow_id: my_workflow" in desc
    assert "mcp_text" in desc
    assert "LIST OF STRINGS" not in desc
    assert "http_get_browser" not in desc
