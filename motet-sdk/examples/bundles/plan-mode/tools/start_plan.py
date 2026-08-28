"""
Motet SDK - Plan Mode Example: start_plan Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-05

Description:
    Kick off plan-mode.plan-manager via core.agent_turn in plan-only mode so
    another agent can request an inspectable draft plan (#173B). Does not
    approve or implement.

Dependencies:
    - motet_sdk: @motet.tool, get_motet_context
    - MotetContext.agents.turn → agent_turn command

Usage:
    plan-mode.start_plan(goal="Add dark mode to settings")
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from motet_sdk import get_motet_context, motet

from ._plan_store import load_plan, plan_response, render_markdown


class StartPlanParams(BaseModel):
    """Input for start_plan."""

    goal: str = Field(
        ...,
        description="Goal for the planner agent to turn into a structured plan",
    )


def _fmt(res: Dict[str, Any]) -> str:
    return f"start_plan(status={res.get('status')!r})"


def _extract_response_text(result: Any) -> str:
    if not isinstance(result, dict):
        return str(result or "")
    for key in ("final_response", "response", "content", "text"):
        val = result.get(key)
        if isinstance(val, str) and val.strip():
            return val
    data = result.get("data")
    if isinstance(data, dict):
        for key in ("final_response", "response", "content", "text"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val
    return ""


@motet.tool(
    description=(
        "Start plan-mode.plan-manager on a goal in plan-only mode. Explores context "
        "and writes a structured plan via plan-mode__write_plan as draft; does "
        "not approve or implement. Use when the user wants an inspectable plan "
        "before implementation. Accepts 'goal' (string, required). After it "
        "returns, review the markdown; ask plan-mode.plan-manager to approve "
        "and implement."
    ),
    name="start_plan",
    schema=StartPlanParams,
    observation_formatter=_fmt,
    category="planning",
    cost_class="high",
    keywords=["plan", "planner", "start", "goal", "planning"],
)
def start_plan(params: Dict[str, Any]) -> Dict[str, Any]:
    """Run plan-mode.plan-manager for the given goal (plan only) and return the stored plan."""
    parsed = StartPlanParams(**(params or {}))

    try:
        ctx = get_motet_context()
    except Exception:
        ctx = None

    if ctx is None or not getattr(ctx, "agents", None):
        return {
            "status": "error",
            "error": "Agent turn not available in current context.",
            "goal": parsed.goal,
        }

    prompt = (
        f"Create an inspectable implementation plan for this goal:\n\n"
        f"{parsed.goal}\n\n"
        "Explore with read-only tools if helpful, then call "
        "plan-mode__write_plan with a structured plan (summary, todos with "
        "stable ids t1/t2/..., files, acceptance, open_questions). "
        "Leave the plan as draft. Do not call approve_plan. Do not implement "
        "— only plan, then stop."
    )

    try:
        result = ctx.agents.turn(
            "plan-mode.plan-manager",
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        return {
            "status": "error",
            "error": f"Plan-mode agent_turn failed: {exc}",
            "goal": parsed.goal,
        }

    plan = load_plan(ctx)
    assistant_text = _extract_response_text(result)
    if plan is None:
        return {
            "status": "incomplete",
            "error": (
                "Agent finished without calling write_plan. "
                "Ask the user to retry or chat as plan-mode.plan-manager directly."
            ),
            "goal": parsed.goal,
            "assistant_text": assistant_text,
            "plan": None,
            "markdown": "",
        }

    out = plan_response(plan, status="planned", goal=parsed.goal)
    if assistant_text:
        out["assistant_text"] = assistant_text
    # Ensure markdown present even if store path skipped render
    out.setdefault("markdown", render_markdown(plan))
    return out
