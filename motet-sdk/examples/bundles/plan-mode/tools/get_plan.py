"""
Motet SDK - Plan Mode Example: get_plan Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-05

Description:
    Load the conversation-scoped plan (structured + markdown +
    approval_status). Used by plan-mode.plan-manager (and any agent)
    tracking progress (#173B).

Dependencies:
    - motet_sdk: @motet.tool, get_motet_context
    - plan_store: load_plan / plan_response

Usage:
    plan-mode.get_plan()
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel

from motet_sdk import get_motet_context, motet

from ._plan_store import load_plan, plan_response


class GetPlanParams(BaseModel):
    """No parameters; loads the plan for the current conversation."""


def _fmt(res: Dict[str, Any]) -> str:
    status = res.get("status", "?")
    n = res.get("todo_count", 0)
    return f"get_plan(status={status}, todos={n})"


@motet.tool(
    description=(
        "Load the current conversation's structured plan and a markdown "
        "view (includes approval_status and latest_artifact_id). Use "
        "before implementing and whenever you need progress context. "
        "No parameters."
    ),
    name="get_plan",
    schema=GetPlanParams,
    observation_formatter=_fmt,
    category="planning",
    cost_class="low",
    keywords=["plan", "get", "read", "todos", "progress"],
)
def get_plan(params: Dict[str, Any]) -> Dict[str, Any]:
    """Return the stored plan for this conversation."""
    _ = params
    try:
        ctx = get_motet_context()
    except Exception:
        ctx = None

    plan = load_plan(ctx)
    if plan is None:
        return {
            "status": "missing",
            "error": "No plan stored for this conversation. Chat as plan-mode.plan-manager or call plan-mode.start_plan first.",
            "plan": None,
            "markdown": "",
            "todo_count": 0,
        }
    return plan_response(plan, status="ok")
