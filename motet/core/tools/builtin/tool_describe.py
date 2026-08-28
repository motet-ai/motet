"""
Motet - Tool Describe Builtin

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Tool describe builtin for the Motet distributed framework.
    Provides comprehensive tool description and schema information for
    registered tools. Includes schema validation, metadata extraction,
    and detailed tool information for distributed tool discovery.

Dependencies:
    - pydantic: Data validation and model definitions
    - typing: Type hints and annotations
    - Tool registry and protocol system

Usage:
    from motet.core.tools.builtin.tool_describe import run

    # Describe tool
    result = run(registry, {
        "name": "web_search",
        "include_schema": True,
        "include_x_imf": True
    })

Notes:
    - Provides comprehensive tool description and schema information
    - Includes schema validation and metadata extraction
    - Supports detailed tool information for distributed discovery
    - Includes error handling and validation
    - Integrates with tool registry and protocol system
    - Supports distributed tool coordination
    - Includes comprehensive observability and logging
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from ..protocol import ok, err
from ..registry import ToolRegistry


class ToolDescribeParams(BaseModel):
    name: Optional[str] = Field(default=None, description="Exact tool name (if not provided, returns error)")
    include_schema: bool = Field(default=True)
    include_x_imf: bool = Field(default=True)


def run(registry: ToolRegistry, params: Dict[str, Any]) -> Dict[str, Any]:
    """Describe a specific tool (synchronous for Celery workers - ADR-0033)."""
    try:
        p = ToolDescribeParams(**(params or {}))
    except Exception as exc:
        return err(f"validation error: {exc}")
    
    # Check if name parameter is provided
    if not p.name:
        return err("tool name is required - use 'tool:tool_name' or 'tool_describe:tool_name'")
    
    # Normalize tool name - strip common prefixes that LLMs sometimes add
    # OpenAI models sometimes prefix tool names with "functions." in their internal namespace
    tool_name = p.name
    for prefix in ("functions.", "function.", "tools.", "tool."):
        if tool_name.startswith(prefix):
            tool_name = tool_name[len(prefix):]
            break
    try:
        items = registry.describe()
        it = next((x for x in items if x.get("name") == tool_name), None)
        if not it:
            return err("tool not found")
        if not p.include_schema:
            it = {k: v for k, v in it.items() if k != "schema"}
        if not p.include_x_imf:
            it = {k: v for k, v in it.items() if k != "x-imf"}
        return ok(it)
    except Exception as exc:
        return err(str(exc))


def _parse(line: str, trig: str) -> Dict[str, Any]:
    rest = line[len(trig):].strip()
    if not rest:
        return {}
    if rest.startswith("name="):
        return {"name": rest.split("=", 1)[1].strip()}
    return {"name": rest}


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.tool_describe",
        description="Describe a single tool including schema and planner hints",
        func=lambda p, _r=registry: run(_r, p),
        tool_schema=ToolDescribeParams,
        triggers=["tool:", "tool_describe:"],
        parse_params=_parse,
        category="system",
        contextualize_observation=False,  # Don't truncate - user wants full tool details
        default_timeout_seconds=3.0,
        suggested_max_calls=1,
        cost_class="low",
    )


