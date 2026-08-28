"""
Motet - Tools List Tool

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Tools list tool for the Motet distributed framework.
    Provides comprehensive tool listing capabilities with category filtering,
    name matching, and MCP tool filtering. Includes schema inclusion,
    result limiting, and comprehensive tool discovery for distributed systems.

Dependencies:
    - re: Regular expression matching and pattern search
    - pydantic: Data validation and model definitions
    - typing: Type hints and annotations
    - Tool registry and protocol system

Usage:
    from motet.core.tools.builtin.tools_list import run

    # List tools
    result = run(registry, {
        "category": "web",
        "name_contains": "search",
        "mcp_only": False,
        "limit": 50
    })

Notes:
    - Provides comprehensive tool listing capabilities
    - Includes category filtering and name matching
    - Supports MCP tool filtering and schema inclusion
    - Includes result limiting and comprehensive tool discovery
    - Supports distributed tool coordination
    - Integrates with tool registry and protocol system
    - Includes comprehensive observability and logging
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ..protocol import ok, err
from ..registry import ToolRegistry
from ...config import Config


class ToolsListParams(BaseModel):
    category: Optional[str] = Field(default=None, description="Filter by category")
    name_contains: Optional[str] = Field(default=None, description="Substring match against name/description")
    mcp_only: bool = Field(default=False, description="Only include MCP proxy tools")
    include_schema: bool = Field(default=False, description="Include JSON schema in results")
    include_x_imf: bool = Field(default=True, description="Include x-imf extensions in results")
    limit: Optional[int] = Field(default=100, description="Max tools to return")


def _filter_tools(items: List[Dict[str, Any]], params: ToolsListParams) -> List[Dict[str, Any]]:
    filtered: List[Dict[str, Any]] = []
    name_q = (params.name_contains or "").lower().strip()
    for it in items:
        name = str(it.get("name", ""))
        desc = str(it.get("description", ""))
        category = str(it.get("category", ""))
        if params.category and category != params.category:
            continue
        if name_q and (name_q not in name.lower() and name_q not in desc.lower()):
            continue
        if params.mcp_only and not name.startswith("mcp."):
            continue
        # Optionally drop schema/x-imf
        if not params.include_schema and "schema" in it:
            it = {k: v for k, v in it.items() if k != "schema"}
        if not params.include_x_imf and "x-imf" in it:
            it = {k: v for k, v in it.items() if k != "x-imf"}
        filtered.append(it)
    # Global allow/deny filters
    cfg = Config()
    allow = set(t.strip() for t in (cfg.tool_allowlist or "").split(",") if t.strip()) if cfg.tool_allowlist else None
    deny = set(t.strip() for t in (cfg.tool_denylist or "").split(",") if t.strip()) if cfg.tool_denylist else None
    if allow is not None:
        filtered = [it for it in filtered if it.get("name") in allow]
    if deny:
        filtered = [it for it in filtered if it.get("name") not in deny]
    # Limit
    lim = params.limit if (params.limit is not None and params.limit >= 0) else None
    return filtered[: lim] if lim is not None else filtered


def run(registry: ToolRegistry, params: Dict[str, Any]) -> Dict[str, Any]:
    """List configured tools with optional filters (synchronous for Celery workers - ADR-0033)."""
    try:
        parsed = ToolsListParams(**(params or {}))
    except Exception as exc:
        return err(f"validation error: {exc}")
    try:
        items = registry.describe()
    except Exception as exc:
        return err(str(exc))
    out = _filter_tools(items, parsed)
    return ok(out)


def _parse(line: str, trig: str) -> Dict[str, Any]:
    rest = line[len(trig):].strip()
    if not rest:
        return {}
    # key=value tokens separated by space or comma; booleans and ints supported
    params: Dict[str, Any] = {}
    for tok in [t for t in re.split(r"[ ,]+", rest) if t]:
        if "=" in tok:
            k, v = tok.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if v.lower() in {"true", "false"}:
                params[k.strip()] = (v.lower() == "true")
            else:
                try:
                    params[k.strip()] = int(v)
                except Exception:
                    params[k.strip()] = v
    return params if params else {"name_contains": rest}


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.tools_list",
        description="List configured tools with optional filters for planning/reasoning",
        func=lambda p, _r=registry: run(_r, p),
        tool_schema=ToolsListParams,
        triggers=["tools:", "tools_list:", "tool_list:"],
        parse_params=_parse,
        category="system",
        contextualize_observation=False,  # Don't truncate - user wants full list
        default_timeout_seconds=3.0,
        suggested_max_calls=1,
        cost_class="low",
    )


