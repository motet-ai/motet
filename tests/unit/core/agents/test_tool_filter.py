"""
Unit tests for ToolFilter and resolve_tools (ADR-0078, ADR-0093).

Tests ToolFilter model, get_discovery_filter_metadata, and resolve_tools
for mode handling, exclude/required filters, and migration from explicit_tools.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from motet.core.agents.registry import (
    ToolFilter,
    get_discovery_filter_metadata,
    resolve_tools,
)


def test_tool_filter_default_mode() -> None:
    """ToolFilter defaults to discovery mode."""
    tf = ToolFilter()
    assert tf.mode == "discovery"


def test_tool_filter_new_fields() -> None:
    """ToolFilter accepts ADR-0093 fields."""
    tf = ToolFilter(
        mode="discovery",
        required_tools=["a.b"],
        required_workflows=["w.x"],
        exclude_tools=["x.y"],
        exclude_workflows=["expert-panel.discuss"],
        no_workflows=True,
        prefix=["p."],
        category=["general"],
    )
    assert tf.required_tools == ["a.b"]
    assert tf.required_workflows == ["w.x"]
    assert tf.exclude_tools == ["x.y"]
    assert tf.exclude_workflows == ["expert-panel.discuss"]
    assert tf.no_workflows is True
    assert tf.prefix == ["p."]
    assert tf.category == ["general"]


def test_tool_filter_no_workflows_default() -> None:
    """ToolFilter no_workflows defaults to False."""
    tf = ToolFilter()
    assert tf.no_workflows is False


def test_tool_filter_prefix_category_union_str() -> None:
    """prefix and category accept str (normalized to list)."""
    tf = ToolFilter(mode="prefix", prefix="motet_admin.", category="general")
    # Model stores as-is; _to_list in resolve_tools normalizes
    assert tf.prefix == "motet_admin."
    assert tf.category == "general"


def test_get_discovery_filter_metadata_none() -> None:
    """get_discovery_filter_metadata returns None when tool_filter is None."""
    assert get_discovery_filter_metadata(None) is None


def test_get_discovery_filter_metadata_non_discovery() -> None:
    """get_discovery_filter_metadata returns None when mode != discovery."""
    tf = ToolFilter(mode="explicit", required_tools=["a"])
    assert get_discovery_filter_metadata(tf) is None


def test_get_discovery_filter_metadata_discovery() -> None:
    """get_discovery_filter_metadata returns dict when mode is discovery."""
    tf = ToolFilter(
        mode="discovery",
        exclude_tools=["x"],
        exclude_workflows=["expert-panel.discuss"],
        no_workflows=True,
        required_tools=["a"],
        required_workflows=["w"],
        prefix="p.",
        category="gen",
    )
    meta = get_discovery_filter_metadata(tf)
    assert meta is not None
    assert meta["exclude_tools"] == ["x"]
    assert meta["exclude_workflows"] == ["expert-panel.discuss"]
    assert meta["no_workflows"] is True
    assert meta["required_tools"] == ["a"]
    assert meta["required_workflows"] == ["w"]
    assert meta["prefix"] == ["p."]
    assert meta["category"] == ["gen"]


def test_resolve_tools_discovery_returns_none() -> None:
    """resolve_tools returns None for discovery mode."""
    tf = ToolFilter(mode="discovery")
    registry = MagicMock()
    exporter = MagicMock()
    result = resolve_tools(tf, registry, exporter)
    assert result is None


def test_resolve_tools_explicit_empty() -> None:
    """resolve_tools with explicit mode and empty required returns []."""
    tf = ToolFilter(mode="explicit", required_tools=[], required_workflows=[])
    registry = MagicMock()
    registry.list_items.return_value = {}
    exporter = MagicMock()
    exporter.export_canonical.return_value = []
    result = resolve_tools(tf, registry, exporter)
    assert result == []


def test_resolve_tools_explicit_tools_migration() -> None:
    """explicit_tools is used as fallback for required_tools (migration)."""
    tf = ToolFilter(mode="explicit", explicit_tools=["core.foo"])
    registry = MagicMock()
    registry.list_items.return_value = {"core.foo": MagicMock()}
    exporter = MagicMock()
    exporter.export_canonical.return_value = [MagicMock(name="core.foo")]
    result = resolve_tools(tf, registry, exporter)
    assert result is not None
    exporter.export_canonical.assert_called_once()
    call_kw = exporter.export_canonical.call_args[1]
    assert "core.foo" in call_kw["preselected_tools"]


def test_resolve_tools_prefix_mode() -> None:
    """resolve_tools prefix mode selects by prefix."""
    tf = ToolFilter(mode="prefix", prefix="motet_admin.")
    registry = MagicMock()
    registry.list_items.return_value = {
        "motet_admin.foo": MagicMock(),
        "motet_admin.bar": MagicMock(),
        "other.baz": MagicMock(),
    }
    exporter = MagicMock()
    exporter.export_canonical.return_value = [MagicMock(), MagicMock()]
    result = resolve_tools(tf, registry, exporter)
    assert result is not None
    tools_passed = exporter.export_canonical.call_args[1]["preselected_tools"]
    assert "motet_admin.foo" in tools_passed
    assert "motet_admin.bar" in tools_passed
    assert "other.baz" not in tools_passed


def test_resolve_tools_exclude_tools_filters_result() -> None:
    """exclude_tools removes matching tools before export."""
    tf = ToolFilter(mode="prefix", prefix="core.", exclude_tools=["core.bar"])
    registry = MagicMock()
    registry.list_items.return_value = {
        "core.foo": MagicMock(),
        "core.bar": MagicMock(),
    }
    exporter = MagicMock()

    def export_for_preselected(**kwargs):
        presel = kwargs.get("preselected_tools") or []
        return [SimpleNamespace(name=n) for n in presel]

    exporter.export_canonical.side_effect = export_for_preselected

    result = resolve_tools(tf, registry, exporter)
    assert result is not None
    names = [getattr(s, "name", "") for s in result]
    assert "core.foo" in names
    assert "core.bar" not in names


def test_apply_discovery_filters_exclude_workflows() -> None:
    """_apply_discovery_filters excludes workflows listed in exclude_workflows."""
    from motet.core.reasoning.react.loop_discovery import (
        _apply_discovery_filters,
    )

    schemas = [
        SimpleNamespace(name="core.foo"),
        SimpleNamespace(name="workflow_expert-panel.discuss"),
    ]
    meta = {"exclude_workflows": ["expert-panel.discuss"]}
    motet = MagicMock()
    result = _apply_discovery_filters(schemas, meta, motet)
    names = [getattr(s, "name", "") for s in result]
    assert "core.foo" in names
    assert "workflow_expert-panel.discuss" not in names


def test_apply_discovery_filters_no_workflows() -> None:
    """_apply_discovery_filters excludes all workflows when no_workflows is True."""
    from motet.core.reasoning.react.loop_discovery import (
        _apply_discovery_filters,
    )

    schemas = [
        SimpleNamespace(name="core.foo"),
        SimpleNamespace(name="workflow_expert-panel.discuss"),
        SimpleNamespace(name="workflow_other.thing"),
    ]
    meta = {"no_workflows": True}
    motet = MagicMock()
    result = _apply_discovery_filters(schemas, meta, motet)
    names = [getattr(s, "name", "") for s in result]
    assert "core.foo" in names
    assert "workflow_expert-panel.discuss" not in names
    assert "workflow_other.thing" not in names


def test_resolve_tools_no_workflows_excludes_workflows() -> None:
    """resolve_tools with no_workflows=True excludes workflows from result."""
    tf = ToolFilter(
        mode="explicit",
        required_tools=["core.foo"],
        required_workflows=["expert-panel.discuss"],
        no_workflows=True,
    )
    registry = MagicMock()
    registry.list_items.return_value = {"core.foo": MagicMock()}
    exporter = MagicMock()

    def export_for_preselected(**kwargs):
        presel = kwargs.get("preselected_tools") or []
        return [SimpleNamespace(name=n) for n in presel]

    exporter.export_canonical.side_effect = export_for_preselected

    result = resolve_tools(tf, registry, exporter)
    assert result is not None
    names = [getattr(s, "name", "") for s in result]
    assert "core.foo" in names
    assert "workflow_expert-panel.discuss" not in names


def test_apply_discovery_filters_prefix() -> None:
    """_apply_discovery_filters filters by prefix."""
    from motet.core.reasoning.react.loop_discovery import (
        _apply_discovery_filters,
    )

    schemas = [
        SimpleNamespace(name="core.foo"),
        SimpleNamespace(name="other.bar"),
    ]
    meta = {"prefix": ["core."]}
    motet = MagicMock()
    result = _apply_discovery_filters(schemas, meta, motet)
    names = [getattr(s, "name", "") for s in result]
    assert "core.foo" in names
    assert "other.bar" not in names


def test_core_default_uses_meta_tool_shortlist() -> None:
    """core.default uses discovery + meta-tool progressive disclosure."""
    from motet.core.agents.registry import get_agent_registry

    config = get_agent_registry().get("core.default")
    assert config is not None
    tf = config.tool_filter
    assert tf.mode == "discovery"
    assert not hasattr(tf, "pad_shortlist")
    assert not hasattr(tf, "freeze_shortlist")
    assert set(tf.required_tools or []) >= {
        "core.help",
        "core.tools_search",
        "core.tool_call",
    }
    assert config.max_tools >= 8
    assert "core.tools_search" in (config.system_prompt or "")
    assert "core.tool_call" in (config.system_prompt or "")
