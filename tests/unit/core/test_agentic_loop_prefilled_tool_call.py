"""
Motet - Agentic Loop Prefilled Tool Call Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-02

Description:
    Unit tests for the prefilled-tool-call path in agentic_loop (ADR-0111):
    synthesis of a canonical tool call, the model_stream-shaped stand-in used to
    skip the planning model call, and validation/authz against the registry and
    the agent tool filter.

Dependencies:
    - pytest: test framework
    - agentic_loop prefilled-tool-call helpers
    - WorkflowRegistry: workflow existence checks

Usage:
    pytest tests/unit/core/test_agentic_loop_prefilled_tool_call.py
"""

import json

from motet.core.workflow import Workflow, WorkflowRegistry
from motet.core.reasoning.react.loop_execution import (
    prefilled_stream_data,
    _prefilled_tool_filter_violation,
    _synthesize_prefilled_tool_call,
    validate_prefilled_tool_calls,
)
from motet.core.reasoning.react.agentic_loop_data import (
    AgenticLoopData,
    PrefilledToolCall,
)


class _FakeToolRegistry:
    """Minimal tool registry stub exposing .get()."""

    def __init__(self, known_names):
        self._known = set(known_names)

    def get(self, name):
        return object() if name in self._known else None


class _FakeMotet:
    def __init__(self, tools=None):
        self.tools = tools


def _register_workflow(workflow: Workflow) -> None:
    if WorkflowRegistry.get(workflow.workflow_id):
        WorkflowRegistry.unregister(workflow.workflow_id)
    WorkflowRegistry.register(workflow)


def test_synthesize_prefilled_tool_call_canonical_shape():
    """Synthesized call carries canonical keys and JSON-encoded arguments."""
    prefilled = PrefilledToolCall(
        tool_name="workflow_test.prefilled_demo",
        arguments={"b": 2, "a": 1},
    )
    call = _synthesize_prefilled_tool_call(prefilled)
    assert call["tool_name"] == "workflow_test.prefilled_demo"
    assert call["arguments"] == {"a": 1, "b": 2}
    assert call["arguments_json"] == json.dumps({"a": 1, "b": 2}, sort_keys=True)
    assert call["call_id"].startswith("prefilled_")


def test_prefilled_stream_data_shape():
    """The stand-in mimics a model_stream tool-call result with zero usage."""
    prefilled = [PrefilledToolCall(tool_name="workflow_test.x", arguments={})]
    stream_data = prefilled_stream_data(prefilled)
    assert stream_data["finish_reason"] == "tool_calls"
    assert stream_data["final_content"] == ""
    assert stream_data["tokens_streamed"] == 0
    calls = stream_data["tool_calls_canonical"]
    assert len(calls) == 1
    assert calls[0]["tool_name"] == "workflow_test.x"


def test_prefilled_stream_data_multiple_calls_are_parallel():
    """Multiple prefilled entries become parallel canonical tool calls in one turn."""
    prefilled = [
        PrefilledToolCall(tool_name="memo.roster_lookup", arguments={"q": "23"}),
        PrefilledToolCall(tool_name="memo.media_index", arguments={"asset_id": "a1"}),
    ]
    stream_data = prefilled_stream_data(prefilled)
    calls = stream_data["tool_calls_canonical"]
    assert [c["tool_name"] for c in calls] == ["memo.roster_lookup", "memo.media_index"]
    # Distinct call ids so downstream dedup/execution treats them independently.
    assert calls[0]["call_id"] != calls[1]["call_id"]


def test_agentic_loop_data_coerces_dict_prefilled_tool_calls():
    """AgenticLoopData accepts a list of dicts and coerces to PrefilledToolCall list."""
    data = AgenticLoopData(
        input="go",
        prefilled_tool_calls=[{"tool_name": "workflow_test.x", "arguments": {"k": "v"}}],
    )
    assert isinstance(data.prefilled_tool_calls, list)
    assert isinstance(data.prefilled_tool_calls[0], PrefilledToolCall)
    assert data.prefilled_tool_calls[0].tool_name == "workflow_test.x"
    assert data.prefilled_tool_calls[0].arguments == {"k": "v"}


def test_agentic_loop_data_coerces_single_object_to_list():
    """A single dict/object is coerced to a one-element list for the common case."""
    data = AgenticLoopData(
        input="go",
        prefilled_tool_calls={"tool_name": "workflow_test.x", "arguments": {}},
    )
    assert isinstance(data.prefilled_tool_calls, list)
    assert len(data.prefilled_tool_calls) == 1
    assert data.prefilled_tool_calls[0].tool_name == "workflow_test.x"


def test_validate_accepts_registered_workflow():
    """A registered workflow tool with no excluding filter validates cleanly."""
    wf_id = "test.prefilled_valid"
    _register_workflow(Workflow(workflow_id=wf_id, name="Prefilled Valid"))
    try:
        prefilled = [PrefilledToolCall(tool_name=f"workflow_{wf_id}", arguments={})]
        assert validate_prefilled_tool_calls(_FakeMotet(), AgenticLoopData(), prefilled) is None
    finally:
        WorkflowRegistry.unregister(wf_id)


def test_validate_rejects_unknown_workflow():
    """An unregistered workflow tool fails validation loudly."""
    prefilled = [PrefilledToolCall(tool_name="workflow_test.does_not_exist", arguments={})]
    err = validate_prefilled_tool_calls(_FakeMotet(), AgenticLoopData(), prefilled)
    assert err is not None
    assert "Unknown workflow" in err


def test_validate_rejects_unknown_tool():
    """A non-workflow tool absent from the registry fails validation."""
    motet = _FakeMotet(tools=_FakeToolRegistry(known_names={"memo.roster_lookup"}))
    prefilled = [PrefilledToolCall(tool_name="memo.bogus", arguments={})]
    err = validate_prefilled_tool_calls(motet, AgenticLoopData(), prefilled)
    assert err is not None
    assert "Unknown tool" in err


def test_validate_accepts_known_tool():
    """A registered non-workflow tool validates cleanly."""
    motet = _FakeMotet(tools=_FakeToolRegistry(known_names={"memo.roster_lookup"}))
    prefilled = [PrefilledToolCall(tool_name="memo.roster_lookup", arguments={"q": "23"})]
    assert validate_prefilled_tool_calls(motet, AgenticLoopData(), prefilled) is None


def test_validate_rejects_any_invalid_entry_in_list():
    """When multiple calls are prefilled, the first invalid entry fails the turn."""
    motet = _FakeMotet(tools=_FakeToolRegistry(known_names={"memo.roster_lookup"}))
    prefilled = [
        PrefilledToolCall(tool_name="memo.roster_lookup", arguments={}),
        PrefilledToolCall(tool_name="memo.bogus", arguments={}),
    ]
    err = validate_prefilled_tool_calls(motet, AgenticLoopData(), prefilled)
    assert err is not None
    assert "Unknown tool" in err


def test_validate_rejects_empty_list():
    """An empty prefilled list is rejected rather than silently ignored."""
    err = validate_prefilled_tool_calls(_FakeMotet(), AgenticLoopData(), [])
    assert err is not None


def test_validate_rejects_excluded_tool():
    """A tool present in the registry but excluded by the tool filter is rejected."""
    motet = _FakeMotet(tools=_FakeToolRegistry(known_names={"memo.roster_lookup"}))
    data = AgenticLoopData(tool_filter_metadata={"exclude_tools": ["memo.roster_lookup"]})
    prefilled = [PrefilledToolCall(tool_name="memo.roster_lookup", arguments={})]
    err = validate_prefilled_tool_calls(motet, data, prefilled)
    assert err is not None
    assert "excluded" in err


def test_validate_rejects_workflow_when_no_workflows():
    """no_workflows in the tool filter blocks a prefilled workflow call."""
    wf_id = "test.prefilled_blocked"
    _register_workflow(Workflow(workflow_id=wf_id, name="Prefilled Blocked"))
    try:
        data = AgenticLoopData(tool_filter_metadata={"no_workflows": True})
        prefilled = [PrefilledToolCall(tool_name=f"workflow_{wf_id}", arguments={})]
        err = validate_prefilled_tool_calls(_FakeMotet(), data, prefilled)
        assert err is not None
        assert "Workflows are disabled" in err
    finally:
        WorkflowRegistry.unregister(wf_id)


def test_tool_filter_violation_none_when_no_metadata():
    """No tool filter metadata means no violation."""
    assert _prefilled_tool_filter_violation(None, "memo.anything") is None
    assert _prefilled_tool_filter_violation({}, "memo.anything") is None
