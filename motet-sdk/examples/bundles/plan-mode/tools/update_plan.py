"""
Motet SDK - Plan Mode Example: update_plan Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-05

Description:
    Patch plan-level fields or replace the todo list when replanning
    mid-flight (#173B). Content edits reset approval_status to draft so
    the human gate re-applies before further build work. Dual-writes a
    markdown artifact snapshot for later read/search.

Dependencies:
    - motet_sdk: @motet.tool, get_motet_context
    - plan_store: apply_update_plan / save_plan_with_artifact

Usage:
    plan-mode.update_plan(summary="...", todos=[...])
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from motet_sdk import get_motet_context, motet

from ._plan_store import (
    apply_update_plan,
    load_plan,
    plan_response,
    save_plan_with_artifact,
)


class UpdatePlanParams(BaseModel):
    """Input for update_plan."""

    summary: Optional[str] = Field(default=None, description="Updated summary")
    files: Optional[List[str]] = Field(default=None, description="Replace files list")
    acceptance: Optional[List[str]] = Field(
        default=None,
        description="Replace acceptance checks",
    )
    open_questions: Optional[List[str]] = Field(
        default=None,
        description="Replace open questions",
    )
    todos: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Replace the full todo list (each needs id + title)",
    )
    goal: Optional[str] = Field(default=None, description="Updated goal text")


def _fmt(res: Dict[str, Any]) -> str:
    return f"update_plan(status={res.get('status')!r}, todos={res.get('todo_count', 0)})"


@motet.tool(
    description=(
        "Patch the conversation plan: optional summary, files, acceptance, "
        "open_questions, goal, and/or full todos replacement. Resets "
        "approval_status to draft — call plan-mode__approve_plan again "
        "before building. Use for mid-run replanning; prefer update_todo "
        "for single-item progress on an approved plan."
    ),
    name="update_plan",
    schema=UpdatePlanParams,
    observation_formatter=_fmt,
    category="planning",
    cost_class="low",
    keywords=["plan", "update", "replan", "todos", "draft"],
)
def update_plan(params: Dict[str, Any]) -> Dict[str, Any]:
    """Patch plan-level fields on the conversation plan."""
    parsed = UpdatePlanParams(**(params or {}))
    if all(
        v is None
        for v in (
            parsed.summary,
            parsed.files,
            parsed.acceptance,
            parsed.open_questions,
            parsed.todos,
            parsed.goal,
        )
    ):
        return {"status": "error", "error": "Provide at least one field to update"}

    try:
        ctx = get_motet_context()
    except Exception:
        ctx = None

    plan = load_plan(ctx)
    if plan is None:
        return {
            "status": "error",
            "error": "No plan stored for this conversation",
        }

    try:
        updated = apply_update_plan(
            plan,
            summary=parsed.summary,
            files=parsed.files,
            acceptance=parsed.acceptance,
            open_questions=parsed.open_questions,
            todos=parsed.todos,
            goal=parsed.goal,
        )
    except Exception as exc:
        return {"status": "error", "error": f"Invalid update: {exc}"}

    updated, key, artifact_id = save_plan_with_artifact(
        ctx, updated, snapshot_reason="update"
    )
    return plan_response(
        updated,
        status="updated",
        storage_key=key,
        artifact_id=artifact_id,
    )
