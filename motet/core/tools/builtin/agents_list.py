"""
Motet - Agents List Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Built-in tool that lists configured agents visible to the current runtime
    context. Uses the worker-local agent registry state (core + loaded bundles)
    and supports optional filtering by role, bundle, and name.

Dependencies:
    - pydantic: Parameter validation
    - motet.core.agents.discovery: Shared agent listing helper
    - motet.core.tools.registry: Runtime stack context resolution
    - motet.core.tools.protocol: Standard tool response envelope helpers

Usage:
    from motet.core.tools.builtin.agents_list import run
    result = run({"role": "admin", "bundle_id": "core"})

Notes:
    - Reads from worker-local registry state populated by bundle reload/startup.
    - Intended for discovery/planning prompts that need available agent IDs.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ..protocol import ok, err
from ..registry import ToolRegistry, get_runtime_stack


class AgentsListParams(BaseModel):
    """Parameters for listing visible agent configurations."""

    role: Optional[str] = Field(default=None, description="Optional role filter merged into principal roles.")
    principal_roles: Optional[List[str]] = Field(
        default=None,
        description="Optional explicit roles used for visibility filtering.",
    )
    bundle_id: Optional[str] = Field(
        default=None,
        description="Optional bundle namespace filter (e.g. 'core' or 'agent-configured').",
    )
    name_contains: Optional[str] = Field(
        default=None,
        description="Case-insensitive substring filter over qualified_id/display_name/description.",
    )
    limit: Optional[int] = Field(default=100, ge=1, le=500, description="Maximum number of agents to return.")
    offset: int = Field(default=0, ge=0, description="Number of agents to skip.")


def _resolve_principal_roles(parsed: AgentsListParams) -> List[str]:
    """Resolve roles from params and runtime stack context."""
    roles: List[str] = []
    if parsed.principal_roles:
        roles.extend([r for r in parsed.principal_roles if r])
    if parsed.role:
        roles.append(parsed.role)

    stack = get_runtime_stack()
    if stack is not None:
        stack_role = getattr(stack, "_role", None)
        if isinstance(stack_role, str) and stack_role:
            roles.append(stack_role)
        principal = getattr(stack, "_principal", None)
        principal_roles = getattr(principal, "roles", None)
        if isinstance(principal_roles, list):
            roles.extend([r for r in principal_roles if isinstance(r, str) and r])
    return sorted(set(roles))


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Return visible agents from current worker registry state."""
    try:
        parsed = AgentsListParams(**(params or {}))
    except Exception as exc:
        return err(f"validation error: {exc}")

    try:
        from ...agents.discovery import list_visible_agents

        roles = _resolve_principal_roles(parsed)
        agents = list_visible_agents(principal_roles=roles)

        bundle_filter = (parsed.bundle_id or "").strip()
        if bundle_filter:
            if bundle_filter == "core":
                agents = [a for a in agents if not a.get("bundle_id")]
            else:
                agents = [a for a in agents if a.get("bundle_id") == bundle_filter]

        q = (parsed.name_contains or "").strip().lower()
        if q:
            agents = [
                a
                for a in agents
                if q in str(a.get("qualified_id", "")).lower()
                or q in str(a.get("display_name", "")).lower()
                or q in str(a.get("description", "")).lower()
            ]

        total = len(agents)
        start = parsed.offset
        end = start + parsed.limit if parsed.limit else None
        paged = agents[start:end] if end is not None else agents[start:]

        return ok(
            {
                "total": total,
                "agents": paged,
                "limit": parsed.limit,
                "offset": parsed.offset,
                "roles_used": roles,
            }
        )
    except Exception as exc:
        return err(f"failed to list agents: {exc}")


def register(registry: ToolRegistry) -> None:
    """Register the built-in agents list tool."""
    registry.register(
        name="core.agents_list",
        description=(
            "List configured agents available on this worker (core + deployed bundles). "
            "Useful for discovering agent IDs and selecting which agent to run."
        ),
        func=run,
        tool_schema=AgentsListParams,
        triggers=["agents:", "agents_list:", "list_agents:"],
        category="system",
        default_timeout_seconds=3.0,
        suggested_max_calls=2,
        cost_class="low",
    )


__all__ = ["register", "run", "AgentsListParams"]

