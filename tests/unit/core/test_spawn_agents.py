"""
Motet - Spawn Agents Tool Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-31

Description:
    Unit coverage for ``core.spawn_agents``, the parallel sub-agent fan-out.

    The rails carry most of the weight here, because the failure modes are
    expensive rather than wrong-looking: unbounded recursion (depth 3 at width
    10 is a thousand agents), scope widening (a child reaching tools its parent
    was denied), and silent truncation of work the model believes it dispatched.

Dependencies:
    - pytest: test framework
    - unittest.mock: stands in for MotetContext and the agent command

Usage:
    pytest tests/unit/core/test_spawn_agents.py

Notes:
    - ``motet.join`` is stubbed; these assert the fan-out's contract with it
      (one AgentData per task, in order, with the inherited filter), not Celery.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from motet.core.agents.registry import CORE_SUBAGENT_ID, get_agent_registry
from motet.core.tools.builtin.spawn_agents import (
    MAX_FANOUT_WIDTH,
    TOOL_NAME,
    _thinking_text,
    _tool_summaries,
    run_spawn_agents,
    spawn_child_id,
)


def _core_subagent():
    cfg = get_agent_registry().get(CORE_SUBAGENT_ID)
    assert cfg is not None
    return cfg

DISCOVERY_FILTER: Dict[str, Any] = {
    "exclude_tools": ["core.host_exec"],
    "exclude_workflows": None,
    "no_workflows": False,
    "required_tools": ["core.web_search"],
    "required_workflows": [],
    "prefix": [],
    "category": [],
}


class _FakeMotet:
    """MotetContext stand-in that records the calls handed to join()."""

    def __init__(self, metadata: Optional[Dict[str, Any]] = None, results: Any = None):
        self.metadata = metadata if metadata is not None else {"tool_filter_metadata": dict(DISCOVERY_FILTER)}
        self.task_id = "task-1"
        self.stream_key = "task:task-1:response"
        self.conversation_id = None
        self.tenant_id = "t1"
        self.principal_id = "p1"
        self.motet_id = "default"
        self.command_id = "cmd-1"
        self.joined: List[Any] = []
        self._results = results

    def join(self, calls: List[Any], **_kwargs: Any) -> List[Any]:
        self.joined = calls
        if self._results is not None:
            return self._results
        return [
            {"final_response": f"answer {i}", "tool_results": []}
            for i in range(len(calls))
        ]


def _run(motet: _FakeMotet, tasks: List[Any]) -> Dict[str, Any]:
    with patch(
        "motet.core.commands.decorator.get_motet_context",
        return_value=motet,
    ):
        return run_spawn_agents({"tasks": tasks})


def _join_data(item: Any) -> Any:
    if isinstance(item, tuple):
        return item[1]
    return getattr(item, "data", None)


def _join_conversation_id(item: Any) -> Optional[str]:
    if isinstance(item, tuple):
        return None
    ctx = getattr(item, "distributed_context", None)
    return getattr(ctx, "conversation_id", None) if ctx is not None else None


def _agent_payloads(motet: _FakeMotet) -> List[Any]:
    return [_join_data(item) for item in motet.joined]


# --- rails -----------------------------------------------------------------


# The bound checks below call run_spawn_agents directly, which is not how a
# model reaches the tool. `tool_execution` validates parameters against the
# registered SpawnAgentsParams schema first, so in production a bad count is
# rejected there with Pydantic's wording and these branches never run. They are
# tested because direct invocation is a real entry point, not because these are
# the messages a model sees.
def test_rejects_a_single_task():
    """One task is not a fan-out; the loop should just call the tool it needs."""
    result = _run(_FakeMotet(), ["only one thing"])

    assert result["status"] == "error"
    assert "at least 2" in result["error"]


def test_rejects_more_tasks_than_the_width_cap_and_says_the_limit():
    """Truncating would let the model believe work ran that never did."""
    tasks = [f"task {i}" for i in range(MAX_FANOUT_WIDTH + 3)]
    result = _run(_FakeMotet(), tasks)

    assert result["status"] == "error"
    assert str(MAX_FANOUT_WIDTH) in result["error"]
    assert str(len(tasks)) in result["error"]


def test_accepts_exactly_the_width_cap():
    motet = _FakeMotet()
    result = _run(motet, [f"task {i}" for i in range(MAX_FANOUT_WIDTH)])

    assert result["status"] == "success"
    assert len(motet.joined) == MAX_FANOUT_WIDTH


def test_children_cannot_fan_out_again():
    """Recursion is bounded by subtracting the tool, not by a depth counter."""
    motet = _FakeMotet()
    _run(motet, ["a", "b"])

    for payload in _agent_payloads(motet):
        assert TOOL_NAME in payload.tool_filter_metadata["exclude_tools"]


def test_children_inherit_the_parents_exclusions():
    """A sub-agent must not reach a tool the parent agent was denied."""
    motet = _FakeMotet()
    _run(motet, ["a", "b"])

    excluded = _agent_payloads(motet)[0].tool_filter_metadata["exclude_tools"]
    assert "core.host_exec" in excluded


def test_children_get_no_handback_tools():
    """Handback suspends the turn (ADR-0127); a sub-agent has no caller."""
    motet = _FakeMotet()
    _run(motet, ["a", "b"])

    for payload in _agent_payloads(motet):
        assert not getattr(payload, "handback_tools", None)
        assert not getattr(payload, "handback_tool_names", None)


def test_refuses_when_the_parent_has_no_delegable_filter():
    """Without the parent's filter, inheritance would guess at the child's scope."""
    motet = _FakeMotet(metadata={})
    result = _run(motet, ["a", "b"])

    assert result["status"] == "error"
    assert "discovery-mode" in result["error"]
    assert motet.joined == []


def test_a_tool_pinned_and_denied_is_not_left_pinned():
    """required_tools is a discovery hint; keeping a denied pin asks for both."""
    filter_metadata = dict(DISCOVERY_FILTER)
    filter_metadata["required_tools"] = [TOOL_NAME, "core.web_search"]
    motet = _FakeMotet(metadata={"tool_filter_metadata": filter_metadata})
    _run(motet, ["a", "b"])

    child = _agent_payloads(motet)[0].tool_filter_metadata
    assert TOOL_NAME not in child["required_tools"]
    assert "core.web_search" in child["required_tools"]


def test_parent_filter_is_not_mutated():
    """The parent keeps its own tool for the rest of the turn."""
    metadata = {"tool_filter_metadata": dict(DISCOVERY_FILTER)}
    motet = _FakeMotet(metadata=metadata)
    _run(motet, ["a", "b"])

    assert TOOL_NAME not in metadata["tool_filter_metadata"]["exclude_tools"]


# --- dispatch and results ---------------------------------------------------


def test_one_sub_agent_per_task_in_order():
    motet = _FakeMotet()
    _run(motet, ["research pricing", "read the postmortems"])

    payloads = _agent_payloads(motet)
    assert [p.input for p in payloads] == ["research pricing", "read the postmortems"]
    assert len({p.agent_id for p in payloads}) == 2


def test_spawn_child_id_is_one_based():
    assert spawn_child_id("core.default", 0) == "core.default.spawn-1"
    assert spawn_child_id("core.default", 1) == "core.default.spawn-2"


def test_children_write_to_the_parent_task_stream_with_their_own_agent_id():
    """Chat UI watches the parent task stream; children must not use a sidecar key."""
    motet = _FakeMotet(
        metadata={
            "tool_filter_metadata": dict(DISCOVERY_FILTER),
            "agent_id": "core.default",
            "enable_thinking": True,
        }
    )
    _run(motet, ["research pricing", "read the postmortems"])

    payloads = _agent_payloads(motet)
    assert [p.agent_id for p in payloads] == [
        "core.default.spawn-1",
        "core.default.spawn-2",
    ]
    for payload in payloads:
        assert payload.use_task_stream is True
        assert payload.base_stream_key == "task:task-1:response"
        assert payload.parent_agent_id == "core.default"
        assert payload.metadata["agent_id"] == payload.agent_id
        assert payload.metadata["parent_agent_id"] == "core.default"
        assert payload.enable_thinking is True
    # Copy-on-write: child metadata must not retag the parent dict.
    assert motet.metadata["agent_id"] == "core.default"


def test_spawn_child_id_is_parent_dot_spawn_n():
    """Stream id stays {parent}.spawn-N; conversation id is minted separately."""
    from motet.core.conversations.lineage import mint_isolated_conversation

    first = mint_isolated_conversation("conv-1", kind="spawn").conversation_id
    second = mint_isolated_conversation("conv-1", kind="spawn").conversation_id
    assert first.startswith("iso-")
    assert second.startswith("iso-")
    assert first != second
    assert spawn_child_id("core.default", 0) == "core.default.spawn-1"


def test_joins_each_child_loop_on_its_isolated_conversation_id():
    motet = _FakeMotet()
    motet.conversation_id = "conv-1"
    lineage: List[Dict[str, Any]] = []

    with patch(
        "motet.core.conversations.lineage.record_conversation_lineage_sync",
        side_effect=lambda **kw: lineage.append(kw) or "conv-1",
    ):
        result = _run(motet, ["research pricing", "read the postmortems"])

    joined = [_join_conversation_id(item) for item in motet.joined]
    assert len(joined) == 2
    assert joined[0] != joined[1]
    assert all(cid and cid.startswith("iso-") and cid != "conv-1" for cid in joined)
    assert [row["child_conversation_id"] for row in lineage] == joined
    assert lineage[0]["parent_conversation_id"] == "conv-1"
    assert lineage[0]["root_conversation_id"] == "conv-1"
    assert result["result"]["results"][0]["child_conversation_id"] == joined[0]
    assert result["result"]["results"][1]["child_conversation_id"] == joined[1]


def test_thinking_text_peels_nested_loop_payload() -> None:
    assert _thinking_text({"thinking_text": "pick two capitals"}) == "pick two capitals"
    assert _thinking_text({"data": {"thinking_text": "why two capitals"}}) == "why two capitals"
    assert _thinking_text({"result": {"reasoning_content": "look it up"}}) == "look it up"
    assert _thinking_text({"final_response": "Austin"}) == ""


def test_tool_summaries_peels_nested_loop_payload() -> None:
    rows = [{"tool_name": "core.web_search", "status": "success", "preview": "Austin"}]
    assert _tool_summaries({"tool_summaries": rows}) == rows
    assert _tool_summaries({"data": {"tool_summaries": rows}}) == rows
    assert _tool_summaries(
        {"result": {"tool_results": [{"tool_name": "core.web_search", "status": "success", "result": "Austin"}]}}
    )[0]["tool_name"] == "core.web_search"


def test_persists_successful_children_on_isolated_conversations():
    """First turn lives on the child cid; the parent keeps a card pointer."""
    motet = _FakeMotet(
        metadata={
            "tool_filter_metadata": dict(DISCOVERY_FILTER),
            "agent_id": "core.default",
            "surface_id": "demo_chat",
        },
        results=[
            {
                "final_response": "price is 12",
                "thinking_text": "look up the list price",
                "tool_summaries": [
                    {"tool_name": "core.web_search", "status": "success", "preview": "list price"},
                ],
                "cost_usd": 0.004,
            },
            {"final_response": "outage notes"},
        ],
    )
    motet.conversation_id = "conv-1"
    motet.memory = object()
    stored: List[Dict[str, Any]] = []
    registered: List[Dict[str, Any]] = []

    def _store(_motet: Any, messages: Any, text: str, **kwargs: Any) -> Dict[str, Any]:
        stored.append(
            {
                "conversation_id": kwargs.get("conversation_id"),
                "agent_id": kwargs.get("agent_id"),
                "text": text,
                "user": getattr(messages[0], "content", None) if messages else None,
                "thinking_text": kwargs.get("thinking_text"),
                "tool_summaries": kwargs.get("tool_summaries"),
                "cost_usd": kwargs.get("cost_usd"),
                "include_tool_invocations": kwargs.get("include_tool_invocations"),
                "root_turn": kwargs.get("root_turn"),
            }
        )
        return {"canonical_transcript_stored": True}

    def _register(*args: Any, **kwargs: Any) -> None:
        registered.append(
            {
                "id": args[3] if len(args) > 3 else kwargs.get("conversation_id"),
                "title": kwargs.get("title"),
                "agent_id": kwargs.get("agent_id"),
                "turn_agent_id": kwargs.get("turn_agent_id"),
                "surface_id": kwargs.get("surface_id"),
            }
        )

    with (
        patch(
            "motet.core.conversations.transcript_storage.store_turn_transcript",
            side_effect=_store,
        ),
        patch(
            "motet.core.conversations.registry.register_or_touch_conversation_sync",
            side_effect=_register,
        ),
        patch(
            "motet.core.conversations.ownership.authorize_conversation_access_sync",
            return_value=None,
        ),
        patch(
            "motet.core.conversations.lineage.record_conversation_lineage_sync",
            return_value="conv-1",
        ),
    ):
        result = _run(motet, ["research pricing", "read the postmortems"])

    assert result["status"] == "success"
    joined = [_join_conversation_id(item) for item in motet.joined]
    briefs = [row for row in stored if row["text"] == ""]
    replies = [row for row in stored if row["text"]]
    assert [row["conversation_id"] for row in briefs] == joined
    assert [row["user"] for row in briefs] == ["research pricing", "read the postmortems"]
    assert [row["conversation_id"] for row in replies] == joined
    assert replies[0]["user"] is None
    assert replies[0]["text"] == "price is 12"
    assert replies[0]["root_turn"] is False
    assert replies[0]["thinking_text"] == "look up the list price"
    assert replies[0]["tool_summaries"] == [
        {"tool_name": "core.web_search", "status": "success", "preview": "list price"}
    ]
    assert replies[0]["cost_usd"] == 0.004
    assert replies[0]["include_tool_invocations"] is True
    assert replies[0]["agent_id"] == "core.subagent"
    cards = result["meta"]["spawn_children"]
    assert cards[0]["child_conversation_id"] == joined[0]
    assert cards[0]["title"] == "research pricing"
    assert cards[0]["agent_id"] == "core.default.spawn-1"
    assert cards[0]["turn_agent_id"] == "core.subagent"
    assert cards[0]["thinking_text"] == "look up the list price"
    assert cards[0]["tool_summaries"] == [
        {"tool_name": "core.web_search", "status": "success", "preview": "list price"}
    ]
    assert "spawn_children" not in motet.metadata
    assert {row["id"] for row in registered} == set(joined)
    assert registered[0]["title"] == "research pricing"
    assert registered[0]["agent_id"] == "core.default"
    assert registered[0]["turn_agent_id"] == "core.subagent"
    assert registered[0]["surface_id"] == "demo_chat"
    assert registered[1]["title"] == "read the postmortems"


def test_does_not_persist_incomplete_child_rows():
    motet = _FakeMotet(
        metadata={
            "tool_filter_metadata": dict(DISCOVERY_FILTER),
            "agent_id": "core.default",
        },
        results=[
            {"final_response": "", "stop_reason": "max_iterations"},
            {"final_response": "kept this one"},
        ],
    )
    motet.conversation_id = "conv-1"
    motet.memory = object()
    stored: List[str] = []

    with (
        patch(
            "motet.core.conversations.transcript_storage.store_turn_transcript",
            side_effect=lambda *_a, **kw: stored.append(kw["conversation_id"]) or {},
        ),
        patch(
            "motet.core.conversations.registry.register_or_touch_conversation_sync",
            return_value=None,
        ),
        patch(
            "motet.core.conversations.ownership.authorize_conversation_access_sync",
            return_value=None,
        ),
        patch(
            "motet.core.conversations.lineage.record_conversation_lineage_sync",
            return_value="conv-1",
        ),
    ):
        _run(motet, ["a", "b"])

    joined = [_join_conversation_id(item) for item in motet.joined]
    assert stored.count(joined[0]) == 1
    assert stored.count(joined[1]) == 2


def test_child_transcript_failure_does_not_fail_the_tool():
    motet = _FakeMotet(
        metadata={
            "tool_filter_metadata": dict(DISCOVERY_FILTER),
            "agent_id": "core.default",
        }
    )
    motet.conversation_id = "conv-1"
    motet.memory = object()

    with (
        patch(
            "motet.core.conversations.transcript_storage.store_turn_transcript",
            side_effect=RuntimeError("memory down"),
        ),
        patch(
            "motet.core.conversations.lineage.record_conversation_lineage_sync",
            return_value="conv-1",
        ),
    ):
        result = _run(motet, ["a", "b"])

    assert result["status"] == "success"


def test_partial_child_persist_still_yields_a_card_per_success():
    """A failed transcript write for one child synthesizes its card anyway."""
    motet = _FakeMotet(
        metadata={
            "tool_filter_metadata": dict(DISCOVERY_FILTER),
            "agent_id": "core.default",
        },
        results=[
            {"final_response": "price is 12"},
            {"final_response": "outage notes"},
        ],
    )
    motet.conversation_id = "conv-1"
    motet.memory = object()

    def _store(_motet: Any, _messages: Any, text: str, **_kw: Any) -> Dict[str, Any]:
        if text == "outage notes":
            raise RuntimeError("memory down")
        return {"canonical_transcript_stored": True}

    with (
        patch(
            "motet.core.conversations.transcript_storage.store_turn_transcript",
            side_effect=_store,
        ),
        patch(
            "motet.core.conversations.registry.register_or_touch_conversation_sync",
            return_value=None,
        ),
        patch(
            "motet.core.conversations.ownership.authorize_conversation_access_sync",
            return_value=None,
        ),
        patch(
            "motet.core.conversations.lineage.record_conversation_lineage_sync",
            return_value="conv-1",
        ),
    ):
        result = _run(motet, ["research pricing", "read the postmortems"])

    assert result["status"] == "success"
    joined = [_join_conversation_id(item) for item in motet.joined]
    cards = result["meta"]["spawn_children"]
    assert [card["child_conversation_id"] for card in cards] == joined
    assert cards[0]["preview"] == "price is 12"
    assert cards[1]["preview"] == "outage notes"
    assert cards[1]["agent_id"] == "core.default.spawn-2"


def _system_contents(payload: Any) -> List[str]:
    texts: List[str] = []
    for msg in payload.conversation_history or []:
        role = getattr(msg, "role", None) or (
            msg.get("role") if isinstance(msg, dict) else None
        )
        content = getattr(msg, "content", None) or (
            msg.get("content") if isinstance(msg, dict) else None
        )
        if role == "system" and isinstance(content, str):
            texts.append(content)
    return texts


def test_sub_agents_do_not_inherit_the_transcript():
    """A child holding the transcript re-answers the user, not its slice."""
    motet = _FakeMotet()
    _run(motet, ["a", "b"])

    for payload in _agent_payloads(motet):
        # Bare instructions declare no tools, so the child is on discovery.
        assert _system_contents(payload) == [
            _core_subagent().metadata["discovery_system_prompt"]
        ]
        assert len(payload.conversation_history) == 1


def test_sibling_system_prompts_are_identical_so_the_prefix_stays_cacheable():
    """Per-task tool names belong on required_tools, not in the system string."""
    motet = _FakeMotet()
    _run(
        motet,
        [
            {"instruction": "pricing", "tools": ["core.web_search"]},
            {"instruction": "docs", "tools": ["core.http_get_browser"]},
        ],
    )

    payloads = _agent_payloads(motet)
    texts = [_system_contents(p)[0] for p in payloads]
    cfg = _core_subagent()
    assert texts[0] == texts[1] == cfg.system_prompt
    assert f"{cfg.max_iterations} tool rounds" in texts[0]
    assert f"{cfg.max_tools} tool calls" in texts[0]
    seconds = int(cfg.metadata["max_tool_time_ms"]) // 1000
    assert f"{seconds} seconds of tool time" in texts[0]
    assert "core.web_search" not in texts[0]
    assert "core.http_get_browser" not in texts[0]
    assert "http_get_browser" not in texts[0]
    assert "core.web_search" in payloads[0].tool_filter_metadata["required_tools"]
    assert "core.http_get_browser" in payloads[1].tool_filter_metadata["required_tools"]


def test_sub_agents_get_tighter_spend_rails_than_the_parent_defaults():
    """Eight children inheriting the parent $0.75 ceiling would be a $6 fan-out."""
    cfg = _core_subagent()
    motet = _FakeMotet()
    _run(motet, ["a", "b"])

    for payload in _agent_payloads(motet):
        assert payload.max_iterations == cfg.max_iterations
        assert payload.max_cost_usd == cfg.max_cost_usd
        assert payload.max_prompt_tokens == cfg.max_prompt_tokens
        assert payload.max_tool_time_ms == cfg.metadata["max_tool_time_ms"]
        assert cfg.max_cost_usd is not None and cfg.max_cost_usd < 0.75


def test_spawn_rails_follow_the_registered_subagent():
    """A registry edit is the rail source — spawn must not keep a second copy."""
    registry = get_agent_registry()
    original = registry.get(CORE_SUBAGENT_ID)
    assert original is not None
    override = original.model_copy(update={"max_iterations": 3, "max_cost_usd": 0.05})
    registry.register_agent(override)
    try:
        motet = _FakeMotet()
        _run(motet, ["a", "b"])
        payload = _agent_payloads(motet)[0]
        assert payload.max_iterations == 3
        assert payload.max_cost_usd == 0.05
    finally:
        registry.register_agent(original)


def test_sub_agents_inherit_the_turns_model():
    """Fan-out must not silently drop to a different provider than the turn."""
    motet = _FakeMotet(
        metadata={
            "tool_filter_metadata": dict(DISCOVERY_FILTER),
            "model_provider": "anthropic",
            "model_name": "claude-sonnet-4",
            "reasoning_effort": "high",
        }
    )
    _run(motet, ["a", "b"])

    payload = _agent_payloads(motet)[0]
    assert payload.model_provider == "anthropic"
    assert payload.model_name == "claude-sonnet-4"
    assert payload.reasoning_effort == "high"


def test_results_come_back_in_task_order_with_provenance():
    motet = _FakeMotet(
        results=[
            {"final_response": "pricing is X", "tool_results": [
                {"status": "success", "tool_name": "core.web_search"},
            ]},
            {"final_response": "no outages", "tool_results": []},
        ]
    )
    result = _run(motet, ["pricing", "outages"])

    entries = result["result"]["results"]
    assert [e["task"] for e in entries] == ["pricing", "outages"]
    assert entries[0]["response"] == "pricing is X"
    assert entries[0]["tools_used"] == ["core.web_search"]


def test_tools_used_falls_back_to_executed_signatures_when_rail_clears_results():
    motet = _FakeMotet(
        results=[
            {
                "final_response": "pricing is X",
                "stop_reason": "max_tool_time",
                "finalized": True,
                "tool_results": [],
                "executed_signatures": [
                    "core.web_search:abcd1234",
                    "core.http_get_browser:efef5678",
                ],
            },
            {"final_response": "ok", "stop_reason": "stop", "tool_results": []},
        ]
    )
    result = _run(motet, ["pricing", "outages"])
    assert result["result"]["results"][0]["tools_used"] == [
        "core.web_search",
        "core.http_get_browser",
    ]


def _gather_error(partial: List[Any]) -> Exception:
    """A GatherExecutionError shaped the way join actually raises one.

    ``partial_results`` are already unwrapped (domain data or ``{_error}``).
    """
    from motet.core.commands.response_models import GatherExecutionError

    return GatherExecutionError(
        error_type="PartialGroupFailure",
        message="1 of 2 commands failed",
        details={},
        recoverable=True,
        command_type="core.gather",
        command_id="g1",
        partial_results=partial,
    )


def test_one_failed_branch_does_not_lose_the_others():
    """join raises on *partial* failure; the good branches must still come back.

    `fail_fast=False` stops gather cancelling siblings, it does not stop the
    raise. Catching only the generic exception would throw away every successful
    sub-agent because one timed out.
    """
    motet = _FakeMotet()

    def _raise(*_args: Any, **_kwargs: Any):
        raise _gather_error(
            [
                {"_error": True, "message": "worker timed out"},
                {"final_response": "found it", "tool_results": []},
            ]
        )

    motet.join = _raise  # type: ignore[method-assign]
    result = _run(motet, ["a", "b"])

    assert result["status"] == "success"
    entries = result["result"]["results"]
    assert entries[0]["status"] == "error"
    assert "timed out" in entries[0]["error"]
    assert entries[1]["response"] == "found it"
    assert result["meta"]["succeeded"] == 1


def test_all_branches_failing_is_a_failed_tool_call():
    """An all-errors payload reported as success reads as findings to the model."""
    motet = _FakeMotet()

    def _raise(*_args: Any, **_kwargs: Any):
        raise _gather_error(
            [{"_error": True, "message": "worker timed out"} for _ in range(2)]
        )

    motet.join = _raise  # type: ignore[method-assign]
    result = _run(motet, ["a", "b"])

    assert result["status"] == "error"
    assert "timed out" in result["error"]


def test_join_failure_is_reported_not_swallowed():
    motet = _FakeMotet()

    def _boom(*_args: Any, **_kwargs: Any):
        raise RuntimeError("broker unreachable")

    motet.join = _boom  # type: ignore[method-assign]
    result = _run(motet, ["a", "b"])

    assert result["status"] == "error"
    assert "broker unreachable" in result["error"]


@pytest.mark.parametrize("tasks", [["a", "", "b"], ["a", "   ", "b"]])
def test_blank_tasks_are_dropped_before_dispatch(tasks: List[str]):
    """A blank task would spawn an agent with no instruction."""
    motet = _FakeMotet()
    result = _run(motet, tasks)

    assert result["status"] == "success"
    assert len(motet.joined) == 2


# --- equipping children -----------------------------------------------------


def _echo_schemas(_motet: Any, names: List[str]) -> List[Dict[str, str]]:
    return [{"name": name} for name in names]


def test_a_task_gets_the_tools_it_declared():
    """Permission without equipment made children shop instead of work.

    Observed live: every child of a three-way fan-out spent its whole budget on
    core.tools_search / core.tool_call and returned nothing. Declared names
    that resolve become AgentData.tools (the catalog). When they do not
    resolve, they stay a required_tools pin so discovery still force-includes
    them.
    """
    motet = _FakeMotet()
    _run(
        motet,
        [
            {"instruction": "check pricing", "tools": ["core.http_get_browser"]},
            {"instruction": "read postmortems", "tools": ["core.artifact_read"]},
        ],
    )

    first, second = _agent_payloads(motet)
    assert "core.http_get_browser" in first.tool_filter_metadata["required_tools"]
    assert "core.artifact_read" in second.tool_filter_metadata["required_tools"]


def test_each_task_is_equipped_separately():
    """One task's declaration must not leak into a sibling's child."""
    motet = _FakeMotet()
    _run(
        motet,
        [
            {"instruction": "a", "tools": ["core.http_get_browser"]},
            {"instruction": "b", "tools": []},
        ],
    )

    first, second = _agent_payloads(motet)
    assert "core.http_get_browser" in first.tool_filter_metadata["required_tools"]
    assert "core.http_get_browser" not in second.tool_filter_metadata["required_tools"]


def test_declared_tools_are_the_childs_catalog():
    """A pin without a cage still lets the child tools_search into the grant."""
    motet = _FakeMotet()
    with patch(
        "motet.core.tools.builtin.spawn_agents.resolve_child_tool_schemas",
        side_effect=_echo_schemas,
    ):
        _run(
            motet,
            [
                {"instruction": "pricing", "tools": ["core.web_search"]},
                {
                    "instruction": "docs",
                    "tools": ["core.http_get_browser", "core.tools_search"],
                },
            ],
        )

    first, second = _agent_payloads(motet)
    assert [schema["name"] for schema in first.tools] == ["core.web_search"]
    assert [schema["name"] for schema in second.tools] == ["core.http_get_browser"]
    for payload in (first, second):
        excluded = payload.tool_filter_metadata["exclude_tools"]
        assert "core.tools_search" in excluded
        assert "core.tool_call" in excluded
        assert "core.help" in excluded
        assert TOOL_NAME in excluded
    assert first.tool_filter_metadata["required_tools"] == ["core.web_search"]
    assert second.tool_filter_metadata["required_tools"] == ["core.http_get_browser"]


def test_discover_opt_in_keeps_declared_tools_off_the_cage():
    """discover=true is how we test catalog search without making it the default."""
    motet = _FakeMotet()
    with patch(
        "motet.core.tools.builtin.spawn_agents.resolve_child_tool_schemas",
        side_effect=_echo_schemas,
    ):
        _run(
            motet,
            [
                {
                    "instruction": "pricing",
                    "tools": ["core.web_search"],
                    "discover": True,
                },
                {"instruction": "docs", "tools": ["core.http_get_browser"]},
            ],
        )

    discovering, caged = _agent_payloads(motet)
    assert discovering.tools is None
    assert "core.tools_search" not in discovering.tool_filter_metadata["exclude_tools"]
    assert "core.web_search" in discovering.tool_filter_metadata["required_tools"]
    assert _system_contents(discovering) == [
        _core_subagent().metadata["discovery_system_prompt"]
    ]
    assert [schema["name"] for schema in caged.tools] == ["core.http_get_browser"]
    assert "core.tools_search" in caged.tool_filter_metadata["exclude_tools"]
    assert _system_contents(caged) == [_core_subagent().system_prompt]


def test_discover_siblings_share_the_discovery_prompt():
    """Two discovery children stay on one static prefix, like the cage cohort."""
    motet = _FakeMotet()
    _run(
        motet,
        [
            {"instruction": "a", "tools": ["core.web_search"], "discover": True},
            {"instruction": "b", "tools": ["core.http_get_browser"], "discover": True},
        ],
    )

    texts = [_system_contents(p)[0] for p in _agent_payloads(motet)]
    assert texts[0] == texts[1] == _core_subagent().metadata["discovery_system_prompt"]
    assert "core.web_search" not in texts[0]
    assert "core.http_get_browser" not in texts[0]


def test_undeclared_sibling_stays_on_discovery():
    """The cage is per task. A sibling that named no tools still has to search."""
    motet = _FakeMotet()
    with patch(
        "motet.core.tools.builtin.spawn_agents.resolve_child_tool_schemas",
        side_effect=_echo_schemas,
    ):
        _run(
            motet,
            [
                {"instruction": "a", "tools": ["core.http_get_browser"]},
                {"instruction": "b", "tools": []},
            ],
        )

    first, second = _agent_payloads(motet)
    assert first.tools is not None
    assert second.tools is None
    assert "core.tools_search" not in second.tool_filter_metadata["exclude_tools"]


def test_declared_tools_cannot_smuggle_past_the_parents_exclusions():
    """Declaring cannot widen a child past what the parent itself could call."""
    motet = _FakeMotet()
    _run(
        motet,
        [
            {"instruction": "a", "tools": ["core.host_exec", TOOL_NAME, "core.file_read"]},
            {"instruction": "b", "tools": []},
        ],
    )

    child = _agent_payloads(motet)[0].tool_filter_metadata
    assert "core.host_exec" not in child["required_tools"]
    assert TOOL_NAME not in child["required_tools"]
    assert "core.file_read" in child["required_tools"]


def test_always_sticky_meta_tools_are_not_pinned():
    """Discovery meta tools are not a catalog. Declaring only those is undeclared."""
    motet = _FakeMotet()
    _run(
        motet,
        [
            {"instruction": "a", "tools": ["core.tools_search", "core.tool_call", "core.help"]},
            {"instruction": "b", "tools": []},
        ],
    )

    assert _agent_payloads(motet)[0].tool_filter_metadata["required_tools"] == [
        "core.web_search"
    ]


def test_bare_instruction_strings_are_accepted():
    """A model that skips the object form should not fail on shape."""
    motet = _FakeMotet()
    result = _run(motet, ["do a", "do b"])

    assert result["status"] == "success"
    assert [p.input for p in _agent_payloads(motet)] == ["do a", "do b"]


def test_the_advertised_schema_asks_for_tools_per_task():
    """The schema is what prompts the model to declare tools at all."""
    from motet.core.tools.builtin.spawn_agents import SpawnAgentsParams

    schema = SpawnAgentsParams.model_json_schema()
    task_schema = schema["$defs"]["SpawnTask"]["properties"]
    assert "instruction" in task_schema
    assert "tools" in task_schema
    assert "discover" in task_schema
    assert task_schema["discover"].get("default") is False


# --- budget exhaustion is not success ---------------------------------------


def test_a_child_that_runs_out_of_budget_is_not_counted_as_success():
    """Budget stops return scaffolding text, not an answer.

    The loop's terminal contract puts "Maximum iterations reached. Please
    continue..." in final_response. Counting that as a success both inflated
    succeeded=N and handed the model boilerplate to summarize as a finding.
    """
    motet = _FakeMotet(
        results=[
            {
                "final_response": "Maximum iterations reached. Please continue to keep working on this task.",
                "stop_reason": "max_iterations",
                "tool_results": [{"status": "success", "tool_name": "core.tools_search"}],
            },
            {"final_response": "found it", "stop_reason": "stop", "tool_results": []},
        ]
    )
    result = _run(motet, ["a", "b"])

    entries = result["result"]["results"]
    assert entries[0]["status"] == "incomplete"
    assert entries[0]["stop_reason"] == "max_iterations"
    assert entries[0]["response"] == ""
    assert "Maximum iterations" not in str(entries[0])
    assert entries[1]["status"] == "success"
    assert result["meta"]["succeeded"] == 1
    assert result["meta"]["incomplete"] == 1


@pytest.mark.parametrize(
    "stop_reason",
    ["max_iterations", "max_model_calls", "max_cost", "max_prompt_tokens", "max_tool_time", "stalled", "error"],
)
def test_every_unproductive_stop_reason_is_incomplete(stop_reason: str):
    motet = _FakeMotet(
        results=[
            {"final_response": "boilerplate", "stop_reason": stop_reason, "tool_results": []},
            {"final_response": "real answer", "stop_reason": "stop", "tool_results": []},
        ]
    )
    result = _run(motet, ["a", "b"])

    assert result["result"]["results"][0]["status"] == "incomplete"


def test_a_fan_out_where_nobody_answered_is_a_failed_tool_call():
    """Otherwise the parent silently redoes the work it just paid to delegate."""
    motet = _FakeMotet(
        results=[
            {"final_response": "boilerplate", "stop_reason": "max_iterations", "tool_results": []},
            {"final_response": "boilerplate", "stop_reason": "max_iterations", "tool_results": []},
        ]
    )
    result = _run(motet, ["a", "b"])

    assert result["status"] == "error"
    assert "budget" in result["error"]


def test_a_finalized_writeup_is_counted_as_success():
    """A rail stop that produced findings is an answer, not scaffolding."""
    motet = _FakeMotet(
        results=[
            {
                "final_response": "RDS on-demand is $0.12/hour in us-east-1.",
                "stop_reason": "max_iterations",
                "finalized": True,
                "tool_results": [{"status": "success", "tool_name": "core.http_get_browser"}],
            },
            {"final_response": "found it", "stop_reason": "stop", "tool_results": []},
        ]
    )
    result = _run(motet, ["a", "b"])

    entries = result["result"]["results"]
    assert entries[0]["status"] == "success"
    assert entries[0]["response"] == "RDS on-demand is $0.12/hour in us-east-1."
    assert result["meta"]["succeeded"] == 2
    assert result["meta"]["incomplete"] == 0


def test_finalized_without_text_is_still_incomplete():
    motet = _FakeMotet(
        results=[
            {
                "final_response": "",
                "stop_reason": "max_cost",
                "finalized": True,
                "tool_results": [],
            },
            {"final_response": "found it", "stop_reason": "stop", "tool_results": []},
        ]
    )
    result = _run(motet, ["a", "b"])

    assert result["result"]["results"][0]["status"] == "incomplete"


def test_a_normal_completion_has_no_stop_reason_penalty():
    """Absent or ordinary stop reasons must stay successes."""
    motet = _FakeMotet(
        results=[
            {"final_response": "one", "tool_results": []},
            {"final_response": "two", "stop_reason": "stop", "tool_results": []},
        ]
    )
    result = _run(motet, ["a", "b"])

    assert result["meta"]["succeeded"] == 2
    assert result["meta"]["incomplete"] == 0


def test_success_cards_always_include_stop_reason():
    motet = _FakeMotet(
        results=[
            {"final_response": "one", "stop_reason": "stop", "tool_results": []},
            {"final_response": "two", "tool_results": []},
        ]
    )
    result = _run(motet, ["a", "b"])
    rows = result["result"]["results"]
    assert rows[0]["stop_reason"] == "stop"
    assert rows[1]["stop_reason"] == ""


def test_long_writeup_is_in_the_observation_and_copied_to_the_artifact():
    """The parent sees the child's full text; the artifact is a clip sidecar."""
    long_text = "RDS on-demand is $0.12/hour. " * 80

    stored: List[Dict[str, Any]] = []

    class _Store:
        def put(self, **kwargs: Any) -> str:
            stored.append(kwargs)
            return "art-fanin-1"

    motet = _FakeMotet(
        results=[
            {
                "final_response": long_text,
                "stop_reason": "max_tool_time",
                "finalized": True,
                "tool_results": [],
            },
            {"final_response": "short", "stop_reason": "stop", "tool_results": []},
        ]
    )
    motet.artifact_store = _Store()
    result = _run(motet, ["a", "b"])

    rows = result["result"]["results"]
    assert rows[0]["response"] == long_text
    assert rows[0]["stop_reason"] == "max_tool_time"
    assert rows[1]["response"] == "short"
    assert long_text.strip() in (result.get("text") or "")
    assert "short" in (result.get("text") or "")
    assert result["result"]["artifact_id"] == "art-fanin-1"
    assert stored[0]["payload"]["results"][0]["response"] == long_text


def test_child_snapshot_cache_is_returned_for_the_parent_to_inherit():
    sig = "core.http_get_browser:abc12345"
    motet = _FakeMotet(
        results=[
            {
                "final_response": "aurora",
                "stop_reason": "max_tool_time",
                "finalized": True,
                "observation_cache": {
                    sig: {
                        "tool_name": "core.http_get_browser",
                        "cache_control": {"same_turn": True, "no_store": False},
                        "stored_at": 1.0,
                        "artifact_id": "child-browser-art",
                    },
                    "core.file_read:ffff": {
                        "tool_name": "core.file_read",
                        "cache_control": {"same_turn": True, "no_store": False},
                        "stored_at": 1.0,
                    },
                },
                "executed_signatures": [sig, "core.file_read:ffff"],
            },
            {"final_response": "cloud sql", "stop_reason": "stop", "tool_results": []},
        ]
    )
    result = _run(motet, ["a", "b"])
    meta = result["meta"]
    assert sig in meta["snapshot_signatures"]
    assert "core.file_read:ffff" not in meta["snapshot_signatures"]
    assert sig in meta["snapshot_cache"]
    assert meta["snapshot_cache"][sig]["tool_name"] == "core.http_get_browser"
    assert meta["snapshot_cache"][sig]["inherited_from"] == "spawn"
    assert meta["snapshot_cache"][sig]["artifact_id"] == "child-browser-art"
