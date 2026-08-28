"""
Motet - Meta-tool disclosure unit tests (core.tools_search / core.tool_call)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Unit tests for progressive capability disclosure:
    - ToolFilter enforcement shared by search disclosure and tool_call
    - core.tools_search defaults to including schemas and omits excluded tools
    - core.tools_search prefers FunctionDiscoveryVectorStore ranking (lexical fallback)
      and returns top tools plus a small separately ranked workflow slice
    - core.tool_call validates parameters (schema echo on failure), refuses
      recursion / excluded targets, dispatches permitted tools, and routes
      workflow_* through workflow_execution
    - core.tool_call returns the target payload verbatim (parity with a direct
      call) and only reports its own dispatch-phase failures

Usage:
    pytest tests/unit/core/tools/test_meta_tool_disclosure.py -v
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel, Field

from motet.core.tools.builtin import tool_call as tool_call_mod
from motet.core.tools.builtin import tools_search as tools_search_mod
from motet.core.tools.meta_tool_policy import (
    filter_described_tools,
    tool_permitted_by_filter,
)
from motet.core.tools.protocol import ok
from motet.core.tools.registry import ToolRegistry


class _EchoParams(BaseModel):
    """Params for a tiny test tool."""

    value: str = Field(..., description="Value to echo")
    count: int = Field(default=1, ge=1, le=5)


def _registry_with_echo() -> ToolRegistry:
    reg = ToolRegistry()

    def _echo(params: Dict[str, Any]) -> Dict[str, Any]:
        parsed = _EchoParams(**(params or {}))
        return ok({"echoed": parsed.value, "count": parsed.count})

    reg.register(
        name="demo.echo",
        description="Echo a string for testing meta-tool dispatch",
        func=_echo,
        tool_schema=_EchoParams,
        category="demo",
    )
    reg.register(
        name="demo.secret",
        description="A tool agents may exclude",
        func=lambda p: ok({"secret": True}),
        tool_schema=_EchoParams,
        category="admin",
    )
    return reg


# --- policy -----------------------------------------------------------------


class TestMetaToolPolicy:
    def test_no_filter_allows_registered_names(self) -> None:
        ok_perm, reason = tool_permitted_by_filter("demo.echo", None)
        assert ok_perm is True
        assert reason == ""

    def test_hard_deny_tool_call_recursion(self) -> None:
        ok_perm, reason = tool_permitted_by_filter("core.tool_call", {"exclude_tools": []})
        assert ok_perm is False
        assert "recursion" in reason

    def test_exclude_tools(self) -> None:
        ok_perm, reason = tool_permitted_by_filter(
            "demo.secret", {"exclude_tools": ["demo.secret"]}
        )
        assert ok_perm is False
        assert "excluded" in reason

    def test_prefix_filter(self) -> None:
        meta = {"prefix": ["mcp.google_workspace."]}
        assert tool_permitted_by_filter("mcp.google_workspace.list_events", meta)[0] is True
        assert tool_permitted_by_filter("mcp.github.issue_read", meta)[0] is False

    def test_category_filter(self) -> None:
        meta = {"category": ["demo"]}
        assert tool_permitted_by_filter("demo.echo", meta, tool_category="demo")[0] is True
        assert tool_permitted_by_filter("demo.secret", meta, tool_category="admin")[0] is False

    def test_no_workflows(self) -> None:
        ok_perm, _ = tool_permitted_by_filter(
            "workflow_web_research", {"no_workflows": True}
        )
        assert ok_perm is False

    def test_filter_described_tools(self) -> None:
        items = [
            {"name": "demo.echo", "category": "demo"},
            {"name": "demo.secret", "category": "admin"},
        ]
        kept = filter_described_tools(items, {"exclude_tools": ["demo.secret"]})
        assert [i["name"] for i in kept] == ["demo.echo"]


# --- tools_search -----------------------------------------------------------


class TestToolsSearchDisclosure:
    def test_include_schema_defaults_true(self) -> None:
        reg = _registry_with_echo()
        out = tools_search_mod.run(reg, {"query": "echo"})
        assert out["status"] == "success"
        hits = out["result"]
        assert len(hits) >= 1
        assert hits[0]["name"] == "demo.echo"
        assert "schema" in hits[0]
        assert "properties" in hits[0]["schema"]

    def test_include_schema_false_strips(self) -> None:
        reg = _registry_with_echo()
        out = tools_search_mod.run(reg, {"query": "echo", "include_schema": False})
        assert "schema" not in out["result"][0]

    def test_respects_tool_filter_exclude(self) -> None:
        reg = _registry_with_echo()
        motet = MagicMock()
        motet.metadata = {"tool_filter_metadata": {"exclude_tools": ["demo.secret"]}}
        motet.function_discovery_store = None
        with patch.object(tools_search_mod, "_get_motet_context_optional", return_value=motet):
            out = tools_search_mod.run(reg, {"query": "demo"})
        names = [h["name"] for h in out["result"]]
        assert "demo.echo" in names
        assert "demo.secret" not in names

    def test_semantic_path_ranks_and_scores(self) -> None:
        reg = _registry_with_echo()
        store = MagicMock()
        store.is_initialized.return_value = True
        # Deliberately reverse registry order: secret first, echo second.
        store.search_functions.return_value = [
            {"type": "tool", "name": "demo.secret", "similarity_score": 0.91},
            {"type": "tool", "name": "demo.echo", "similarity_score": 0.42},
        ]
        motet = MagicMock()
        motet.metadata = {}
        motet.function_discovery_store = store
        motet.tenant_id = "t1"
        motet.motet_id = "m1"
        motet.principal_id = "p1"
        with patch.object(tools_search_mod, "_get_motet_context_optional", return_value=motet):
            out = tools_search_mod.run(reg, {"query": "echo a value", "mode": "semantic"})
        assert out["status"] == "success"
        names = [h["name"] for h in out["result"]]
        assert names == ["demo.secret", "demo.echo"]
        assert out["result"][0]["similarity_score"] == pytest.approx(0.91)
        assert "schema" in out["result"][0]
        assert store.search_functions.call_count == 2
        types = [c.kwargs["search_types"] for c in store.search_functions.call_args_list]
        assert types == [["tool"], ["workflow"]]

    def test_semantic_includes_workflows(self) -> None:
        reg = _registry_with_echo()
        store = MagicMock()
        store.is_initialized.return_value = True
        store.search_functions.return_value = [
            {
                "type": "workflow",
                "name": "workflow_web_research",
                "workflow_id": "web_research",
                "similarity_score": 0.88,
            },
            {"type": "tool", "name": "demo.echo", "similarity_score": 0.40},
        ]
        motet = MagicMock()
        motet.metadata = {}
        motet.function_discovery_store = store
        wf_item = {
            "name": "workflow_web_research",
            "description": "Multi-step web research",
            "category": "workflow",
            "schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            "type": "workflow",
            "workflow_id": "web_research",
        }
        with patch.object(tools_search_mod, "_get_motet_context_optional", return_value=motet):
            with patch.object(
                tools_search_mod,
                "_workflow_described_by_name",
                return_value={"workflow_web_research": wf_item},
            ):
                out = tools_search_mod.run(
                    reg, {"query": "research the web", "mode": "semantic"}
                )
        assert out["status"] == "success"
        names = [h["name"] for h in out["result"]]
        assert names == ["demo.echo", "workflow_web_research"]
        wf_hit = out["result"][1]
        assert wf_hit["type"] == "workflow"
        assert wf_hit["similarity_score"] == pytest.approx(0.88)
        assert "query" in wf_hit["schema"]["properties"]

    def test_assemble_typed_results_inserts_workflows_after_first_tool(self) -> None:
        tools = [
            {"name": f"mcp.playwright.browser_{i}", "type": "tool", "similarity_score": 0.11 - i * 0.001}
            for i in range(10)
        ]
        workflows = [
            {
                "name": "workflow_navigate_screenshot",
                "type": "workflow",
                "similarity_score": 0.09,
            }
        ]
        assembled = tools_search_mod.assemble_typed_results(tools, workflows, limit=10)
        names = [h["name"] for h in assembled]
        assert names[0] == "mcp.playwright.browser_0"
        assert "workflow_navigate_screenshot" in names
        assert names[1] == "workflow_navigate_screenshot"
        assert len([n for n in names if n.startswith("mcp.playwright.")]) == 10
        assert len(assembled) == 11

    def test_assemble_typed_results_drops_low_score_workflows(self) -> None:
        tools = [{"name": "demo.echo", "type": "tool", "similarity_score": 0.20}]
        workflows = [
            {
                "name": "workflow_unrelated",
                "type": "workflow",
                "similarity_score": 0.04,
            }
        ]
        assembled = tools_search_mod.assemble_typed_results(tools, workflows, limit=10)
        assert [h["name"] for h in assembled] == ["demo.echo"]

    def test_assemble_typed_results_keeps_lexical_workflows_without_scores(self) -> None:
        tools = [{"name": "demo.echo", "type": "tool"}]
        workflows = [{"name": "workflow_navigate_screenshot", "type": "workflow"}]
        assembled = tools_search_mod.assemble_typed_results(tools, workflows, limit=10)
        assert [h["name"] for h in assembled] == [
            "demo.echo",
            "workflow_navigate_screenshot",
        ]

    def test_assemble_typed_results_caps_workflows_at_three(self) -> None:
        tools = [{"name": "demo.echo", "type": "tool", "similarity_score": 0.2}]
        workflows = [
            {"name": f"workflow_w{i}", "type": "workflow", "similarity_score": 0.18}
            for i in range(5)
        ]
        assembled = tools_search_mod.assemble_typed_results(tools, workflows, limit=10)
        wf_names = [h["name"] for h in assembled if h["name"].startswith("workflow_")]
        assert wf_names == ["workflow_w0", "workflow_w1", "workflow_w2"]

    def test_semantic_playwright_wall_still_surfaces_workflow(self) -> None:
        reg = _registry_with_echo()
        store = MagicMock()
        store.is_initialized.return_value = True

        def _search(**kwargs: Any) -> list:
            if kwargs.get("search_types") == ["workflow"]:
                return [
                    {
                        "type": "workflow",
                        "name": "workflow_navigate_screenshot",
                        "workflow_id": "navigate_screenshot",
                        "similarity_score": 0.09,
                    }
                ]
            return [
                {"type": "tool", "name": "demo.echo", "similarity_score": 0.11},
                {"type": "tool", "name": "demo.secret", "similarity_score": 0.10},
            ]

        store.search_functions.side_effect = _search
        motet = MagicMock()
        motet.metadata = {}
        motet.function_discovery_store = store
        wf_item = {
            "name": "workflow_navigate_screenshot",
            "description": "Navigate to a URL and take a screenshot",
            "category": "workflow",
            "schema": {"type": "object", "properties": {"url": {"type": "string"}}},
            "type": "workflow",
            "workflow_id": "navigate_screenshot",
        }
        with patch.object(tools_search_mod, "_get_motet_context_optional", return_value=motet):
            with patch.object(
                tools_search_mod,
                "_workflow_described_by_name",
                return_value={"workflow_navigate_screenshot": wf_item},
            ):
                out = tools_search_mod.run(
                    reg,
                    {
                        "query": "navigate to a website and take a screenshot with browser",
                        "mode": "semantic",
                    },
                )
        assert out["status"] == "success"
        names = [h["name"] for h in out["result"]]
        assert names[0] == "demo.echo"
        assert "workflow_navigate_screenshot" in names

    def test_semantic_can_exclude_workflows_param(self) -> None:
        reg = _registry_with_echo()
        store = MagicMock()
        store.is_initialized.return_value = True
        store.search_functions.return_value = [
            {"type": "tool", "name": "demo.echo", "similarity_score": 0.5},
        ]
        motet = MagicMock()
        motet.metadata = {}
        motet.function_discovery_store = store
        with patch.object(tools_search_mod, "_get_motet_context_optional", return_value=motet):
            out = tools_search_mod.run(
                reg,
                {"query": "echo", "mode": "semantic", "include_workflows": False},
            )
        assert out["status"] == "success"
        kwargs = store.search_functions.call_args.kwargs
        assert kwargs["search_types"] == ["tool"]

    def test_include_workflows_hidden_from_llm_schema(self) -> None:
        from motet.core.tools.schema_exporter import ToolSchemaExporter

        raw = tools_search_mod.ToolsSearchParams.model_json_schema()
        assert raw["properties"]["include_workflows"]["x-imf-hide-from-llm"] is True

        exported = ToolSchemaExporter(_registry_with_echo())._extract_json_schema(
            tools_search_mod.ToolsSearchParams
        )
        assert "include_workflows" not in exported["properties"]
        assert "query" in exported["properties"]

    def test_workflow_describe_uses_live_discovery_keywords(self) -> None:
        by_name = tools_search_mod._workflow_described_by_name()
        item = by_name.get("workflow_navigate_screenshot")
        assert item is not None
        assert "playwright" in item["keywords"]
        assert "browser" in item["keywords"]
        assert item["keywords"] != ["workflow"]

    def test_semantic_respects_exclude_after_rank(self) -> None:
        reg = _registry_with_echo()
        store = MagicMock()
        store.is_initialized.return_value = True
        store.search_functions.return_value = [
            {"type": "tool", "name": "demo.secret", "similarity_score": 0.9},
            {"type": "tool", "name": "demo.echo", "similarity_score": 0.8},
        ]
        motet = MagicMock()
        motet.metadata = {"tool_filter_metadata": {"exclude_tools": ["demo.secret"]}}
        motet.function_discovery_store = store
        with patch.object(tools_search_mod, "_get_motet_context_optional", return_value=motet):
            out = tools_search_mod.run(reg, {"query": "demo", "mode": "semantic"})
        names = [h["name"] for h in out["result"]]
        assert names == ["demo.echo"]

    def test_auto_falls_back_to_lexical_without_store(self) -> None:
        reg = _registry_with_echo()
        with patch.object(tools_search_mod, "_get_motet_context_optional", return_value=None):
            out = tools_search_mod.run(reg, {"query": "echo", "mode": "auto"})
        assert out["status"] == "success"
        assert out["result"][0]["name"] == "demo.echo"
        assert "similarity_score" not in out["result"][0]

    def test_regex_forces_lexical(self) -> None:
        reg = _registry_with_echo()
        store = MagicMock()
        store.is_initialized.return_value = True
        store.search_functions.return_value = [
            {"type": "tool", "name": "demo.secret", "similarity_score": 0.99},
        ]
        motet = MagicMock()
        motet.metadata = {}
        motet.function_discovery_store = store
        with patch.object(tools_search_mod, "_get_motet_context_optional", return_value=motet):
            out = tools_search_mod.run(
                reg, {"query": r"demo\.echo", "regex": True, "mode": "auto"}
            )
        assert out["status"] == "success"
        assert [h["name"] for h in out["result"]] == ["demo.echo"]
        store.search_functions.assert_not_called()

    def test_semantic_mode_errors_without_store(self) -> None:
        reg = _registry_with_echo()
        with patch.object(tools_search_mod, "_get_motet_context_optional", return_value=None):
            out = tools_search_mod.run(reg, {"query": "echo", "mode": "semantic"})
        assert out["status"] == "error"
        assert "unavailable" in out["error"]


# --- tool_call --------------------------------------------------------------


class TestToolCall:
    def test_dispatches_permitted_tool_in_process(self) -> None:
        reg = _registry_with_echo()
        with patch.object(tool_call_mod, "_get_motet_context_optional", return_value=None):
            out = tool_call_mod.run(
                reg,
                {"tool_name": "demo.echo", "parameters": {"value": "hi", "count": 2}},
            )
        assert out["status"] == "success"
        assert out["result"]["echoed"] == "hi"
        assert out["result"]["count"] == 2

    def test_leftover_wire_format_name_is_not_found(self) -> None:
        """Issue #225: leftover ``demo__echo`` is not converted at dispatch."""
        reg = _registry_with_echo()
        with patch.object(tool_call_mod, "_get_motet_context_optional", return_value=None):
            out = tool_call_mod.run(
                reg,
                {"tool_name": "demo__echo", "parameters": {"value": "x"}},
            )
        assert out["status"] == "error"

    def test_validation_error_echoes_schema(self) -> None:
        reg = _registry_with_echo()
        with patch.object(tool_call_mod, "_get_motet_context_optional", return_value=None):
            out = tool_call_mod.run(
                reg,
                {"tool_name": "demo.echo", "parameters": {"count": 99}},  # missing value, bad count
            )
        assert out["status"] == "error"
        assert "expected_schema" in (out.get("meta") or {})
        assert "validation_errors" in (out.get("meta") or {})
        props = out["meta"]["expected_schema"]["properties"]
        assert "value" in props

    def test_denies_excluded_tool(self) -> None:
        reg = _registry_with_echo()
        motet = MagicMock()
        motet.metadata = {"tool_filter_metadata": {"exclude_tools": ["demo.echo"]}}
        with patch.object(tool_call_mod, "_get_motet_context_optional", return_value=motet):
            out = tool_call_mod.run(
                reg, {"tool_name": "demo.echo", "parameters": {"value": "x"}}
            )
        assert out["status"] == "error"
        assert out.get("meta", {}).get("denied") is True

    def test_denies_tool_hidden_from_agents(self) -> None:
        """registry.describe() hides expose_to_agents=False; dispatch must too."""
        reg = _registry_with_echo()
        reg.register(
            name="demo.internal",
            description="Internal-only tool never disclosed to agents",
            func=lambda p: ok({"internal": True}),
            tool_schema=_EchoParams,
            category="demo",
            expose_to_agents=False,
        )
        assert all(i["name"] != "demo.internal" for i in reg.describe())
        with patch.object(tool_call_mod, "_get_motet_context_optional", return_value=None):
            out = tool_call_mod.run(
                reg, {"tool_name": "demo.internal", "parameters": {"value": "x"}}
            )
        assert out["status"] == "error"
        assert out.get("meta", {}).get("denied") is True
        assert "not exposed to agents" in out["error"]

    def test_denies_self_recursion(self) -> None:
        reg = _registry_with_echo()
        out = tool_call_mod.run(
            reg, {"tool_name": "core.tool_call", "parameters": {"tool_name": "demo.echo"}}
        )
        assert out["status"] == "error"
        assert "recursion" in out["error"]

    def test_denies_workflow_when_no_workflows(self) -> None:
        reg = _registry_with_echo()
        motet = MagicMock()
        motet.metadata = {"tool_filter_metadata": {"no_workflows": True}}
        with patch.object(tool_call_mod, "_get_motet_context_optional", return_value=motet):
            out = tool_call_mod.run(
                reg, {"tool_name": "workflow_web_research", "parameters": {"query": "x"}}
            )
        assert out["status"] == "error"
        assert out.get("meta", {}).get("denied") is True
        assert "no_workflows" in out["error"]

    def test_workflow_requires_motet_context(self) -> None:
        reg = _registry_with_echo()
        with patch.object(tool_call_mod, "_get_motet_context_optional", return_value=None):
            with patch.object(tool_call_mod, "_workflow_schema_for", return_value=None):
                out = tool_call_mod.run(
                    reg,
                    {"tool_name": "workflow_web_research", "parameters": {"query": "x"}},
                )
        assert out["status"] == "error"
        assert "MotetContext" in out["error"]

    def test_dispatches_workflow_via_motet_do(self) -> None:
        reg = _registry_with_echo()
        motet = MagicMock()
        motet.metadata = {}
        motet.do = MagicMock(return_value={"workflow_id": "web_research", "ok": True})
        fake_data = MagicMock()
        with patch.object(tool_call_mod, "_get_motet_context_optional", return_value=motet):
            with patch.object(
                tool_call_mod,
                "_workflow_schema_for",
                return_value={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            ):
                with patch(
                    "motet.core.workflow.WorkflowRegistry.prepare_workflow_for_execution",
                    return_value=fake_data,
                ) as prep:
                    out = tool_call_mod.run(
                        reg,
                        {
                            "tool_name": "workflow_web_research",
                            "parameters": {"query": "cnn"},
                        },
                    )
        assert out["status"] == "success"
        assert out["result"]["ok"] is True
        assert out["meta"]["kind"] == "workflow"
        assert out["meta"]["workflow_id"] == "web_research"
        prep.assert_called_once()
        assert prep.call_args.kwargs["workflow_id"] == "web_research"
        assert prep.call_args.kwargs["llm_parameters"] == {"query": "cnn"}
        assert motet.do.called

    def test_workflow_validation_echoes_schema(self) -> None:
        reg = _registry_with_echo()
        motet = MagicMock()
        motet.metadata = {}
        with patch.object(tool_call_mod, "_get_motet_context_optional", return_value=motet):
            with patch.object(
                tool_call_mod,
                "_workflow_schema_for",
                return_value={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            ):
                out = tool_call_mod.run(
                    reg, {"tool_name": "workflow_web_research", "parameters": {}}
                )
        assert out["status"] == "error"
        assert "expected_schema" in (out.get("meta") or {})
        assert "query" in out["meta"]["expected_schema"]["properties"]

    def test_not_found(self) -> None:
        reg = _registry_with_echo()
        with patch.object(tool_call_mod, "_get_motet_context_optional", return_value=None):
            out = tool_call_mod.run(
                reg, {"tool_name": "demo.missing", "parameters": {}}
            )
        assert out["status"] == "error"
        assert "not found" in out["error"]

    def test_nested_dispatch_via_motet_do(self) -> None:
        reg = _registry_with_echo()
        motet = MagicMock()
        motet.metadata = {}
        motet.do = MagicMock(
            return_value={"tool_name": "demo.echo", "result": ok({"echoed": "via-do"}), "executed": True}
        )
        with patch.object(tool_call_mod, "_get_motet_context_optional", return_value=motet):
            out = tool_call_mod.run(
                reg, {"tool_name": "demo.echo", "parameters": {"value": "via-do"}}
            )
        assert out["status"] == "success"
        assert out["result"]["echoed"] == "via-do"
        assert motet.do.called

    def _dispatch_returning(self, payload: Any) -> Dict[str, Any]:
        """Run core.tool_call against a nested tool_execution returning ``payload``."""
        reg = _registry_with_echo()
        motet = MagicMock()
        motet.metadata = {}
        motet.do = MagicMock(
            return_value={"tool_name": "demo.echo", "result": payload, "executed": True}
        )
        with patch.object(tool_call_mod, "_get_motet_context_optional", return_value=motet):
            return tool_call_mod.run(
                reg, {"tool_name": "demo.echo", "parameters": {"value": "x"}}
            )

    def test_target_payload_returned_verbatim(self) -> None:
        """artifact_read-style status=ok reaches the model exactly as written."""
        payload = {
            "status": "ok",
            "text": "CNN homepage…",
            "read_source": "raw_payload",
            "total_chars": 14,
        }
        assert self._dispatch_returning(payload) == payload

    def test_soft_not_ready_passes_through(self) -> None:
        payload = {
            "status": "not_ready",
            "artifact_id": "a1",
            "message": "Derived text is not available yet for this artifact.",
        }
        assert self._dispatch_returning(payload) == payload

    def test_target_error_passes_through_untranslated(self) -> None:
        """A failing target reads the same as it would on a direct call."""
        payload = {"status": "error", "error": "boom"}
        out = self._dispatch_returning(payload)
        assert out == payload
        # Not a dispatch failure: tool_call did its job.
        assert "meta" not in out

    def test_dispatch_failures_are_tagged(self) -> None:
        reg = _registry_with_echo()
        with patch.object(tool_call_mod, "_get_motet_context_optional", return_value=None):
            out = tool_call_mod.run(reg, {"tool_name": "demo.missing", "parameters": {}})
        assert out["status"] == "error"
        assert out["meta"]["phase"] == "dispatch"

    def test_passthrough_matches_direct_registry_call(self) -> None:
        """Dispatched and in-process paths must produce identical payloads."""
        reg = _registry_with_echo()
        with patch.object(tool_call_mod, "_get_motet_context_optional", return_value=None):
            direct = tool_call_mod.run(
                reg, {"tool_name": "demo.echo", "parameters": {"value": "x"}}
            )
        dispatched = self._dispatch_returning(direct)
        assert dispatched == direct

    def test_passthrough_wraps_non_dict_payload(self) -> None:
        out = tool_call_mod._passthrough_target_result(
            {"tool_name": "demo.echo", "result": "plain text", "executed": True}
        )
        assert out["status"] == "success"
        assert out["result"] == "plain text"

    def test_register_adds_core_tool_call(self) -> None:
        reg = ToolRegistry()
        tool_call_mod.register(reg)
        assert reg.get("core.tool_call") is not None
        schema = reg.get("core.tool_call").tool_schema.model_json_schema()
        assert "tool_name" in schema["properties"]
        assert "parameters" in schema["properties"]


def test_builtin_table_includes_tool_call() -> None:
    from motet.core.tools.builtin import _BUILTIN_TOOL_SPECS, register_all_builtin_tools

    labels = [label for label, _, _ in _BUILTIN_TOOL_SPECS]
    assert "tool_call" in labels
    fresh = ToolRegistry()
    register_all_builtin_tools(fresh, strict=True)
    assert fresh.get("core.tool_call") is not None
    assert fresh.get("core.tools_search") is not None
