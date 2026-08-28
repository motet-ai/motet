"""
Motet - Agentic Loop Workflow Fast-Path Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Unit tests for opt-in workflow fast-path behavior in agentic_loop helpers.

Dependencies:
    - pytest: test framework
    - agentic_loop presentation/passthrough helpers

Usage:
    pytest tests/unit/core/test_agentic_loop_workflow_fast_path.py
"""

from motet.core.workflow import Workflow, WorkflowRegistry
from motet.core.reasoning.react.loop_execution import (
    _all_tools_user_facing,
    _extract_fast_path_tool_texts,
    _extract_passthrough_from_workflow_result,
    _get_workflow_presentation,
    maybe_fast_path_return,
)
from motet.core.reasoning.react.agentic_loop_data import AgenticLoopData
from motet.core.types import Message


class _FakeMotet:
    tools = None

    def __init__(self, redis=None):
        self.redis = redis
        self.streamed_tokens: list[str] = []
        self.events: list[tuple[str, dict]] = []

    def stream_token(self, token: str, *, stream_key: str) -> None:
        self.streamed_tokens.append(token)

    def stream_event(self, event_type: str, **kwargs) -> None:
        self.events.append((event_type, kwargs))


def _register_workflow(workflow: Workflow) -> None:
    if WorkflowRegistry.get(workflow.workflow_id):
        WorkflowRegistry.unregister(workflow.workflow_id)
    WorkflowRegistry.register(workflow)


def test_workflow_presentation_enables_fast_path():
    """Workflow tools with user_facing presentation should qualify for fast-path."""
    wf_id = "test.fast_path_demo"
    _register_workflow(
        Workflow(
            workflow_id=wf_id,
            name="Fast Path Demo",
            presentation={
                "user_facing": True,
                "requires_llm": False,
                "passthrough_field": "summary",
            },
        )
    )
    try:
        tool_calls = [{"tool_call_id": "call_1", "tool_name": f"workflow_{wf_id}"}]
        tool_results = [{"tool_call_id": "call_1", "tool_name": f"workflow_{wf_id}", "status": "success", "result": {}}]
        assert _all_tools_user_facing(_FakeMotet(), tool_calls, tool_results) is True
        assert _get_workflow_presentation(f"workflow_{wf_id}") == {
            "user_facing": True,
            "requires_llm": False,
            "passthrough_field": "summary",
        }
    finally:
        WorkflowRegistry.unregister(wf_id)


def test_workflow_without_presentation_skips_fast_path():
    """Workflow tools default to requiring a post-workflow LLM pass."""
    wf_id = "test.no_fast_path"
    _register_workflow(Workflow(workflow_id=wf_id, name="No Fast Path"))
    try:
        tool_calls = [{"tool_call_id": "call_1", "tool_name": f"workflow_{wf_id}"}]
        tool_results = [{"tool_call_id": "call_1", "tool_name": f"workflow_{wf_id}", "status": "success", "result": {}}]
        assert _all_tools_user_facing(_FakeMotet(), tool_calls, tool_results) is False
    finally:
        WorkflowRegistry.unregister(wf_id)


def test_extract_passthrough_from_flat_agent_turn_step():
    """Last agent_turn step exposes the field on the payload, not under data."""
    workflow_result = {
        "output_field": "final_response",
        "step_results": {
            "analyze_optimist": {"final_response": "pro"},
            "synthesize": {"agent_id": "expert-panel.synthesizer", "final_response": "## Take"},
        },
    }
    text = _extract_passthrough_from_workflow_result(
        workflow_result,
        {"passthrough_field": "final_response"},
    )
    assert text == "## Take"


def test_extract_passthrough_from_workflow_result_json_fence():
    """Passthrough extraction should honor output_field and json_fence wrapping."""
    workflow_result = {
        "output_field": "agent_response",
        "step_results": {
            "finalize": {
                "status": "success",
                "data": {
                    "agent_response": '{"status":"recommendations_ready","message":"ok"}',
                },
            }
        },
    }
    presentation = {
        "user_facing": True,
        "requires_llm": False,
        "response_wrap": "json_fence",
    }
    text = _extract_passthrough_from_workflow_result(workflow_result, presentation)
    assert text is not None
    assert text.startswith("```json")
    assert "recommendations_ready" in text


def test_maybe_fast_path_return_streams_workflow_passthrough():
    """Configured workflows should return passthrough content without another LLM iteration."""
    wf_id = "test.stream_demo"
    _register_workflow(
        Workflow(
            workflow_id=wf_id,
            name="Stream Demo",
            output_field="digest_markdown",
            presentation={
                "user_facing": True,
                "requires_llm": False,
                "response_wrap": "json_fence",
            },
        )
    )
    try:
        motet = _FakeMotet(redis=object())
        data = AgenticLoopData(
            input="run workflow",
            conversation_history=[
                Message(role="user", content="run workflow"),
                Message(
                    role="tool",
                    tool_call_id="call_1",
                    name=f"workflow_{wf_id}",
                    content="✅ Workflow completed (formatted for LLM)",
                ),
            ],
            stream_key="task:test:response",
            max_iterations=3,
            remaining_iterations=2,
        )
        workflow_result = {
            "output_field": "digest_markdown",
            "step_results": {
                "final": {
                    "status": "success",
                    "data": {"digest_markdown": '{"status":"done"}'},
                }
            },
        }
        unique_tool_calls = [{"tool_call_id": "call_1", "tool_name": f"workflow_{wf_id}"}]
        tool_results = [
            {
                "tool_call_id": "call_1",
                "tool_name": f"workflow_{wf_id}",
                "status": "success",
                "result": workflow_result,
            }
        ]

        result = maybe_fast_path_return(
            motet,
            data,
            unique_tool_calls,
            tool_results,
            iterations_used=1,
            accumulated_usage={},
        )

        assert result is not None
        assert result["stop_reason"] == "stop"
        assert result["final_response"].startswith("```json")
        assert "done" in result["final_response"]
        assert motet.streamed_tokens == [f"\n\n{result['final_response']}"]
        assert any(event[0] == "agentic_loop_complete" for event in motet.events)
    finally:
        WorkflowRegistry.unregister(wf_id)


def test_extract_fast_path_tool_texts_prefers_workflow_passthrough():
    """Fast-path text extraction should not use formatted workflow step markdown."""
    wf_id = "test.extract_demo"
    workflow_result = {
        "output_field": "report",
        "step_results": {
            "final": {
                "status": "success",
                "data": {"report": "FINAL REPORT"},
            }
        },
    }
    data = AgenticLoopData(
        input="run",
        conversation_history=[
            Message(
                role="tool",
                tool_call_id="call_1",
                name=f"workflow_{wf_id}",
                content="✅ Workflow 'Demo' completed successfully",
            )
        ],
    )
    texts = _extract_fast_path_tool_texts(
        _FakeMotet(),
        data,
        [{"tool_call_id": "call_1", "tool_name": f"workflow_{wf_id}"}],
        [
            {
                "tool_call_id": "call_1",
                "tool_name": f"workflow_{wf_id}",
                "status": "success",
                "result": workflow_result,
            }
        ],
    )
    assert texts == ["FINAL REPORT"]


def _dispatch_call(wf_id: str) -> dict:
    """What the batch looks like when the model dispatches via the meta-tool."""
    return {
        "tool_call_id": "call_1",
        "tool_name": "core.tool_call",
        "parameters": {"tool_name": f"workflow_{wf_id}", "parameters": {"topic": "x"}},
    }


def _dispatch_result(wf_id: str, workflow_result: dict) -> dict:
    """core.tool_call wraps the workflow result in its ok() envelope."""
    return {
        "tool_call_id": "call_1",
        "tool_name": "core.tool_call",
        "status": "success",
        "result": {
            "status": "success",
            "result": workflow_result,
            "meta": {
                "tool_name": f"workflow_{wf_id}",
                "kind": "workflow",
                "workflow_id": wf_id,
            },
        },
    }


class TestDispatchedThroughToolCall:
    """
    After core.tools_search the model dispatches through core.tool_call, so the
    outer name in the batch is the meta-tool and the presentation opt-in on the
    workflow underneath has to still be honored.
    """

    def test_presentation_is_read_from_the_dispatched_workflow(self):
        wf_id = "test.dispatched_demo"
        _register_workflow(
            Workflow(
                workflow_id=wf_id,
                name="Dispatched Demo",
                presentation={"user_facing": True, "requires_llm": False},
            )
        )
        try:
            assert (
                _all_tools_user_facing(
                    _FakeMotet(),
                    [_dispatch_call(wf_id)],
                    [_dispatch_result(wf_id, {})],
                )
                is True
            )
        finally:
            WorkflowRegistry.unregister(wf_id)

    def test_passthrough_is_unwrapped_from_the_dispatch_envelope(self):
        wf_id = "test.dispatched_extract"
        _register_workflow(
            Workflow(
                workflow_id=wf_id,
                name="Dispatched Extract",
                presentation={"user_facing": True, "requires_llm": False},
            )
        )
        workflow_result = {
            "output_field": "report",
            "step_results": {"final": {"status": "success", "data": {"report": "FINAL REPORT"}}},
        }
        data = AgenticLoopData(
            input="run",
            conversation_history=[
                Message(
                    role="tool",
                    tool_call_id="call_1",
                    name="core.tool_call",
                    content="tool_call(target_status=ok)",
                )
            ],
        )
        try:
            texts = _extract_fast_path_tool_texts(
                _FakeMotet(),
                data,
                [_dispatch_call(wf_id)],
                [_dispatch_result(wf_id, workflow_result)],
            )
            assert texts == ["FINAL REPORT"]
        finally:
            WorkflowRegistry.unregister(wf_id)

    def test_dispatching_a_plain_tool_does_not_fast_path(self):
        """
        core.tool_call's own observation is a status line, so fast-pathing a
        tool through it would hand the user the dispatch summary.
        """
        call = {
            "tool_call_id": "call_1",
            "tool_name": "core.tool_call",
            "parameters": {"tool_name": "expert-panel.recall_discussion"},
        }
        result = {
            "tool_call_id": "call_1",
            "tool_name": "core.tool_call",
            "status": "success",
            "result": {"status": "success", "result": {"results": []}},
        }
        assert _all_tools_user_facing(_FakeMotet(), [call], [result]) is False

    def test_passthrough_reads_flat_agent_turn_step_payload(self):
        """agent_turn steps store {agent_id, final_response}, not {status, data}."""
        wf_id = "test.flat_agent_turn"
        _register_workflow(
            Workflow(
                workflow_id=wf_id,
                name="Flat Agent Turn",
                output_field="final_response",
                presentation={
                    "user_facing": True,
                    "requires_llm": False,
                    "passthrough_field": "final_response",
                },
            )
        )
        workflow_result = {
            "status": "completed",
            "output_field": "final_response",
            "step_results": {
                "analyze_optimist": {
                    "agent_id": "expert-panel.optimist",
                    "final_response": "# Optimistic take\n\n- point",
                },
                "analyze_skeptic": {
                    "agent_id": "expert-panel.skeptic",
                    "final_response": "# Skeptical take\n\n- risk",
                },
                "synthesize": {
                    "agent_id": "expert-panel.synthesizer",
                    "final_response": "## Executive Summary\n\nBalanced.",
                },
            },
            "presentation": {
                "user_facing": True,
                "requires_llm": False,
                "passthrough_field": "final_response",
            },
        }
        data = AgenticLoopData(
            input="run",
            conversation_history=[
                Message(
                    role="tool",
                    tool_call_id="call_1",
                    name="core.tool_call",
                    content='{ "status": "success", "result": { "step_results": {} } } [full result in artifact_id=x]',
                )
            ],
        )
        try:
            texts = _extract_fast_path_tool_texts(
                _FakeMotet(),
                data,
                [_dispatch_call(wf_id)],
                [_dispatch_result(wf_id, workflow_result)],
            )
            assert texts == ["## Executive Summary\n\nBalanced."]
        finally:
            WorkflowRegistry.unregister(wf_id)

    def test_missing_passthrough_does_not_dump_workflow_observation(self):
        """Failed extract must not surface the clipped core.tool_call JSON."""
        wf_id = "test.missing_field"
        _register_workflow(
            Workflow(
                workflow_id=wf_id,
                name="Missing Field",
                presentation={
                    "user_facing": True,
                    "requires_llm": False,
                    "passthrough_field": "final_response",
                },
            )
        )
        workflow_result = {
            "output_field": "final_response",
            "step_results": {"final": {"status": "success", "data": {"other": "nope"}}},
        }
        data = AgenticLoopData(
            input="run",
            conversation_history=[
                Message(
                    role="tool",
                    tool_call_id="call_1",
                    name="core.tool_call",
                    content="HUGE WORKFLOW JSON DUMP",
                )
            ],
        )
        try:
            texts = _extract_fast_path_tool_texts(
                _FakeMotet(),
                data,
                [_dispatch_call(wf_id)],
                [_dispatch_result(wf_id, workflow_result)],
            )
            assert texts == []
        finally:
            WorkflowRegistry.unregister(wf_id)

    def test_falls_back_to_call_parameters_without_result_meta(self):
        wf_id = "test.dispatched_no_meta"
        _register_workflow(
            Workflow(
                workflow_id=wf_id,
                name="Dispatched No Meta",
                presentation={"user_facing": True, "requires_llm": False},
            )
        )
        result = {
            "tool_call_id": "call_1",
            "tool_name": "core.tool_call",
            "status": "success",
            "result": {"status": "success", "result": {}},
        }
        try:
            assert (
                _all_tools_user_facing(_FakeMotet(), [_dispatch_call(wf_id)], [result]) is True
            )
        finally:
            WorkflowRegistry.unregister(wf_id)
