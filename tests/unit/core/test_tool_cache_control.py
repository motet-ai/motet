"""
Motet - Tool Observation Cache-Control Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-23

Description:
    Unit tests for response-level tool freshness: parse/default directives,
    snapshot heuristics, loop replay of a fresh hit, and re-execution when
    the result is no-store, expired, or missing from history.

Usage:
    pytest tests/unit/core/test_tool_cache_control.py
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock

from motet.core.reasoning.react.agentic_loop_data import AgenticLoopData
from motet.core.reasoning.react.loop_execution import (
    _generate_tool_signature,
    execute_tools_and_append_results,
)
from motet.core.tools.cache_control import (
    CACHE_NOTICE_PREFIX,
    INHERITED_FROM_SPAWN,
    NO_STORE,
    SAME_TURN,
    attach_snapshot_cache_control,
    inherit_snapshot_cache,
    parse_cache_control,
    remember_observation,
    resolve_cache_control,
    snapshot_default,
    take_fresh_cache_hit,
)
from motet.core.tools.protocol import err, ok
from motet.core.types import Message


def _data(**overrides: Any) -> AgenticLoopData:
    defaults: Dict[str, Any] = dict(
        input="price rds",
        conversation_history=[Message(role="user", content="price rds")],
        stream_key="task:t1:response",
        max_tools=10,
    )
    defaults.update(overrides)
    return AgenticLoopData(**defaults)


def _motet() -> MagicMock:
    motet = MagicMock()
    motet.stream_key = "task:t1:response"
    motet.principal_id = "p"
    motet.tenant_id = "t"
    motet.task_id = "task"
    motet.conversation_id = "c"
    motet.metadata = {}
    motet.tools = MagicMock()
    motet.join = MagicMock(return_value=[])
    return motet


def _call(name: str, params: Dict[str, Any], call_id: str = "c1") -> Dict[str, Any]:
    return {
        "tool_call_id": call_id,
        "tool_name": name,
        "parameters": params,
        "signature": _generate_tool_signature(name, params),
        "is_repeat": False,
    }


# --- parse / protocol --------------------------------------------------------


def test_parse_http_style_directives() -> None:
    assert parse_cache_control("no-store").no_store is True
    assert parse_cache_control("same-turn").same_turn is True
    assert parse_cache_control("max-age=600").max_age_seconds == 600
    assert parse_cache_control("max-age=0").no_store is True
    assert parse_cache_control("max-age=120, same-turn").same_turn is True


def test_ok_accepts_cache_control_string() -> None:
    payload = ok({"n": 1}, cache_control="same-turn")
    assert payload["cache_control"]["same_turn"] is True
    assert payload["status"] == "success"


def test_err_is_never_cacheable() -> None:
    payload = err("failed")
    assert resolve_cache_control("core.http_get", payload).is_cacheable() is False


# --- snapshot defaults -------------------------------------------------------


def test_http_get_usable_page_is_same_turn() -> None:
    text = "Amazon RDS for PostgreSQL pricing " * 4
    cc = snapshot_default("core.http_get", {"status": 200, "text": text})
    assert cc == SAME_TURN


def test_http_get_short_or_error_is_no_store() -> None:
    assert snapshot_default("core.http_get", {"status": 200, "text": "ok"}) == NO_STORE
    assert snapshot_default("core.http_get", {"status": 404, "text": "x" * 200}) == NO_STORE
    assert snapshot_default("core.http_get", {"status": "error", "error": "timeout"}) == NO_STORE


def test_web_search_empty_results_are_no_store() -> None:
    assert snapshot_default(
        "core.web_search",
        {"status": "success", "results": [], "main_content": "No specific results found for q."},
    ) == NO_STORE
    assert snapshot_default(
        "core.web_search",
        {"status": "success", "results": [{"title": "RDS", "content": "price"}]},
    ) == SAME_TURN


def test_file_read_defaults_to_no_store() -> None:
    assert resolve_cache_control(
        "core.file_read",
        {"status": "success", "text": "class Foo:\n    pass\n" * 20},
    ) == NO_STORE


def test_explicit_directive_wins_over_snapshot_default() -> None:
    cc = resolve_cache_control(
        "core.http_get",
        {"status": 200, "text": "x" * 200, "cache_control": "no-store"},
    )
    assert cc.no_store is True


def test_attach_does_not_overwrite_explicit() -> None:
    payload = attach_snapshot_cache_control(
        "core.http_get",
        {"status": 200, "text": "x" * 200, "cache_control": {"no_store": True}},
    )
    assert payload["cache_control"]["no_store"] is True


# --- cache table -------------------------------------------------------------


def test_fresh_same_turn_hit_and_expired_max_age() -> None:
    cache: Dict[str, Any] = {}
    remember_observation(
        cache,
        signature="http:aaaa",
        tool_name="core.http_get",
        payload={"status": 200, "text": "x" * 200, "cache_control": "same-turn"},
        now=100.0,
    )
    assert take_fresh_cache_hit(cache, "http:aaaa", now=999.0) is not None

    cache2: Dict[str, Any] = {}
    remember_observation(
        cache2,
        signature="http:bbbb",
        tool_name="core.http_get",
        payload={"status": 200, "text": "x" * 200, "cache_control": "max-age=30"},
        now=100.0,
    )
    assert take_fresh_cache_hit(cache2, "http:bbbb", now=120.0) is not None
    assert take_fresh_cache_hit(cache2, "http:bbbb", now=140.0) is None


def test_hit_requires_signature_still_in_history() -> None:
    cache: Dict[str, Any] = {}
    remember_observation(
        cache,
        signature="http:cccc",
        tool_name="core.http_get",
        payload={"status": 200, "text": "x" * 200, "cache_control": "same-turn"},
        now=1.0,
    )
    assert take_fresh_cache_hit(
        cache, "http:cccc", now=2.0, executed_signatures=["other"],
    ) is None
    assert take_fresh_cache_hit(
        cache, "http:cccc", now=2.0, executed_signatures=["http:cccc"],
    ) is not None


# --- loop execute ------------------------------------------------------------


def _run_execute(
    data: AgenticLoopData,
    calls: List[Dict[str, Any]],
    motet: MagicMock,
) -> Any:
    return execute_tools_and_append_results(
        calls,
        [],
        data,
        motet,
        current_iteration=1,
        iterations_used=1,
        accumulated_usage={
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "reasoning_tokens": 0,
            "tool_time_ms": 0,
        },
        timings={"embedding_ms": 0.0, "llm_ms": 0.0, "tool_execution_ms": 0.0},
    )


def test_fresh_hit_skips_join_and_appends_notice() -> None:
    call = _call("core.http_get_browser", {"url": "https://aws.amazon.com/rds/aurora/pricing/"})
    data = _data(executed_signatures=[call["signature"]])
    remember_observation(
        data.observation_cache,
        signature=call["signature"],
        tool_name="core.http_get_browser",
        payload={"status": 200, "main_content": "Aurora pricing " * 20, "cache_control": "same-turn"},
        now=1.0,
    )
    motet = _motet()
    result = _run_execute(data, [call], motet)

    motet.join.assert_not_called()
    assert result.tool_results[0]["cached"] is True
    assert result.tool_results[0]["result"].startswith(CACHE_NOTICE_PREFIX)
    assert "Use that observation" in result.tool_results[0]["result"]
    assert "core.spawn_agents cards" not in result.tool_results[0]["result"]
    assert data.conversation_history[-1].content.startswith(CACHE_NOTICE_PREFIX)


def test_file_read_repeat_still_executes() -> None:
    call = _call("core.file_read", {"path": "a.py"})
    data = _data(executed_signatures=[call["signature"]])
    motet = _motet()
    motet.join.return_value = [{"result": "class Foo:\n    pass\n"}]
    _run_execute(data, [call], motet)
    motet.join.assert_called_once()


def test_successful_snapshot_is_remembered_for_next_round() -> None:
    call = _call("core.web_search", {"query": "rds pricing"})
    data = _data()
    motet = _motet()
    motet.join.return_value = [{
        "status": "success",
        "results": [{"title": "RDS", "content": "On-demand"}],
        "main_content": "On-demand RDS pricing",
        "cache_control": {"same_turn": True},
    }]
    _run_execute(data, [call], motet)
    assert take_fresh_cache_hit(
        data.observation_cache,
        call["signature"],
        now=10.0,
        executed_signatures=[call["signature"]],
    ) is not None


def test_inherit_snapshot_cache_skips_non_snapshot_tools() -> None:
    dest_cache: Dict[str, Any] = {}
    dest_sigs: List[str] = []
    http_sig = "core.http_get:aaaa"
    file_sig = "core.file_read:bbbb"
    count = inherit_snapshot_cache(
        dest_cache,
        dest_sigs,
        {
            http_sig: {
                "tool_name": "core.http_get",
                "cache_control": {"same_turn": True, "no_store": False},
                "stored_at": 1.0,
                "artifact_id": "child-http-1",
            },
            file_sig: {
                "tool_name": "core.file_read",
                "cache_control": {"same_turn": True, "no_store": False},
                "stored_at": 1.0,
            },
        },
        [http_sig, file_sig],
    )
    assert count == 1
    assert dest_sigs == [http_sig]
    assert http_sig in dest_cache
    assert dest_cache[http_sig]["inherited_from"] == INHERITED_FROM_SPAWN
    assert dest_cache[http_sig]["artifact_id"] == "child-http-1"
    assert file_sig not in dest_cache


def test_spawn_meta_is_inherited_so_the_parent_304s_the_same_call() -> None:
    call = _call("core.http_get_browser", {"url": "https://aws.amazon.com/rds/postgresql/pricing/"})
    spawn_call = _call("core.spawn_agents", {"tasks": ["a", "b"]}, call_id="spawn")
    data = _data()
    motet = _motet()
    motet.join.return_value = [{
        "status": "success",
        "result": {"results": [{"task": "a", "status": "success", "response": "ok"}]},
        "meta": {
            "snapshot_cache": {
                call["signature"]: {
                    "tool_name": "core.http_get_browser",
                    "cache_control": {"same_turn": True, "no_store": False},
                    "stored_at": 1.0,
                    "artifact_id": "child-browser-1",
                },
            },
            "snapshot_signatures": [call["signature"]],
        },
    }]
    _run_execute(data, [spawn_call], motet)
    assert call["signature"] in data.executed_signatures
    assert data.observation_cache[call["signature"]]["inherited_from"] == INHERITED_FROM_SPAWN
    assert data.observation_cache[call["signature"]]["artifact_id"] == "child-browser-1"

    motet.join.reset_mock()
    result = _run_execute(data, [call], motet)
    motet.join.assert_not_called()
    notice = result.tool_results[0]["result"]
    assert result.tool_results[0]["cached"] is True
    assert notice.startswith(CACHE_NOTICE_PREFIX)
    assert "core.spawn_agents observation" in notice
    assert "artifact_id=child-browser-1" in notice
    assert "Use that observation" not in notice
    assert data.conversation_history[-1].content == notice


def test_remember_unwraps_tool_execution_wrapper_and_contextualized_body() -> None:
    cache: Dict[str, Any] = {}
    remember_observation(
        cache,
        signature="core.http_get_browser:deadbeef",
        tool_name="core.http_get_browser",
        payload={
            "tool_name": "core.http_get_browser",
            "executed": True,
            "artifact_id": "offloaded-browser-1",
            "result": {
                "status": "success",
                "_context_processed": True,
                "_context_items": 4,
            },
        },
        now=1.0,
    )
    hit = take_fresh_cache_hit(
        cache,
        "core.http_get_browser:deadbeef",
        now=2.0,
        executed_signatures=["core.http_get_browser:deadbeef"],
    )
    assert hit is not None
    assert hit.cache_control.same_turn is True
    assert hit.artifact_id == "offloaded-browser-1"


def test_inherit_reads_contextualized_spawn_meta_alias() -> None:
    from motet.core.reasoning.react.loop_execution import _spawn_meta_from_payload

    meta = _spawn_meta_from_payload({
        "tool_name": "core.spawn_agents",
        "executed": True,
        "result": {
            "status": "success",
            "spawn_agents.meta": {
                "snapshot_cache": {"core.http_get:aa": {}},
                "snapshot_signatures": ["core.http_get:aa"],
            },
        },
    })
    assert meta is not None
    assert meta["snapshot_signatures"] == ["core.http_get:aa"]
