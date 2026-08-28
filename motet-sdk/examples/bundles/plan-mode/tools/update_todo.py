"""
Motet SDK - Plan Mode Example: update_todo Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-05

Description:
    Patch one todo's status/notes on an approved plan. Keep exactly one
    todo in_progress while implementing (Claude TodoWrite discipline)
    (#173B). Rejects updates when approval_status is not approved.

Dependencies:
    - motet_sdk: @motet.tool, get_motet_context
    - plan_store: apply_update_todo / save_plan / require_approved_for_build

Usage:
    plan-mode.update_todo(todo_id="t1", status="in_progress")
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field

from motet_sdk import get_motet_context, motet

from ._plan_store import (
    TodoStatus,
    apply_update_todo,
    load_plan,
    plan_response,
    require_approved_for_build,
    save_plan,
)


class UpdateTodoParams(BaseModel):
    """Input for update_todo."""

    todo_id: str = Field(..., description="Todo id to update (e.g. t1)")
    status: Optional[Literal["pending", "in_progress", "completed", "cancelled"]] = Field(
        default=None,
        description="New status; omit to leave unchanged",
    )
    notes: Optional[str] = Field(
        default=None,
        description="Optional progress notes; omit to leave unchanged",
    )


def _fmt(res: Dict[str, Any]) -> str:
    return (
        f"update_todo(todo_id={res.get('todo_id')!r}, "
        f"status={res.get('status')!r})"
    )


@motet.tool(
    description=(
        "Update one plan todo's status and/or notes. Requires an approved "
        "plan (call plan-mode__approve_plan first). Prefer exactly one "
        "todo in_progress at a time; marking in_progress demotes others. "
        "Accepts 'todo_id' (required), optional 'status' "
        "(pending|in_progress|completed|cancelled), optional 'notes'."
    ),
    name="update_todo",
    schema=UpdateTodoParams,
    observation_formatter=_fmt,
    category="planning",
    cost_class="low",
    keywords=["plan", "todo", "progress", "status", "update"],
)
def update_todo(params: Dict[str, Any]) -> Dict[str, Any]:
    """Patch a single todo on the conversation plan (approved plans only)."""
    parsed = UpdateTodoParams(**(params or {}))
    if parsed.status is None and parsed.notes is None:
        return {
            "status": "error",
            "error": "Provide status and/or notes to update",
            "todo_id": parsed.todo_id,
        }

    try:
        ctx = get_motet_context()
    except Exception:
        ctx = None

    plan = load_plan(ctx)
    if plan is None:
        return {
            "status": "error",
            "error": "No plan stored for this conversation",
            "todo_id": parsed.todo_id,
        }

    gate_error = require_approved_for_build(plan)
    if gate_error:
        return {
            "status": "error",
            "error": gate_error,
            "todo_id": parsed.todo_id,
            "approval_status": plan.approval_status,
        }

    try:
        status: Optional[TodoStatus] = parsed.status  # type: ignore[assignment]
        updated = apply_update_todo(
            plan,
            parsed.todo_id,
            status=status,
            notes=parsed.notes,
        )
    except KeyError as exc:
        return {
            "status": "error",
            "error": str(exc),
            "todo_id": parsed.todo_id,
        }

    key = save_plan(ctx, updated)
    return plan_response(
        updated,
        status="updated",
        todo_id=parsed.todo_id,
        storage_key=key,
    )
