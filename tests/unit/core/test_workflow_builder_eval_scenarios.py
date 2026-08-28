"""
Motet - Workflow Builder Eval Scenario Smoke Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-19

Description:
    Smoke coverage for eval scenarios E1–E3 and E6 in
    ``tests/eval/workflow_builder_scenarios.md`` (validate → register → export,
    allowlist denial, ownership, docs_read YAML contract). Cross-worker
    durability (E4) needs Redis + workers and is covered manually / integration.

Dependencies:
    - pytest
    - motet.core.workflow.builder
"""

from __future__ import annotations

import motet.core.commands.builtin.memory  # noqa: F401
import motet.core.commands.builtin.tool  # noqa: F401
import motet.core.commands.builtin.transform  # noqa: F401

from motet.core.tools.builtin.math_eval import register as register_math
from motet.core.tools.registry import ToolRegistry
from motet.core.workflow import WorkflowRegistry
from motet.core.workflow.builder import run_workflow_builder


YAML = """
workflow_id: eval_brief
name: Eval brief
required_inputs: [topic]
steps:
  calc:
    step_id: calc
    command_type: core.tool_execution
    command_data:
      tool_name: core.math_eval
      parameters:
        expression: "2+2"
    dependencies: []
"""


def _reg() -> ToolRegistry:
    r = ToolRegistry()
    register_math(r)
    return r


def _run(**kwargs):
    kwargs.setdefault("persist", False)
    kwargs.setdefault("fan_out", False)
    kwargs.setdefault("tool_registry", _reg())
    kwargs.setdefault("scope_slug", "acme")
    return run_workflow_builder(**kwargs)


def setup_function() -> None:
    for wf in list(WorkflowRegistry.list_all()):
        if wf.workflow_id.startswith("user."):
            WorkflowRegistry.unregister(wf.workflow_id)


def test_e1_validate_register_export_call_shape() -> None:
    v = _run(mode="validate", yaml_text=YAML)
    assert v.ok, v.to_dict()
    assert v.data["workflow"]["tool_name"].startswith("workflow_user.")

    r = _run(mode="register", yaml_text=YAML, principal_id="p1", tenant_id="acme")
    assert r.ok, r.to_dict()
    wid = r.data["workflow_id"]
    assert WorkflowRegistry.get(wid) is not None

    # Same path the agent uses after register: prepare + execution data.
    data = WorkflowRegistry.prepare_workflow_for_execution(
        workflow_id=wid,
        llm_parameters={"topic": "robots"},
        tenant_id="acme",
    )
    assert data.workflow_id == wid
    assert data.workflow_steps

    exported = _run(mode="export", workflow_id=wid, principal_id="p1")
    assert exported.ok
    assert "workflow_id: eval_brief" in exported.data["yaml"]


def test_e2_reject_non_allowlisted_command() -> None:
    bad = YAML.replace("core.tool_execution", "core.memory_store")
    result = _run(mode="validate", yaml_text=bad)
    assert not result.ok
    assert any(e.code == "command_not_allowed" for e in result.errors)


def test_e3_ownership_gate() -> None:
    r = _run(mode="register", yaml_text=YAML, principal_id="alice", tenant_id="acme")
    assert r.ok, r.to_dict()
    denied = _run(
        mode="unregister",
        workflow_id=r.data["workflow_id"],
        principal_id="bob",
        tenant_id="acme",
    )
    assert not denied.ok
    assert any(e.code == "ownership_denied" for e in denied.errors)


def test_e6_docs_read_yaml_contract_not_guessing() -> None:
    """E6: YAML contract lives in core.docs_read, and the builder points there."""
    import pytest

    from motet.core.developer_docs.corpus import get_docs_dir, read_agent_facing
    from motet.core.tools.builtin import workflow_builder as wb
    from motet.core.tools.registry import ToolRegistry

    if get_docs_dir() is None:
        pytest.skip("developer onboarding docs dir not present")

    payload = read_agent_facing(doc_id="11-workflow-system", section="YAML structure")
    text = payload["text"]
    assert "required_inputs" in text
    assert "input_parameters" in text
    assert "command_type" in text
    assert "command_data" in text
    assert "{param_name}" in text or "{{param_name}}" in text
    assert "mcp_text" in text
    assert "playwright_result" in text

    reg = ToolRegistry()
    wb.register(reg)
    desc = reg.get("core.workflow_builder").description
    assert "core.docs_read" in desc
    assert "11-workflow-system" in desc
    assert "do not guess" in desc
