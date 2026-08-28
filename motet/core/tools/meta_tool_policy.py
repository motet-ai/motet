"""
Motet - Meta-tool policy helpers (capability disclosure)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Shared authorization for ``core.tool_call`` and disclosure filtering for
    ``core.tools_search``. Enforces the calling agent's ToolFilter metadata so
    generic dispatch cannot bypass exclude/prefix/category/workflow gates that
    shaped the shortlist.

Dependencies:
    - typing / pydantic-free: filter metadata is a plain dict from
      ``get_discovery_filter_metadata``

Usage:
    from motet.core.tools.meta_tool_policy import (
        tool_permitted_by_filter,
        filter_tool_names,
        HARD_DENY_META_TARGETS,
    )

    ok, reason = tool_permitted_by_filter("mcp.google_workspace.list_events", meta)
    if not ok:
        ...

Notes:
    - Discovery mode (the Cursor / handback case) allows the full registry
      subject to exclude/prefix/category/no_workflows — residency is not the
      gate; that is the point of ``core.tool_call``.
    - When filter metadata is absent (non-discovery agents, or tools invoked
      outside an agent turn), permission defaults to allow except for the
      hard-deny recursion set.
    - ``core.tool_call`` itself is always denied as a target (no recursion).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Targets that must never be invoked through generic dispatch, regardless of
# ToolFilter. Recursion into tool_call would nest indefinitely; the meta tools
# are disclosure/dispatch surfaces, not catalog capabilities.
HARD_DENY_META_TARGETS = frozenset(
    {
        "core.tool_call",
    }
)


def _as_str_list(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        token = raw.strip()
        return [token] if token else []
    if isinstance(raw, (list, tuple, set)):
        out: List[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out
    return []


def tool_filter_metadata_from_context(motet: Any) -> Optional[Dict[str, Any]]:
    """Read ``tool_filter_metadata`` from MotetContext.metadata when present."""
    if motet is None:
        return None
    metadata = getattr(motet, "metadata", None) or {}
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get("tool_filter_metadata")
    return raw if isinstance(raw, dict) else None


def tool_permitted_by_filter(
    tool_name: str,
    tool_filter_metadata: Optional[Dict[str, Any]],
    *,
    tool_category: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Decide whether *tool_name* may be disclosed or invoked under the filter.

    Returns ``(permitted, reason)``. *reason* is empty when permitted.
    """
    name = (tool_name or "").strip()
    if not name:
        return False, "no tool name given"

    if name in HARD_DENY_META_TARGETS:
        return False, (
            f"tool {name!r} cannot be invoked via core.tool_call "
            "(meta-tool recursion is blocked)"
        )

    meta = tool_filter_metadata or {}
    if not meta:
        # No agent filter in scope — do not invent a tighter gate than today's
        # accidental "any registered name executes" path.
        return True, ""

    exclude_tools = set(_as_str_list(meta.get("exclude_tools")))
    exclude_workflows = _as_str_list(meta.get("exclude_workflows"))
    no_workflows = bool(meta.get("no_workflows"))
    prefix_list = _as_str_list(meta.get("prefix"))
    category_list = _as_str_list(meta.get("category"))

    for wf_id in exclude_workflows:
        exclude_tools.add(f"workflow_{wf_id}")

    if name in exclude_tools:
        return False, f"tool {name!r} is excluded by this agent's ToolFilter"

    if no_workflows and name.startswith("workflow_"):
        return False, "workflows are disabled for this agent (no_workflows=true)"

    if prefix_list and not any(name.startswith(p) for p in prefix_list):
        return False, (
            f"tool {name!r} does not match this agent's ToolFilter prefix "
            f"({', '.join(prefix_list)})"
        )

    if category_list and not name.startswith("workflow_"):
        cat = (tool_category or "general").strip() or "general"
        if cat not in set(category_list):
            return False, (
                f"tool {name!r} category {cat!r} is outside this agent's "
                f"ToolFilter categories ({', '.join(category_list)})"
            )

    return True, ""


def filter_tool_names(
    names: Iterable[str],
    tool_filter_metadata: Optional[Dict[str, Any]],
    *,
    category_for: Optional[Any] = None,
) -> List[str]:
    """
    Keep only names permitted by the filter.

    ``category_for`` is an optional callable ``(name) -> Optional[str]`` used
    when the filter has a category list.
    """
    kept: List[str] = []
    for name in names:
        cat = category_for(name) if callable(category_for) else None
        ok, _ = tool_permitted_by_filter(
            name, tool_filter_metadata, tool_category=cat
        )
        if ok:
            kept.append(name)
    return kept


def filter_described_tools(
    items: Sequence[Dict[str, Any]],
    tool_filter_metadata: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Filter ``registry.describe()`` rows by the agent ToolFilter."""
    out: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        cat = item.get("category")
        cat_s = str(cat) if isinstance(cat, str) else None
        ok, _ = tool_permitted_by_filter(
            name, tool_filter_metadata, tool_category=cat_s
        )
        if ok:
            out.append(item)
    return out


__all__ = [
    "HARD_DENY_META_TARGETS",
    "filter_described_tools",
    "filter_tool_names",
    "tool_filter_metadata_from_context",
    "tool_permitted_by_filter",
]
