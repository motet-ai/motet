"""
Unit tests for ToolSchemaExporter (canonical-only, ADR-0137).

Tests canonical export, caching, and JSON Schema extraction.
"""

import pytest
from pydantic import BaseModel, Field
from typing import List, Optional
from unittest.mock import Mock

from motet.core.tools.schema_exporter import ToolSchemaExporter
from motet.core.tools.registry import RegisteredTool
from motet.core.types import CanonicalToolSchema


class MockToolParams(BaseModel):
    """Mock Pydantic model for tool parameters."""
    location: str = Field(description="Location name")
    unit: Optional[str] = Field(default="fahrenheit", description="Temperature unit")


class NestedItem(BaseModel):
    """Nested model that Pydantic emits under $defs."""
    artifact_id: str = Field(description="Artifact id")
    path: Optional[str] = Field(default=None, description="Path")


class NestedToolParams(BaseModel):
    """Params with a nested list — triggers $ref + $defs in model_json_schema()."""
    command: str = Field(description="Command")
    input_artifacts: List[NestedItem] = Field(
        default_factory=list,
        description="Nested inputs",
    )


class TestToolSchemaExporter:
    """Test suite for ToolSchemaExporter."""

    @pytest.fixture
    def mock_registry(self):
        """Create mock tool registry."""
        registry = Mock()

        def mock_tool_func(params):
            return {"result": "success"}

        mock_tool = RegisteredTool(
            name="get_weather",
            description="Get weather for a location",
            func=mock_tool_func,
            schema=MockToolParams,
            triggers=["weather:"],
            category="external"
        )

        registry.list_items.return_value = {"get_weather": mock_tool}
        registry.get.return_value = mock_tool

        return registry

    @pytest.fixture
    def exporter(self, mock_registry):
        """Create ToolSchemaExporter instance."""
        return ToolSchemaExporter(mock_registry)

    def test_export_canonical(self, exporter):
        """Export is CanonicalToolSchema only (ADR-0137)."""
        tools = exporter.export_canonical()

        assert len(tools) == 1
        assert isinstance(tools[0], CanonicalToolSchema)
        assert tools[0].name == "get_weather"
        assert tools[0].description == "Get weather for a location"
        assert tools[0].json_schema["type"] == "object"
        assert "location" in tools[0].json_schema["properties"]
        assert "unit" in tools[0].json_schema["properties"]

    def test_export_with_max_tools(self, mock_registry):
        """Test limiting number of tools exported."""
        mock_registry.list_items.return_value = {"tool1": object(), "tool2": object(), "tool3": object()}

        def mock_get(name):
            def mock_func(params):
                return {}
            return RegisteredTool(
                name=name,
                description=f"Description for {name}",
                func=mock_func,
                schema=None
            )

        mock_registry.get.side_effect = mock_get

        exporter = ToolSchemaExporter(mock_registry)
        tools = exporter.export_canonical(max_tools=2)

        assert len(tools) == 2

    def test_export_caching(self, exporter):
        """Test schema export caching."""
        tools1 = exporter.export_canonical()
        tools2 = exporter.export_canonical()

        assert tools1 == tools2
        assert exporter.registry.list_items.call_count == 1

    def test_cache_clear(self, exporter):
        """Test clearing the cache."""
        exporter.export_canonical()
        exporter.clear_cache()
        exporter.export_canonical()

        assert exporter.registry.list_items.call_count == 2

    def test_cache_ttl(self, exporter):
        """Test cache TTL."""
        import time

        exporter.set_cache_ttl(1)
        exporter.export_canonical()
        time.sleep(1.1)
        exporter.export_canonical()

        assert exporter.registry.list_items.call_count == 2

    def test_extract_json_schema_with_pydantic_model(self, exporter):
        """Test JSON Schema extraction from Pydantic model."""
        schema = exporter._extract_json_schema(MockToolParams)

        assert schema["type"] == "object"
        assert "properties" in schema
        assert "location" in schema["properties"]
        assert "unit" in schema["properties"]

    def test_extract_json_schema_preserves_defs_for_nested_models(self, exporter):
        """Nested Pydantic models must keep $defs so $ref targets resolve (xAI)."""
        schema = exporter._extract_json_schema(NestedToolParams)

        items = schema["properties"]["input_artifacts"]["items"]
        assert items.get("$ref") == "#/$defs/NestedItem"
        assert "$defs" in schema
        assert "NestedItem" in schema["$defs"]
        assert "artifact_id" in schema["$defs"]["NestedItem"]["properties"]

    def test_extract_json_schema_with_none(self, exporter):
        """Test JSON Schema extraction with no model."""
        schema = exporter._extract_json_schema(None)

        assert schema["type"] == "object"
        assert schema["properties"] == {}
        assert schema["required"] == []

    def test_export_for_provider_removed(self, exporter):
        """ADR-0137: provider formatters are adapter-owned."""
        assert not hasattr(exporter, "export_for_provider")
        assert not hasattr(exporter, "_to_openai_format")
        assert not hasattr(exporter, "_sanitize_tool_name")
