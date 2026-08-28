"""
Motet - Agent Handoff Builtin

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Per-agent delegation tool. In the function catalog like
    core.spawn_agents. The schema is also pinned when AgentConfig.handoffs
    is set. The model names a qualified target from that list and a
    message; the child turn runs under the same principal, tenant, and
    conversation. The result is one tool observation (final response
    plus usage). Child usage and cost roll up into the parent turn the
    same way spawn_agents children do.

    Depth is capped at 2. An agent already on the handoff path is not
    offered again. The tool is in the function catalog (same as
    core.spawn_agents). The schema is also pinned when this agent
    declared handoffs and depth is under the cap. The handler is the
    grant: an empty list or a target not on it fails closed.

Dependencies:
    - motet.core.orchestration.turn.agent_turn: child turn
    - motet.core.commands.command_data_classes.AgentTurnData: child payload
    - MotetContext: same conversation / principal as the parent

Usage:
    Catalog-visible. Also pinned when AgentConfig.handoffs is non-empty
    and handoff_depth < 2. The model calls
    core.handoff(agent_id="bundle.agent", message="...").

Notes:
    - In the catalog. Not always-sticky. Schema is force-included for
      the declaring agent so a search hop is not required.
    - Recursion A→B→A is blocked by the path list, not by a name filter
      on the tool itself.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ..protocol import err, ok
from ..registry import ToolRegistry

MAX_HANDOFF_DEPTH = 2


class Params(BaseModel):
    agent_id: str = Field(
        ...,
        description="Qualified agent id from this agent's handoffs list (e.g. bundle.reviewer).",
    )
    message: str = Field(
        ...,
        description="Instruction or question for the target agent. Returned as one observation.",
    )


def _get_motet_context_optional() -> Any:
    try:
        from motet.core.commands.decorator import get_motet_context

        return get_motet_context()
    except Exception:
        return None


def _as_path(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(item).strip() for item in value if str(item).strip()]


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Delegate one turn to a configured handoff target."""
    try:
        parsed = Params(**(params or {}))
    except Exception as exc:
        return err(f"validation error: {exc}")

    target = parsed.agent_id.strip()
    message = parsed.message.strip()
    if not target:
        return err("agent_id is required")
    if not message:
        return err("message is required")

    motet = _get_motet_context_optional()
    if motet is None:
        return err("core.handoff requires a distributed command context")

    metadata = dict(getattr(motet, "metadata", None) or {})
    allowed = _as_path(metadata.get("handoffs"))
    if target not in allowed:
        return err(
            f"{target} is not in this agent's handoffs list. "
            f"Allowed: {', '.join(allowed) or '(none)'}."
        )

    path = _as_path(metadata.get("handoff_path"))
    if target in path:
        return err(f"{target} is already on this handoff path; cycles are not allowed.")

    try:
        depth = int(metadata.get("handoff_depth") or 0)
    except (TypeError, ValueError):
        depth = 0
    if depth >= MAX_HANDOFF_DEPTH:
        return err(f"handoff depth {depth} exceeds the cap of {MAX_HANDOFF_DEPTH}")

    parent_id = str(metadata.get("agent_id") or "").strip()
    child_context = {
        key: value
        for key, value in metadata.items()
        if key
        not in {
            "handoffs",
            "tool_filter_metadata",
            "skill_refs",
            "prefilled_tool_calls",
            "handback_tools",
            "handback_tool_names",
        }
    }
    child_context["handoff_depth"] = depth + 1
    child_context["handoff_path"] = path + ([parent_id] if parent_id else [])
    if "agent_id" in child_context:
        del child_context["agent_id"]

    from motet.core.commands.command_data_classes import AgentTurnData
    from motet.core.orchestration.turn.agent_turn import agent_turn
    from motet.core.types import Message

    try:
        result = motet.do(
            agent_turn,
            data=AgentTurnData(
                agent_id=target,
                messages=[Message(role="user", content=message)],
                context=child_context,
            ),
        )
    except Exception as exc:
        return err(f"handoff to {target} failed: {exc}")

    payload = result if isinstance(result, dict) else {"response": str(result)}
    response = (
        payload.get("response")
        or payload.get("final_response")
        or payload.get("content")
        or ""
    )
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    cost = payload.get("cost_usd")
    return ok(
        {
            "agent_id": target,
            "response": response,
            "usage": usage,
            "cost_usd": cost,
        }
    )


def _fmt(res: Dict[str, Any]) -> str:
    if res.get("status") != "success":
        return f"handoff(error={res.get('error')})"
    result = res.get("result") or {}
    text = str(result.get("response") or "")
    preview = text[:240] + ("…" if len(text) > 240 else "")
    return f"handoff(agent_id={result.get('agent_id')}, response={preview})"


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.handoff",
        description=(
            "Delegate this turn to one configured teammate agent. "
            "Pass a qualified agent_id from this agent's handoffs list and the "
            "message they should answer. Only succeeds when this agent declared "
            "teammates; the list is the grant, not the catalog. The result comes "
            "back as one observation (their final response and usage). Same "
            "conversation and user; do not use this to impersonate someone else."
        ),
        func=run,
        tool_schema=Params,
        triggers=["handoff:"],
        priority=2,
        observation_formatter=_fmt,
        category="agents",
        default_timeout_seconds=600.0,
        suggested_max_calls=4,
        cost_class="high",
        keywords=["handoff", "delegate", "teammate", "agent", "transfer"],
    )


__all__ = ["MAX_HANDOFF_DEPTH", "register", "run"]
