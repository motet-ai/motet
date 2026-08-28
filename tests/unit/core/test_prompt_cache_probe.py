"""
Motet - Prompt Cache Prefix Probe Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-29

Description:
    Unit tests for the prompt-cache prefix probe (ADR-0124 diagnostics): segment
    fingerprinting in provider prefix order, rolling prefix-hash chaining, and
    the verdict each prompt mutation produces. The verdicts are the whole point
    of the probe — an append must report ``append_only`` (cache preserved) while
    a rewrite upstream of the tail must report ``prefix_rewritten`` and name the
    diverging segment — so each mutation shape is asserted explicitly.

Usage:
    pytest tests/unit/core/test_prompt_cache_probe.py
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from motet.core.reasoning.react.prompt_cache_probe import (
    PROBE_TTL_SECONDS,
    _chain,
    _first_divergence,
    _segments,
    probe_enabled,
    record_prompt_fingerprint,
)


class _Msg:
    """Minimal stand-in for motet.core.types.Message."""

    def __init__(self, role: str, content: str, tool_call_id: Optional[str] = None) -> None:
        self.role = role
        self.content = content
        self.name = None
        self.tool_call_id = tool_call_id


def _tool(name: str, description: str = "d") -> Dict[str, Any]:
    return {"name": name, "description": description, "parameters": {"type": "object"}}


# --- segment fingerprinting --------------------------------------------------


def test_segments_follow_provider_prefix_order() -> None:
    """Tools precede messages: the provider serializes the tools block first."""
    segments = _segments([_tool("ReadFile"), _tool("core.help")], [_Msg("system", "s")])
    assert [label for label, _, _ in segments] == [
        "tool:ReadFile",
        "tool:core.help",
        "msg0:system",
    ]


def test_identical_prompts_produce_identical_chains() -> None:
    tools = [_tool("ReadFile")]
    messages = [_Msg("system", "s"), _Msg("user", "u")]
    assert _chain(_segments(tools, messages)) == _chain(_segments(tools, messages))


def test_chain_is_prefix_stable_under_append() -> None:
    """Appending must not disturb earlier rolling hashes, or every append would miss."""
    tools = [_tool("ReadFile")]
    before = _chain(_segments(tools, [_Msg("system", "s")]))
    after = _chain(_segments(tools, [_Msg("system", "s"), _Msg("user", "u")]))
    assert after[: len(before)] == before
    assert _first_divergence(before, after) is None


def test_chain_diverges_at_edited_message_index() -> None:
    tools = [_tool("ReadFile")]
    before = _chain(_segments(tools, [_Msg("system", "s"), _Msg("user", "original")]))
    after = _chain(_segments(tools, [_Msg("system", "s"), _Msg("user", "edited")]))
    # index 0 is the tool, 1 the system message, 2 the edited user message.
    assert _first_divergence(before, after) == 2


def test_chain_diverges_at_tool_change_before_any_message() -> None:
    """A tools-block change invalidates the system prompt and entire history."""
    messages = [_Msg("system", "s"), _Msg("user", "u")]
    before = _chain(_segments([_tool("ReadFile", "old")], messages))
    after = _chain(_segments([_tool("ReadFile", "new")], messages))
    assert _first_divergence(before, after) == 0


# --- probe gating ------------------------------------------------------------


def test_probe_disabled_by_default() -> None:
    with patch.dict("os.environ", {}, clear=True):
        assert probe_enabled() is False


def test_probe_enabled_accepts_truthy_spellings() -> None:
    for value in ("true", "TRUE", "1", "yes", "on"):
        with patch.dict("os.environ", {"MOTET_PROMPT_CACHE_PROBE": value}, clear=True):
            assert probe_enabled() is True


def test_disabled_probe_touches_no_redis() -> None:
    with patch.dict("os.environ", {"MOTET_PROMPT_CACHE_PROBE": "false"}, clear=True):
        with patch(
            "motet.core.distributed.redis_manager.retrieve_structured_data_sync"
        ) as retrieve:
            record_prompt_fingerprint(
                tenant_id="t",
                motet_id="m",
                conversation_id="c",
                tools=[_tool("ReadFile")],
                messages=[_Msg("user", "u")],
            )
    retrieve.assert_not_called()


def test_missing_conversation_id_is_a_noop() -> None:
    """Cross-call comparison needs stable identity; without it the probe stays silent."""
    with patch.dict("os.environ", {"MOTET_PROMPT_CACHE_PROBE": "true"}, clear=True):
        with patch(
            "motet.core.distributed.redis_manager.retrieve_structured_data_sync"
        ) as retrieve:
            record_prompt_fingerprint(
                tenant_id="t",
                motet_id="m",
                conversation_id=None,
                tools=[_tool("ReadFile")],
                messages=[_Msg("user", "u")],
            )
    retrieve.assert_not_called()


# --- verdicts ----------------------------------------------------------------


class _FakeStore:
    """In-memory stand-in for the Redis round-trip, capturing logged verdicts."""

    def __init__(self) -> None:
        self.data: Dict[str, Dict[str, Any]] = {}
        self.expires: List[int] = []

    def retrieve(self, _service: str, key: str, format_type: str = "hash") -> Any:
        return self.data.get(key)

    def store(
        self, _service: str, key: str, data: Dict[str, Any], format_type: str = "hash"
    ) -> None:
        self.data[key] = data

    def client(self, _service: str) -> MagicMock:
        client = MagicMock()
        client.expire = lambda _key, ttl: self.expires.append(ttl)
        return client


def _run_probe(
    store: _FakeStore,
    tools: List[Dict[str, Any]],
    messages: List[_Msg],
) -> Dict[str, Any]:
    """Record one call and return the kwargs of the emitted probe log line."""
    module = "motet.core.reasoning.react.prompt_cache_probe"
    with patch.dict("os.environ", {"MOTET_PROMPT_CACHE_PROBE": "true"}, clear=True):
        with patch(
            "motet.core.distributed.redis_manager.retrieve_structured_data_sync",
            side_effect=store.retrieve,
        ), patch(
            "motet.core.distributed.redis_manager.store_structured_data_sync",
            side_effect=store.store,
        ), patch(
            "motet.core.distributed.redis_manager.get_sync_redis_client",
            side_effect=store.client,
        ), patch(
            f"{module}.logger"
        ) as logger:
            record_prompt_fingerprint(
                tenant_id="t",
                motet_id="m",
                conversation_id="conv-1",
                tools=tools,
                messages=messages,
            )
    assert logger.info.called, "probe emitted no diagnostic line"
    event, kwargs = logger.info.call_args[0][0], logger.info.call_args[1]
    assert event == "prompt_cache_probe"
    return kwargs


def test_first_call_reports_first_call_and_sets_ttl() -> None:
    store = _FakeStore()
    result = _run_probe(store, [_tool("ReadFile")], [_Msg("user", "u")])
    assert result["verdict"] == "first_call"
    assert store.expires == [PROBE_TTL_SECONDS]


def test_appending_a_turn_reports_append_only_with_no_loss() -> None:
    """The healthy agentic path: observations append, prefix survives."""
    store = _FakeStore()
    tools = [_tool("ReadFile")]
    _run_probe(store, tools, [_Msg("system", "s"), _Msg("user", "u")])
    result = _run_probe(
        store,
        tools,
        [_Msg("system", "s"), _Msg("user", "u"), _Msg("tool", "observation")],
    )
    assert result["verdict"] == "append_only"
    assert result["lost_chars"] == 0
    assert result["divergence_index"] is None


def test_rewriting_history_reports_prefix_rewritten_and_names_the_segment() -> None:
    """Context trimming/compaction signature: an upstream edit invalidates the suffix."""
    store = _FakeStore()
    tools = [_tool("ReadFile")]
    _run_probe(store, tools, [_Msg("system", "s"), _Msg("user", "original"), _Msg("tool", "obs")])
    result = _run_probe(
        store,
        tools,
        [_Msg("system", "s"), _Msg("user", "trimmed"), _Msg("tool", "obs")],
    )
    assert result["verdict"] == "prefix_rewritten"
    assert result["divergence_index"] == 2
    assert result["divergence_segment"].startswith("msg1:user")
    assert result["previous_segment"].startswith("msg1:user")
    # Everything from the edit onward is re-ingested; the head stays cacheable.
    assert result["lost_chars"] > 0
    assert result["cacheable_chars"] > 0
    assert result["cacheable_chars"] + result["lost_chars"] == result["total_chars"]


def test_tool_membership_change_invalidates_the_whole_prompt() -> None:
    """Why the sticky shortlist matters: tools sit ahead of every message."""
    store = _FakeStore()
    messages = [_Msg("system", "s"), _Msg("user", "u")]
    _run_probe(store, [_tool("core.help")], messages)
    result = _run_probe(store, [_tool("core.tools_search")], messages)
    assert result["verdict"] == "prefix_rewritten"
    assert result["divergence_index"] == 0
    assert result["cacheable_chars"] == 0
    assert result["lost_chars"] == result["total_chars"]


def test_truncated_history_is_distinguished_from_append() -> None:
    store = _FakeStore()
    tools = [_tool("ReadFile")]
    _run_probe(store, tools, [_Msg("system", "s"), _Msg("user", "u"), _Msg("tool", "obs")])
    result = _run_probe(store, tools, [_Msg("system", "s"), _Msg("user", "u")])
    assert result["verdict"] == "prefix_truncated"


def test_redis_failure_degrades_to_warning() -> None:
    """Diagnostics must never fail a turn."""
    module = "motet.core.reasoning.react.prompt_cache_probe"
    with patch.dict("os.environ", {"MOTET_PROMPT_CACHE_PROBE": "true"}, clear=True):
        with patch(
            "motet.core.distributed.redis_manager.retrieve_structured_data_sync",
            side_effect=RuntimeError("redis down"),
        ), patch(f"{module}.logger") as logger:
            record_prompt_fingerprint(
                tenant_id="t",
                motet_id="m",
                conversation_id="conv-1",
                tools=[_tool("ReadFile")],
                messages=[_Msg("user", "u")],
            )
    assert logger.warning.called
    assert logger.warning.call_args[0][0] == "prompt_cache_probe_failed"
