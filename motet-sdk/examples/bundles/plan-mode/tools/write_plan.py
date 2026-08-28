"""
Motet SDK - Plan Mode Example: write_plan Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-05

Description:
    Create or replace the conversation-scoped plan document as draft
    (awaiting approval). Used by plan-mode.plan-manager after exploring
    context (#173B). Build requires plan-mode.approve_plan first.
    Dual-writes a markdown artifact snapshot for later read/search.

Dependencies:
    - motet_sdk: @motet.tool, get_motet_context
    - plan_store: PlanDocument persistence + markdown render + artifact snapshot

Usage:
    plan-mode.write_plan(goal=..., summary=..., todos=[{id, title, status}, ...])
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from motet_sdk import get_motet_context, motet

from ._plan_store import (
    PlanDocument,
    PlanTodo,
    plan_response,
    save_plan_with_artifact,
    utc_now_iso,
)


class WritePlanParams(BaseModel):
    """Input for write_plan."""

    goal: str = Field(..., description="Original user goal this plan addresses")
    summary: str = Field(..., description="One-paragraph plan summary")
    todos: List[Dict[str, Any]] = Field(
        ...,
        description=(
            "Ordered todos. Each item needs id (e.g. t1), title, optional "
            "status (pending|in_progress|completed|cancelled), optional notes."
        ),
    )
    files: Optional[List[str]] = Field(
        default=None,
        description="Paths expected to change",
    )
    acceptance: Optional[List[str]] = Field(
        default=None,
        description="Overall acceptance checks",
    )
    open_questions: Optional[List[str]] = Field(
        default=None,
        description="Unresolved design questions",
    )


def _fmt(res: Dict[str, Any]) -> str:
    n = res.get("todo_count", 0)
    status = res.get("status", "?")
    return f"write_plan(status={status}, todos={n})"


@motet.tool(
    description=(
        "Create or replace the structured plan for this conversation "
        "(JSON source of truth in Redis) as draft awaiting approval. Also "
        "dual-writes a searchable markdown artifact (tag plan-mode). Call "
        "once when the plan is ready, then stop for user approval. Accepts "
        "'goal' (string), 'summary' (string), 'todos' (list of {id, title, "
        "status?, notes?}), optional 'files', 'acceptance', and "
        "'open_questions'. Returns the plan, markdown, and artifact_id. "
        "Does not approve — call plan-mode__approve_plan after the user "
        "approves."
    ),
    name="write_plan",
    schema=WritePlanParams,
    observation_formatter=_fmt,
    category="planning",
    cost_class="low",
    keywords=["plan", "write", "todos", "planning", "draft"],
)
def write_plan(params: Dict[str, Any]) -> Dict[str, Any]:
    """Persist a full plan document as draft for the current conversation."""
    parsed = WritePlanParams(**(params or {}))
    try:
        ctx = get_motet_context()
    except Exception:
        ctx = None

    try:
        todos = [PlanTodo(**t) for t in parsed.todos]
    except Exception as exc:
        return {"status": "error", "error": f"Invalid todos: {exc}"}

    if not todos:
        return {"status": "error", "error": "todos must be a non-empty list"}

    plan = PlanDocument(
        goal=parsed.goal,
        summary=parsed.summary,
        todos=todos,
        files=list(parsed.files or []),
        acceptance=list(parsed.acceptance or []),
        open_questions=list(parsed.open_questions or []),
        approval_status="draft",
        updated_at=utc_now_iso(),
    )
    plan, key, artifact_id = save_plan_with_artifact(
        ctx, plan, snapshot_reason="write"
    )
    return plan_response(
        plan,
        status="draft",
        storage_key=key,
        artifact_id=artifact_id,
    )
