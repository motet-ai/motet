"""
Motet SDK - Roundtable Example: roster Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    List the agents the facilitator may invite, so speaker selection is
    grounded in the agent registry rather than guessed. Returns each agent's
    id and description, which is what the facilitator reasons over when it
    decides who is best placed to answer the question in front of it.

Dependencies:
    - motet_sdk: @motet.tool, get_motet_context
    - MotetContext.agents.list → agent registry

Usage:
    roundtable.roster()
    roundtable.roster(bundle="roundtable")

Notes:
    - Excludes the facilitator itself; inviting yourself is a loop, not a panel.
    - Without ``bundle``, every visible agent is listed, including agents from
      other bundles. That is the point: the panel is not a fixed cast.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from motet_sdk import get_motet_context, motet

_FACILITATOR_ID = "roundtable.facilitator"


class RosterParams(BaseModel):
    """Input for roster."""

    bundle: Optional[str] = Field(
        default=None,
        description="Only list agents from this bundle (e.g. 'roundtable'). Omit for all visible agents.",
    )


def _fmt(res: Dict[str, Any]) -> str:
    return f"roster(count={res.get('count', 0)})"


def _as_dict(item: Any) -> Dict[str, Any]:
    if hasattr(item, "model_dump"):
        dumped = item.model_dump()
        if isinstance(dumped, dict):
            return dumped
    if isinstance(item, dict):
        return item
    return {"agent_id": str(item)}


@motet.tool(
    description=(
        "List the agents you can invite to speak, with their ids and "
        "descriptions. Call this before roundtable__invite so you choose a "
        "real agent id. Accepts 'bundle' (string, optional) to list only one "
        "bundle's agents."
    ),
    name="roster",
    schema=RosterParams,
    observation_formatter=_fmt,
    category="roundtable",
    cost_class="low",
    keywords=["who can speak", "available agents", "panel roster", "invite list"],
)
def roster(params: Dict[str, Any]) -> Dict[str, Any]:
    """Return invitable agents (id + description) from the agent registry."""
    parsed = RosterParams(**(params or {}))

    try:
        ctx = get_motet_context()
    except Exception:
        ctx = None

    if ctx is None or not getattr(ctx, "agents", None):
        return {"agents": [], "count": 0, "error": "Agent registry not available in current context."}

    try:
        listed = ctx.agents.list()
    except Exception as exc:
        return {"agents": [], "count": 0, "error": f"Agent list failed: {exc}"}

    agents: List[Dict[str, str]] = []
    for item in listed or []:
        dumped = _as_dict(item)
        agent_id = str(dumped.get("agent_id") or dumped.get("id") or "").strip()
        if not agent_id or agent_id == _FACILITATOR_ID:
            continue
        if parsed.bundle and not agent_id.startswith(f"{parsed.bundle}."):
            continue
        agents.append(
            {
                "agent_id": agent_id,
                "display_name": str(dumped.get("display_name") or ""),
                "description": str(dumped.get("description") or ""),
            }
        )

    agents.sort(key=lambda a: a["agent_id"])
    return {"agents": agents, "count": len(agents)}
