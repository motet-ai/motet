"""
Motet SDK - Plan Mode Example: approve_plan Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-05

Description:
    Human approval gate for plan-mode (#173B): transition the conversation
    plan from draft (or rejected) to approved so build-phase todo updates
    can proceed. Call only after the user explicitly approves the plan
    (or asked to plan and implement without pausing). Mirrors app-builder's
    plan-pending → plan-approved gate without GitHub labels. Dual-writes a
    markdown artifact snapshot (approved/rejected) for later read/search.

Dependencies:
    - motet_sdk: @motet.tool, get_motet_context
    - plan_store: apply_approval / load_plan / save_plan_with_artifact

Usage:
    plan-mode.approve_plan()
    plan-mode.approve_plan(decision="rejected")
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field

from motet_sdk import get_motet_context, motet

from ._plan_store import (
    ApprovalStatus,
    apply_approval,
    load_plan,
    plan_response,
    save_plan_with_artifact,
)


class ApprovePlanParams(BaseModel):
    """Input for approve_plan."""

    decision: Literal["approved", "rejected"] = Field(
        default="approved",
        description=(
            "approved = unlock build; rejected = block build until rewrite. "
            "Default approved."
        ),
    )
    notes: Optional[str] = Field(
        default=None,
        description="Optional short note recorded in the tool result only",
    )


def _fmt(res: Dict[str, Any]) -> str:
    return (
        f"approve_plan(decision={res.get('decision')!r}, "
        f"approval_status={res.get('approval_status')!r})"
    )


@motet.tool(
    description=(
        "Approve or reject the current conversation plan. Call "
        "decision='approved' only after the user explicitly approves "
        "(or said to plan and implement without pausing). Rejected plans "
        "block update_todo until write_plan/update_plan revises them. "
        "Accepts optional 'decision' (approved|rejected, default approved) "
        "and optional 'notes'."
    ),
    name="approve_plan",
    schema=ApprovePlanParams,
    observation_formatter=_fmt,
    category="planning",
    cost_class="low",
    keywords=["plan", "approve", "reject", "gate", "approval"],
)
def approve_plan(params: Dict[str, Any]) -> Dict[str, Any]:
    """Set plan approval_status from an explicit user decision."""
    parsed = ApprovePlanParams(**(params or {}))
    try:
        ctx = get_motet_context()
    except Exception:
        ctx = None

    plan = load_plan(ctx)
    if plan is None:
        return {
            "status": "error",
            "error": "No plan stored for this conversation",
            "decision": parsed.decision,
        }

    if parsed.decision == "approved" and not plan.todos:
        return {
            "status": "error",
            "error": "Cannot approve a plan with no todos",
            "decision": parsed.decision,
            "approval_status": plan.approval_status,
        }

    new_status: ApprovalStatus = parsed.decision  # type: ignore[assignment]
    try:
        updated = apply_approval(plan, new_status)
    except ValueError as exc:
        return {"status": "error", "error": str(exc), "decision": parsed.decision}

    snapshot_reason = "approve" if parsed.decision == "approved" else "reject"
    updated, key, artifact_id = save_plan_with_artifact(
        ctx, updated, snapshot_reason=snapshot_reason
    )
    out = plan_response(
        updated,
        status=parsed.decision,
        decision=parsed.decision,
        storage_key=key,
        artifact_id=artifact_id,
    )
    if parsed.notes:
        out["notes"] = parsed.notes
    return out
