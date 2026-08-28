"""
Motet - Unit tests for the expert-panel example bundle

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-03

Description:
Unit tests for the expert-panel bundle's recall_discussion tool. The panel
itself is three core.agent_turn steps with no custom command code, so the
recall path is the bundle's only Python surface — and the place where it can
silently return nothing by querying tags no one writes. These tests pin the
recall contract to what core.finalize_turn actually stores: an
"agent:<agent_id>" tag and an agent_id in metadata.

Dependencies:
- pytest
- motet_sdk.testing.MockMotetContext: SDK test double for MotetContext
- _expert_panel_test_loader: canonical-name bundle module loading

Usage:
  pytest tests/unit/bundles/test_expert_panel_bundle.py -q

Notes:
- No memory backend or LLM is involved: memory is injected as a mock through
  MockMotetContext and get_motet_context is monkeypatched.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from motet_sdk.testing import MockMotetContext

from _expert_panel_test_loader import load_tool_module


@pytest.fixture(scope="module")
def recall_mod():
    return load_tool_module("recall_discussion")


def _turn_memory(agent: str, topic: str, *, in_metadata: bool = True) -> dict:
    """A response as core.finalize_turn stores it."""
    agent_id = f"expert-panel.{agent}"
    return {
        "content": f"Analysis of {topic} from the {agent} perspective.",
        "type": "assistant_response",
        "tags": ["conversation:conv-1", f"agent:{agent_id}", "wm", "stm"],
        "metadata": {"agent_id": agent_id, "conversation_id": "conv-1"} if in_metadata else {},
        "created_at": "2026-08-03T00:00:00Z",
    }


def _ctx(items, recall_mod, monkeypatch) -> Mock:
    memory = Mock()
    memory.recall = Mock(return_value=items)
    monkeypatch.setattr(recall_mod, "get_motet_context", lambda: MockMotetContext(memory=memory))
    return memory


def test_recall_queries_the_tags_finalize_turn_writes(recall_mod, monkeypatch):
    """The panel stores "agent:<id>" tags; querying a "panel" tag finds nothing."""
    memory = _ctx([_turn_memory("optimist", "remote work")], recall_mod, monkeypatch)

    recall_mod.recall_discussion({"topic": "remote work"})

    tags = memory.recall.call_args.kwargs["tags"]
    assert tags == [
        "agent:expert-panel.optimist",
        "agent:expert-panel.skeptic",
        "agent:expert-panel.synthesizer",
    ]


def test_recall_returns_all_three_perspectives(recall_mod, monkeypatch):
    _ctx(
        [
            _turn_memory("optimist", "remote work"),
            _turn_memory("skeptic", "remote work"),
            _turn_memory("synthesizer", "remote work"),
        ],
        recall_mod,
        monkeypatch,
    )

    result = recall_mod.recall_discussion({"topic": "remote work"})

    assert result["result_count"] == 3
    assert result["perspectives_found"] == ["optimist", "skeptic", "synthesizer"]


def test_perspective_derived_from_agent_tag_without_metadata(recall_mod, monkeypatch):
    _ctx([_turn_memory("skeptic", "remote work", in_metadata=False)], recall_mod, monkeypatch)

    result = recall_mod.recall_discussion({"topic": "remote work"})

    assert result["perspectives_found"] == ["skeptic"]


def test_perspective_filter_narrows_to_one_agent(recall_mod, monkeypatch):
    memory = _ctx(
        [_turn_memory("skeptic", "remote work"), _turn_memory("optimist", "remote work")],
        recall_mod,
        monkeypatch,
    )

    result = recall_mod.recall_discussion({"topic": "remote work", "perspective": "skeptic"})

    assert memory.recall.call_args.kwargs["tags"] == ["agent:expert-panel.skeptic"]
    assert result["perspectives_found"] == ["skeptic"]


def test_synthesis_alias_maps_to_synthesizer(recall_mod, monkeypatch):
    memory = _ctx([_turn_memory("synthesizer", "remote work")], recall_mod, monkeypatch)

    recall_mod.recall_discussion({"topic": "remote work", "perspective": "synthesis"})

    assert memory.recall.call_args.kwargs["tags"] == ["agent:expert-panel.synthesizer"]


def test_non_panel_memories_are_dropped(recall_mod, monkeypatch):
    """Tag filters are OR-matched upstream, so foreign memories can come back."""
    stray = {
        "content": "Remote work notes from some other agent.",
        "tags": ["conversation:conv-1", "agent:core.default"],
        "metadata": {"agent_id": "core.default"},
    }
    _ctx([stray, _turn_memory("optimist", "remote work")], recall_mod, monkeypatch)

    result = recall_mod.recall_discussion({"topic": "remote work"})

    assert result["result_count"] == 1
    assert result["perspectives_found"] == ["optimist"]


def test_topic_recall_asks_core_for_a_strict_relevance_floor(recall_mod, monkeypatch):
    """
    Topic filtering belongs on MemoryManager (query coverage, head-biased).
    This tool must request a strict floor so buried single-word hits stay out.
    """
    memory = _ctx([], recall_mod, monkeypatch)

    recall_mod.recall_discussion({"topic": "AI and future jobs"})

    kwargs = memory.recall.call_args.kwargs
    assert kwargs["query"] == "AI and future jobs"
    assert kwargs["min_relevance"] == 0.8


def test_limit_caps_results_after_formatting(recall_mod, monkeypatch):
    memory = _ctx(
        [_turn_memory(a, "remote work") for a in ("optimist", "skeptic", "synthesizer")],
        recall_mod,
        monkeypatch,
    )

    result = recall_mod.recall_discussion({"topic": "remote work", "limit": 2})

    # Over-fetch upstream so perspective derivation has candidates to keep.
    assert memory.recall.call_args.kwargs["limit"] > 2
    assert result["result_count"] == 2


def test_reports_missing_context(recall_mod, monkeypatch):
    monkeypatch.setattr(recall_mod, "get_motet_context", lambda: None)

    result = recall_mod.recall_discussion({"topic": "remote work"})

    assert result["result_count"] == 0
    assert result["error"]


def test_reports_memory_failure(recall_mod, monkeypatch):
    memory = Mock()
    memory.recall = Mock(side_effect=RuntimeError("vector store down"))
    monkeypatch.setattr(recall_mod, "get_motet_context", lambda: MockMotetContext(memory=memory))

    result = recall_mod.recall_discussion({"topic": "remote work"})

    assert result["result_count"] == 0
    assert "vector store down" in result["error"]
