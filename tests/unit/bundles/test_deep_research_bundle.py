"""
Motet - Unit tests for the deep-research example bundle

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-03

Description:
Unit tests for the deep-research example bundle, doubling as the worked
example of testing bundle code with the SDK's MockMotetContext (ADR-0080).
Covers the tool-output normalization every bundle needs (context-processed
keys are namespaced and scalars are label-prefixed), the LLM planning /
extraction / synthesis commands with mocked model inference, principal-scoped
memory persistence, and topic-scoped recall.

Dependencies:
- pytest
- motet_sdk.testing.MockMotetContext: SDK test double for MotetContext
- _deep_research_test_loader: canonical-name bundle module loading

Usage:
  pytest tests/unit/bundles/test_deep_research_bundle.py -q

Notes:
- Bundle modules are not on the default PYTHONPATH; tests load them by file
  path under their canonical package names (bundle.deep-research.commands.*)
  so relative imports resolve the same way they do in a worker.
- No network or LLM calls: model inference, tool execution, and memory are
  injected as mocks through MockMotetContext.
"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from motet_sdk.testing import MockMotetContext

from _deep_research_test_loader import load_command_module, load_tool_module


@pytest.fixture(scope="module")
def search_mod():
    return load_command_module("search_source")


@pytest.fixture(scope="module")
def plan_mod():
    return load_command_module("plan_queries")


@pytest.fixture(scope="module")
def extract_mod():
    return load_command_module("extract_findings")


@pytest.fixture(scope="module")
def gather_mod():
    return load_command_module("gather_sources")


@pytest.fixture(scope="module")
def analyze_mod():
    return load_command_module("analyze_sources")


@pytest.fixture(scope="module")
def synth_mod():
    return load_command_module("synthesize")


@pytest.fixture(scope="module")
def recall_mod():
    return load_tool_module("recall_research")


# --- tool output normalization ------------------------------------------------


def test_search_items_reads_context_namespaced_key(search_mod):
    """Live core.web_search output namespaces the item list under web_search.*."""
    payload = {
        "status": "success",
        "web_search.results": [{"url": "https://a.test", "title": "A"}],
        "data": "[{'url': 'https://stale.test'}]",
    }
    items = search_mod._search_items(payload)
    assert [i["url"] for i in items] == ["https://a.test"]


def test_search_items_falls_back_to_repr_string_data(search_mod):
    """"data" arrives as a Python repr of the item list, not JSON."""
    payload = {"data": "[{'url': 'https://b.test', 'title': 'B'}]"}
    assert search_mod._search_items(payload) == [{"url": "https://b.test", "title": "B"}]


def test_search_items_tolerates_unusable_payloads(search_mod):
    assert search_mod._search_items({"data": "not-a-list"}) == []
    assert search_mod._search_items({}) == []
    assert search_mod._search_items("nope") == []


def test_search_path_strips_label_prefix_from_scalar(search_mod):
    """Context-processed scalars come back as "<key>: <value>"."""
    assert search_mod._search_path({"web_search.web_search_path": "web_search_path: ddgs"}) == "ddgs"
    assert search_mod._search_path({"web_search_path": "native"}) == "native"
    assert search_mod._search_path({}) is None


def test_search_source_maps_live_payload_to_sources(search_mod):
    tools = Mock()
    tools.execute = Mock(
        return_value={
            "web_search.results": [
                {"url": "https://a.test", "title": "A", "content": "snippet a"},
                {"title": "no url — dropped"},
            ],
            "web_search.web_search_path": "web_search_path: ddgs",
        }
    )
    motet = MockMotetContext(tools=tools)

    result = search_mod.search_source(search_mod.SearchSourceData(query="q"), motet)

    assert result["ok"] is True
    assert result["result_count"] == 1
    assert result["results"][0] == {
        "url": "https://a.test",
        "title": "A",
        "snippet": "snippet a",
    }
    assert result["web_search_path"] == "ddgs"
    assert tools.execute.call_args.args[0] == "core.web_search"


def test_search_source_reports_tool_failure_without_raising(search_mod):
    tools = Mock()
    tools.execute = Mock(side_effect=RuntimeError("search backend down"))

    result = search_mod.search_source(
        search_mod.SearchSourceData(query="q"), MockMotetContext(tools=tools)
    )

    assert result["ok"] is False
    assert "search backend down" in result["error"]


# --- LLM planning ------------------------------------------------------------


def test_plan_queries_parses_json_array(plan_mod):
    models = Mock()
    models.infer = Mock(return_value={"content": '["query one", "query two"]'})

    result = plan_mod.plan_queries(
        plan_mod.PlanQueriesData(topic="rust async", num_queries=2),
        MockMotetContext(models=models),
    )

    assert result["queries"] == ["query one", "query two"]
    assert result["planning_status"] == "planned"


def test_plan_queries_reports_fallback_when_json_unparsable(plan_mod):
    models = Mock()
    models.infer = Mock(return_value={"content": "Sure! Here are some ideas."})

    result = plan_mod.plan_queries(
        plan_mod.PlanQueriesData(topic="rust async", num_queries=3),
        MockMotetContext(models=models),
    )

    # Searching the bare topic still works, but the degrade must be visible.
    assert result["queries"] == ["rust async"]
    assert result["planning_status"] == "fallback_topic_only"


def test_plan_queries_caps_to_requested_count(plan_mod):
    models = Mock()
    models.infer = Mock(return_value={"content": json.dumps(["a", "b", "c", "d"])})

    result = plan_mod.plan_queries(
        plan_mod.PlanQueriesData(topic="t", num_queries=2),
        MockMotetContext(models=models),
    )

    assert result["query_count"] == 2


# --- page fetch + extraction -------------------------------------------------


def test_extract_findings_parses_llm_json(extract_mod):
    tools = Mock()
    tools.execute = Mock(return_value={"main_content": "x" * 400, "title": "Page"})
    models = Mock()
    models.infer = Mock(
        return_value={
            "content": json.dumps(
                {"findings": ["fact one", "fact two"], "relevance": "high", "summary": "useful"}
            )
        }
    )

    result = extract_mod.extract_findings(
        extract_mod.ExtractFindingsData(url="https://a.test", topic="t"),
        MockMotetContext(tools=tools, models=models),
    )

    assert result["ok"] is True
    assert result["findings"] == ["fact one", "fact two"]
    assert result["relevance"] == "high"
    assert result["title"] == "Page"


def test_extract_findings_never_sends_payload_repr_to_llm(extract_mod):
    """A page with no extractable text must not be stringified into a prompt."""
    tools = Mock()
    tools.execute = Mock(return_value={"status": 200, "unexpected_shape": True})
    models = Mock()
    models.infer = Mock(return_value={"content": "{}"})

    result = extract_mod.extract_findings(
        extract_mod.ExtractFindingsData(url="https://a.test", topic="t"),
        MockMotetContext(tools=tools, models=models),
    )

    assert result["relevance"] == "low"
    assert result["findings"] == []
    assert result["summary"] == "Page had insufficient content."
    models.infer.assert_not_called()


def test_extract_findings_reports_fetch_failure(extract_mod):
    tools = Mock()
    tools.execute = Mock(side_effect=TimeoutError("browser timeout"))

    result = extract_mod.extract_findings(
        extract_mod.ExtractFindingsData(url="https://a.test", topic="t"),
        MockMotetContext(tools=tools, models=Mock()),
    )

    assert result["ok"] is False
    assert "browser timeout" in result["error"]


# --- parallel fan-out --------------------------------------------------------


def test_gather_sources_dedupes_urls_and_skips_failed_searches(gather_mod):
    motet = MockMotetContext()
    motet.apply = Mock(
        return_value=[
            {
                "ok": True,
                "query": "q1",
                "results": [
                    {"url": "https://a.test", "title": "A", "snippet": ""},
                    {"url": "https://b.test", "title": "B", "snippet": ""},
                ],
            },
            {"ok": True, "query": "q2", "results": [{"url": "https://a.test", "title": "A dup"}]},
            {"ok": False, "query": "q3", "results": [], "error": "boom"},
        ]
    )

    result = gather_mod.gather_sources(
        gather_mod.GatherSourcesData(topic="t", queries=["q1", "q2", "q3"]), motet
    )

    assert [s["url"] for s in result["sources"]] == ["https://a.test", "https://b.test"]
    assert result["queries_executed"] == 3


def test_gather_sources_skips_apply_when_no_queries(gather_mod):
    motet = MockMotetContext()
    motet.apply = Mock()

    result = gather_mod.gather_sources(gather_mod.GatherSourcesData(topic="t", queries=[]), motet)

    assert result["source_count"] == 0
    motet.apply.assert_not_called()


def test_analyze_sources_caps_pages_and_ranks_by_relevance(analyze_mod):
    motet = MockMotetContext()
    motet.apply = Mock(
        return_value=[
            {"ok": True, "url": "https://low.test", "relevance": "low", "findings": []},
            {"ok": True, "url": "https://high.test", "relevance": "high", "findings": ["f"]},
        ]
    )

    result = analyze_mod.analyze_sources(
        analyze_mod.AnalyzeSourcesData(
            topic="t",
            sources=[{"url": f"https://{i}.test"} for i in range(5)],
            max_pages=2,
        ),
        motet,
    )

    assert len(motet.apply.call_args.kwargs["inputs"]) == 2
    assert [r["relevance"] for r in result["analyzed"]] == ["high", "low"]
    assert result["high_relevance_count"] == 1


# --- synthesis + memory persistence ------------------------------------------


def test_synthesize_stores_report_principal_scoped(synth_mod):
    models = Mock()
    models.infer = Mock(return_value={"content": "# Report\n\nFindings..."})
    memory = Mock()
    memory.store = Mock(return_value={"memory_id": "mem-1", "stored": True})

    result = synth_mod.synthesize(
        synth_mod.SynthesizeData(
            topic="rust async runtimes",
            analyzed=[
                {
                    "url": "https://a.test",
                    "title": "A",
                    "findings": ["tokio dominates"],
                    "relevance": "high",
                }
            ],
        ),
        MockMotetContext(models=models, memory=memory),
    )

    assert result["memory_store_status"] == "stored"
    assert result["memory_id"] == "mem-1"
    assert result["source_count"] == 1
    kwargs = memory.store.call_args.kwargs
    assert kwargs["scope_type"] == "principal"
    assert kwargs["type"] == "research_report"
    assert "deep-research" in kwargs["tags"]
    assert kwargs["metadata"]["topic"] == "rust async runtimes"


def test_synthesize_surfaces_memory_failure(synth_mod):
    models = Mock()
    models.infer = Mock(return_value={"content": "# Report"})
    memory = Mock()
    memory.store = Mock(side_effect=RuntimeError("memory backend down"))

    result = synth_mod.synthesize(
        synth_mod.SynthesizeData(
            topic="t",
            analyzed=[{"url": "https://a.test", "findings": ["f"], "relevance": "high"}],
        ),
        MockMotetContext(models=models, memory=memory),
    )

    assert result["memory_store_status"] == "error"
    assert "memory backend down" in result["memory_store_error"]


def test_synthesize_skips_llm_when_no_findings(synth_mod):
    models = Mock()
    models.infer = Mock()

    result = synth_mod.synthesize(
        synth_mod.SynthesizeData(
            topic="t", analyzed=[{"url": "https://a.test", "findings": [], "relevance": "low"}]
        ),
        MockMotetContext(models=models, memory=Mock()),
    )

    assert result["memory_store_status"] == "skipped_no_findings"
    assert "Next Step" in result["report"]
    models.infer.assert_not_called()


# --- recall tool -------------------------------------------------------------


def _report_memory(topic: str) -> dict:
    return {
        "content": f"Research report about {topic}",
        "type": "research_report",
        "tags": ["deep-research", "research"],
        "metadata": {"topic": topic},
        "created_at": "2026-08-03T00:00:00Z",
        "id": f"mem-{topic}",
        "scope_type": "principal",
    }


def test_recall_research_prefers_principal_scope(recall_mod, monkeypatch):
    memory = Mock()
    memory.recall_principal = Mock(return_value=[_report_memory("rust async runtimes")])
    memory.recall = Mock()
    ctx = MockMotetContext(memory=memory)
    monkeypatch.setattr(recall_mod, "get_motet_context", lambda: ctx)

    result = recall_mod.recall_research({"topic": "rust async runtimes", "limit": 3})

    assert result["recall_path"] == "principal"
    assert result["result_count"] == 1
    memory.recall.assert_not_called()
    kwargs = memory.recall_principal.call_args.kwargs
    assert kwargs["query"] == "rust async runtimes"
    assert kwargs["min_relevance"] == 0.8
    assert "deep-research" in (kwargs.get("tags") or [])


def test_recall_research_trusts_core_when_topic_misses(recall_mod, monkeypatch):
    """When core returns nothing for the query, the tool must not invent hits."""
    memory = Mock()
    memory.recall_principal = Mock(return_value=[])
    memory.recall = Mock(return_value=[])
    ctx = MockMotetContext(memory=memory)
    monkeypatch.setattr(recall_mod, "get_motet_context", lambda: ctx)

    result = recall_mod.recall_research({"topic": "zeppelin metallurgy", "limit": 3})

    assert result["result_count"] == 0
    assert memory.recall_principal.call_args.kwargs["min_relevance"] == 0.8


def test_recall_research_reports_missing_context(recall_mod, monkeypatch):
    monkeypatch.setattr(recall_mod, "get_motet_context", lambda: None)

    result = recall_mod.recall_research({"topic": "anything"})

    assert result["result_count"] == 0
    assert result["error"]
