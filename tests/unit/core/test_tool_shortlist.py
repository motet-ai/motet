"""
Motet - Conversation Tool Shortlist Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-22

Description:
    Unit tests for the sticky per-conversation tool shortlist (ADR-0124 prompt
    caching + meta-tool progressive disclosure): frozen membership merge, Redis
    persistence round-trip (mocked), meta-tool filtering, and the agentic loop's
    schema-level merge. Discovery never admits catalog tools into the tools
    prefix; reachability is core.tools_search → core.tool_call. Permanent
    members are core.help, core.tools_search, core.tool_call, and
    core.spawn_agents.

Usage:
    pytest tests/unit/core/test_tool_shortlist.py
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from motet.core.reasoning.react.tool_shortlist import (
    ALWAYS_STICKY_TOOL_NAMES,
    SHORTLIST_TTL_SECONDS,
    load_tool_shortlist,
    merge_sticky_tool_names,
    store_tool_shortlist,
)

# Permanent shortlist members (help + progressive-disclosure meta tools).
_ALWAYS = list(ALWAYS_STICKY_TOOL_NAMES)


# --- merge_sticky_tool_names -------------------------------------------------


def test_merge_seeds_always_sticky_on_empty() -> None:
    assert merge_sticky_tool_names([], max_tools=10) == _ALWAYS


def test_merge_preserves_sticky_order_and_admits_missing_always() -> None:
    result = merge_sticky_tool_names(
        sticky_names=["core.help", "a", "b", "c"],
        max_tools=10,
    )
    assert result == [
        "core.help",
        "a",
        "b",
        "c",
        "core.tools_search",
        "core.tool_call",
        "core.spawn_agents",
    ]


def test_merge_never_admits_discovery_drift() -> None:
    """Catalog tools are not shortlist members; tools_search → tool_call reaches them."""
    sticky = list(_ALWAYS)
    # Pins are the only deliberate admissions besides always-sticky.
    assert merge_sticky_tool_names(sticky, max_tools=10) == sticky
    assert merge_sticky_tool_names(sticky, max_tools=10, pinned_names=[]) == sticky


def test_merge_identical_across_turns_is_stable() -> None:
    sticky = [*_ALWAYS, "a", "b", "c"]
    assert merge_sticky_tool_names(sticky, max_tools=10) == sticky
    assert merge_sticky_tool_names(sticky, max_tools=7) == sticky


def test_merge_always_sticky_admitted_once_then_stable() -> None:
    sticky = [f"s{i}" for i in range(10)]
    result = merge_sticky_tool_names(sticky, max_tools=10)
    assert result == [*[f"s{i}" for i in range(10 - len(_ALWAYS))], *_ALWAYS]
    assert merge_sticky_tool_names(result, max_tools=10) == result


def test_merge_pinned_tools_enter_by_evicting_stale_tail() -> None:
    sticky = [*_ALWAYS, *[f"s{i}" for i in range(7)]]
    result = merge_sticky_tool_names(
        sticky,
        max_tools=10,
        pinned_names=["core.current_time", "core.schedule_command"],
    )
    assert "core.current_time" in result
    assert "core.schedule_command" in result
    assert result[: len(_ALWAYS) + 2] == [*_ALWAYS, "s0", "s1"]
    assert merge_sticky_tool_names(
        result,
        max_tools=10,
        pinned_names=["core.current_time", "core.schedule_command"],
    ) == result


def test_merge_never_evicts_permanent_or_pinned_members() -> None:
    sticky = [*_ALWAYS, "core.current_time", *[f"s{i}" for i in range(5)]]
    result = merge_sticky_tool_names(
        sticky,
        max_tools=10,
        pinned_names=["core.current_time"],
    )
    assert result == sticky


def test_merge_deduplicates_and_drops_empty_names() -> None:
    result = merge_sticky_tool_names(
        sticky_names=["a", "", "a", "b"],
        max_tools=10,
    )
    assert result == ["a", "b", *_ALWAYS]


def test_merge_frozen_shortlist_is_minimal_and_stable_across_turns() -> None:
    turn1 = merge_sticky_tool_names([], max_tools=8)
    assert turn1 == _ALWAYS
    turn2 = merge_sticky_tool_names(turn1, max_tools=8)
    assert turn2 == turn1


def test_merge_admits_keyword_pins() -> None:
    result = merge_sticky_tool_names(
        list(_ALWAYS),
        max_tools=8,
        pinned_names=["core.memory_store", "core.memory_recall"],
    )
    assert result == [*_ALWAYS, "core.memory_store", "core.memory_recall"]
    assert merge_sticky_tool_names(
        result,
        max_tools=8,
        pinned_names=["core.memory_store", "core.memory_recall"],
    ) == result


def test_max_tools_must_leave_headroom_for_the_largest_pin_group() -> None:
    """Truncation to max_tools happens *after* pins are admitted."""
    temporal = [
        "core.current_time",
        "core.schedule_command",
        "core.scheduled_commands_list",
        "core.manage_schedule",
    ]
    assert merge_sticky_tool_names(
        list(_ALWAYS), max_tools=8, pinned_names=temporal
    ) == [*_ALWAYS, *temporal]
    assert merge_sticky_tool_names(
        list(_ALWAYS), max_tools=1, pinned_names=temporal
    ) == ["core.help"]


# --- load/store persistence --------------------------------------------------


def test_load_returns_empty_without_conversation_id() -> None:
    assert load_tool_shortlist(tenant_id="t", motet_id="m", conversation_id=None) == []
    assert load_tool_shortlist(tenant_id="t", motet_id="m", conversation_id="") == []


def test_load_round_trip_via_redis_manager() -> None:
    with patch(
        "motet.core.distributed.redis_manager.retrieve_structured_data_sync",
        return_value={"tools": ["core.web_search", "workflow_web_research"]},
    ) as retrieve:
        names = load_tool_shortlist(tenant_id="t1", motet_id="m1", conversation_id="c1")
    assert names == ["core.web_search", "workflow_web_research"]
    args = retrieve.call_args
    assert args.args[0] == "tool_shortlist"
    assert args.args[1] == "tool_shortlist:t1:m1:c1"
    assert args.kwargs["format_type"] == "json_string"


def test_load_swallows_redis_errors() -> None:
    with patch(
        "motet.core.distributed.redis_manager.retrieve_structured_data_sync",
        side_effect=ConnectionError("redis down"),
    ):
        assert load_tool_shortlist(tenant_id="t", motet_id="m", conversation_id="c") == []


def test_store_filters_meta_tools_and_sets_ttl() -> None:
    # autospec: a bare MagicMock accepts any kwargs, which hides signature drift
    # between this call and redis_manager.store_structured_data_sync.
    with patch(
        "motet.core.distributed.redis_manager.store_structured_data_sync",
        autospec=True,
    ) as store, patch(
        "motet.core.distributed.redis_manager.get_sync_redis_client",
        autospec=True,
    ) as get_client:
        store_tool_shortlist(
            tenant_id="t1",
            motet_id="m1",
            conversation_id="c1",
            tool_names=["core.web_search", "", "core.help"],
        )
    key = "tool_shortlist:t1:m1:c1"
    store.assert_called_once_with(
        "tool_shortlist",
        key,
        {"tools": ["core.web_search", "core.help"]},
        format_type="json_string",
    )
    get_client.return_value.expire.assert_called_once_with(key, SHORTLIST_TTL_SECONDS)


def test_store_skips_without_conversation_id() -> None:
    with patch(
        "motet.core.distributed.redis_manager.store_structured_data_sync",
        autospec=True,
    ) as store:
        store_tool_shortlist(tenant_id="t", motet_id="m", conversation_id=None, tool_names=["a"])
    store.assert_not_called()


def test_store_skips_when_filtering_leaves_nothing() -> None:
    """A list of only empty names cleans to []; writing it would wipe the working set."""
    with patch(
        "motet.core.distributed.redis_manager.store_structured_data_sync",
        autospec=True,
    ) as store:
        store_tool_shortlist(
            tenant_id="t",
            motet_id="m",
            conversation_id="c",
            tool_names=["", ""],
        )
    store.assert_not_called()


def test_store_swallows_redis_errors() -> None:
    with patch(
        "motet.core.distributed.redis_manager.store_structured_data_sync",
        autospec=True,
        side_effect=ConnectionError("redis down"),
    ):
        store_tool_shortlist(tenant_id="t", motet_id="m", conversation_id="c", tool_names=["a"])


# --- agentic loop schema-level merge ------------------------------------------


def _schema(name: str):
    from motet.core.types import CanonicalToolSchema

    return CanonicalToolSchema(
        name=name, description=name, json_schema={"type": "object", "properties": {}}
    )


def test_merge_sticky_tool_schemas_carries_over_and_admits_always() -> None:
    from motet.core.reasoning.react.loop_discovery import (
        merge_sticky_tool_schemas,
    )

    carried = {
        "core.current_time": _schema("core.current_time"),
        "core.help": _schema("core.help"),
        "core.tools_search": _schema("core.tools_search"),
        "core.tool_call": _schema("core.tool_call"),
        "core.spawn_agents": _schema("core.spawn_agents"),
    }
    with patch(
        "motet.core.reasoning.react.loop_discovery._resolve_tool_schemas_by_name",
        return_value=carried,
    ):
        merged = merge_sticky_tool_schemas(
            sticky_names=["core.current_time"],
            motet=MagicMock(),
            max_tools=10,
        )
    assert [s.name for s in merged] == [
        "core.current_time",
        "core.help",
        "core.tools_search",
        "core.tool_call",
        "core.spawn_agents",
    ]


def test_merge_sticky_tool_schemas_passes_query_pins() -> None:
    """Temporal query intent admits pinned tools even without residency."""
    from motet.core.reasoning.react.loop_discovery import (
        merge_sticky_tool_schemas,
    )

    pinned = {
        "core.help": _schema("core.help"),
        "core.tools_search": _schema("core.tools_search"),
        "core.tool_call": _schema("core.tool_call"),
        "core.spawn_agents": _schema("core.spawn_agents"),
        "core.current_time": _schema("core.current_time"),
        "core.schedule_command": _schema("core.schedule_command"),
        "core.scheduled_commands_list": _schema("core.scheduled_commands_list"),
        "core.manage_schedule": _schema("core.manage_schedule"),
    }
    with patch(
        "motet.core.reasoning.react.loop_discovery._resolve_tool_schemas_by_name",
        return_value=pinned,
    ):
        merged = merge_sticky_tool_schemas(
            sticky_names=["core.help"],
            motet=MagicMock(),
            max_tools=10,
            query="remind me tomorrow to check the build",
        )
    names = [s.name for s in merged]
    assert "core.current_time" in names
    assert "core.schedule_command" in names


def test_merge_sticky_tool_schemas_drops_unresolvable_names() -> None:
    from motet.core.reasoning.react.loop_discovery import (
        merge_sticky_tool_schemas,
    )

    with patch(
        "motet.core.reasoning.react.loop_discovery._resolve_tool_schemas_by_name",
        return_value={},  # deregistered tool no longer resolves
    ):
        merged = merge_sticky_tool_schemas(
            sticky_names=["old.gone_tool"],
            motet=MagicMock(),
            max_tools=10,
        )
    assert merged == []


def test_merge_sticky_tool_schemas_empty_sticky_seeds_always_sticky() -> None:
    from motet.core.reasoning.react.loop_discovery import (
        merge_sticky_tool_schemas,
    )

    always = {n: _schema(n) for n in _ALWAYS}
    with patch(
        "motet.core.reasoning.react.loop_discovery._resolve_tool_schemas_by_name",
        return_value=always,
    ):
        merged = merge_sticky_tool_schemas([], MagicMock(), 10)
    assert [s.name for s in merged] == list(_ALWAYS)


def test_merge_sticky_tool_schemas_never_admits_catalog_by_name() -> None:
    """Catalog tools stay out of the tools prefix; search → tool_call reaches them."""
    from motet.core.reasoning.react.loop_discovery import (
        merge_sticky_tool_schemas,
    )

    always = {n: _schema(n) for n in _ALWAYS}
    with patch(
        "motet.core.reasoning.react.loop_discovery._resolve_tool_schemas_by_name",
        return_value=always,
    ), patch(
        "motet.core.reasoning.react.loop_discovery._apply_discovery_filters",
        side_effect=lambda schemas, *a, **kw: schemas,
    ):
        merged = merge_sticky_tool_schemas([], MagicMock(), 8)
    assert [s.name for s in merged] == _ALWAYS
    assert "core.web_search" not in [s.name for s in merged]


# --- keyword pin groups -------------------------------------------------------


def test_memory_keywords_pin_memory_tools() -> None:
    """Memory tools rely on keyword pins, not residency in the frozen bag."""
    from motet.core.reasoning.react.loop_discovery import (
        _MEMORY_PIN_TOOLS,
        _keyword_pinned_tool_names,
    )

    for query in ("remember that I prefer tabs", "recall what we decided", "keep track of this"):
        assert _keyword_pinned_tool_names(query) == list(_MEMORY_PIN_TOOLS)


def test_forget_phrases_pin_forget_not_store() -> None:
    """Forget is a separate pin, not part of the default store/recall list."""
    from motet.core.reasoning.react.loop_discovery import (
        _MEMORY_FORGET_PIN_TOOLS,
        _MEMORY_PIN_TOOLS,
        _keyword_pinned_tool_names,
    )

    assert _keyword_pinned_tool_names("forget that I like soccer") == list(
        _MEMORY_FORGET_PIN_TOOLS
    )
    assert _keyword_pinned_tool_names("please forget this") == list(_MEMORY_FORGET_PIN_TOOLS)
    assert _keyword_pinned_tool_names("don't forget that I prefer tabs") == list(
        _MEMORY_PIN_TOOLS
    )


def test_ordinary_coding_turns_pin_nothing() -> None:
    """Pins must stay quiet on the turns that dominate a coding session."""
    from motet.core.reasoning.react.loop_discovery import (
        _keyword_pinned_tool_names,
    )

    assert _keyword_pinned_tool_names("fix the type error in registry.py") == []
    assert _keyword_pinned_tool_names("why is this test failing") == []
