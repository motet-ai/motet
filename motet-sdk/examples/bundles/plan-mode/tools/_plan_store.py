"""
Motet SDK - Plan Mode Example: Plan Document Store

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-05

Description:
    Structured plan document (JSON source of truth), markdown renderer, and
    conversation-scoped persistence for the plan-mode example bundle (#173B).
    Plans start as draft; approve_plan transitions to approved before build.
    Dual-writes markdown snapshots to Motet artifacts on write/update/approve
    so plans remain listable and searchable after the Redis TTL. Pure helpers
    are importable without a live Motet runtime for unit tests.

Dependencies:
    - pydantic: PlanDocument / PlanTodo schema
    - MotetContext.redis (optional): conversation-scoped plan persistence
    - MotetContext.commands / artifact_store (optional): dual-write snapshots

Usage:
    from ._plan_store import PlanDocument, render_markdown, save_plan, load_plan
    from ._plan_store import save_plan_with_artifact

Notes:
    - Redis key: plan-mode:plan:{conversation_id} (7-day TTL) — live SoT.
    - Artifact snapshots: text/markdown, tags plan-mode + plan-{status}.
    - Falls back to a process-local dict when redis is unavailable (tests/dev).
    - Setting a todo to in_progress demotes any other in_progress todos.
    - Content edits (write_plan / update_plan) reset approval_status to draft.
    - update_todo does not snapshot (progress noise); use write/update/approve.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field

TodoStatus = Literal["pending", "in_progress", "completed", "cancelled"]
ApprovalStatus = Literal["draft", "approved", "rejected"]
SnapshotReason = Literal["write", "update", "approve", "reject"]

_PLAN_KEY_PREFIX = "plan-mode:plan:"
_PLAN_TTL_SECONDS = 7 * 24 * 3600
_PLAN_ARTIFACT_TAG = "plan-mode"
_FALLBACK_STORE: Dict[str, str] = {}


class PlanTodo(BaseModel):
    """One actionable item in a plan."""

    id: str = Field(..., description="Stable todo id (e.g. t1)")
    title: str = Field(..., description="Short todo title")
    status: TodoStatus = Field(default="pending", description="Todo status")
    notes: str = Field(default="", description="Optional progress notes")


class PlanDocument(BaseModel):
    """Structured plan stored as JSON; markdown is derived for humans."""

    version: int = Field(default=1, description="Schema version")
    goal: str = Field(default="", description="Original user goal")
    summary: str = Field(default="", description="One-paragraph plan summary")
    todos: List[PlanTodo] = Field(default_factory=list, description="Ordered todos")
    files: List[str] = Field(default_factory=list, description="Paths expected to change")
    acceptance: List[str] = Field(
        default_factory=list,
        description="Overall acceptance checks",
    )
    open_questions: List[str] = Field(
        default_factory=list,
        description="Unresolved design questions",
    )
    approval_status: ApprovalStatus = Field(
        default="draft",
        description="draft | approved | rejected — build requires approved",
    )
    latest_artifact_id: str = Field(
        default="",
        description="Most recent dual-write plan markdown artifact id (if any)",
    )
    updated_at: str = Field(
        default="",
        description="ISO-8601 UTC timestamp of last write",
    )


def utc_now_iso() -> str:
    """Return current UTC time as ISO-8601."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def render_markdown(plan: PlanDocument) -> str:
    """Render a human-readable markdown view of the plan."""
    lines: List[str] = ["# Plan", ""]
    status_label = {
        "draft": "draft — awaiting approval",
        "approved": "approved — ready to build",
        "rejected": "rejected — revise or rewrite",
    }.get(plan.approval_status, plan.approval_status)
    lines.extend([f"**Approval:** {status_label}", ""])
    if plan.goal:
        lines.extend([f"**Goal:** {plan.goal}", ""])
    if plan.summary:
        lines.extend(["## Summary", "", plan.summary, ""])

    lines.extend(["## Todos", ""])
    if not plan.todos:
        lines.append("_(no todos)_")
        lines.append("")
    else:
        for todo in plan.todos:
            if todo.status == "completed":
                box = "[x]"
            elif todo.status == "cancelled":
                box = "[-]"
            elif todo.status == "in_progress":
                box = "[~]"
            else:
                box = "[ ]"
            suffix = f" ({todo.status})"
            note = f" — {todo.notes}" if todo.notes else ""
            lines.append(f"- {box} `{todo.id}` {todo.title}{suffix}{note}")
        lines.append("")

    if plan.files:
        lines.extend(["## Files", ""])
        for path in plan.files:
            lines.append(f"- `{path}`")
        lines.append("")

    if plan.acceptance:
        lines.extend(["## Acceptance", ""])
        for item in plan.acceptance:
            lines.append(f"- {item}")
        lines.append("")

    if plan.open_questions:
        lines.extend(["## Open questions", ""])
        for item in plan.open_questions:
            lines.append(f"- {item}")
        lines.append("")

    if plan.updated_at:
        lines.append(f"_Updated: {plan.updated_at}_")

    return "\n".join(lines).rstrip() + "\n"


def apply_update_todo(
    plan: PlanDocument,
    todo_id: str,
    *,
    status: Optional[TodoStatus] = None,
    notes: Optional[str] = None,
) -> PlanDocument:
    """Patch one todo; demote other in_progress items when promoting one."""
    found = False
    new_todos: List[PlanTodo] = []
    for todo in plan.todos:
        if todo.id != todo_id:
            new_todos.append(todo)
            continue
        found = True
        data = todo.model_dump()
        if status is not None:
            data["status"] = status
        if notes is not None:
            data["notes"] = notes
        new_todos.append(PlanTodo(**data))
    if not found:
        raise KeyError(f"Todo not found: {todo_id}")

    if status == "in_progress":
        demoted: List[PlanTodo] = []
        for todo in new_todos:
            if todo.id != todo_id and todo.status == "in_progress":
                demoted.append(todo.model_copy(update={"status": "pending"}))
            else:
                demoted.append(todo)
        new_todos = demoted

    return plan.model_copy(
        update={"todos": new_todos, "updated_at": utc_now_iso()},
    )


def apply_update_plan(
    plan: PlanDocument,
    *,
    summary: Optional[str] = None,
    files: Optional[List[str]] = None,
    acceptance: Optional[List[str]] = None,
    open_questions: Optional[List[str]] = None,
    todos: Optional[List[Dict[str, Any]]] = None,
    goal: Optional[str] = None,
) -> PlanDocument:
    """Patch plan-level fields and/or replace the full todo list.

    Content changes reset approval_status to draft so the human gate re-applies.
    """
    updates: Dict[str, Any] = {
        "updated_at": utc_now_iso(),
        "approval_status": "draft",
    }
    if goal is not None:
        updates["goal"] = goal
    if summary is not None:
        updates["summary"] = summary
    if files is not None:
        updates["files"] = list(files)
    if acceptance is not None:
        updates["acceptance"] = list(acceptance)
    if open_questions is not None:
        updates["open_questions"] = list(open_questions)
    if todos is not None:
        updates["todos"] = [PlanTodo(**t) if isinstance(t, dict) else t for t in todos]
    return plan.model_copy(update=updates)


def apply_approval(
    plan: PlanDocument,
    approval_status: ApprovalStatus,
) -> PlanDocument:
    """Set approval_status (draft | approved | rejected)."""
    if approval_status not in ("draft", "approved", "rejected"):
        raise ValueError(f"Invalid approval_status: {approval_status}")
    return plan.model_copy(
        update={
            "approval_status": approval_status,
            "updated_at": utc_now_iso(),
        },
    )


def require_approved_for_build(plan: PlanDocument) -> Optional[str]:
    """Return an error message if the plan is not approved for build work."""
    if plan.approval_status == "approved":
        return None
    if plan.approval_status == "rejected":
        return (
            "Plan is rejected. Call plan-mode__write_plan or "
            "plan-mode__update_plan to revise, then plan-mode__approve_plan "
            "after the user approves."
        )
    return (
        "Plan is still draft (awaiting approval). Ask the user to approve, "
        "then call plan-mode__approve_plan before updating todos / building."
    )


def conversation_key(conversation_id: str) -> str:
    """Redis / fallback key for a conversation's plan."""
    cid = (conversation_id or "").strip() or "default"
    return f"{_PLAN_KEY_PREFIX}{cid}"


def resolve_conversation_id(ctx: Any) -> str:
    """Best-effort conversation id from MotetContext."""
    if ctx is None:
        return "default"
    try:
        if hasattr(ctx, "resolve_conversation_id"):
            cid = ctx.resolve_conversation_id()
            if cid:
                return str(cid)
    except Exception:
        pass
    cid = getattr(ctx, "conversation_id", None)
    return str(cid).strip() if cid else "default"


def _plan_artifact_tags(plan: PlanDocument, reason: str) -> List[str]:
    """Tags for dual-write snapshots (RAG + list filters)."""
    tags = [_PLAN_ARTIFACT_TAG, f"plan-{plan.approval_status}"]
    reason_s = (reason or "").strip()
    if reason_s:
        tags.append(f"plan-snapshot-{reason_s}")
    return tags


def snapshot_plan_artifact(
    ctx: Any,
    plan: PlanDocument,
    *,
    reason: SnapshotReason | str,
) -> Optional[str]:
    """Best-effort dual-write of plan markdown to a new artifact.

    Prefers ``core.create_artifact`` (derivations + indexing). Falls back to
    ``artifact_store.put``. Returns artifact_id or None; never raises.
    """
    if ctx is None:
        return None

    conversation_id = resolve_conversation_id(ctx)
    markdown = render_markdown(plan)
    payload = markdown.encode("utf-8")
    safe_cid = conversation_id.replace("/", "_")[:64] or "default"
    filename = f"plan-{safe_cid}-{plan.approval_status}.md"
    tags = _plan_artifact_tags(plan, str(reason))
    metadata: Dict[str, Any] = {
        "filename": filename,
        "original_filename": filename,
        "conversation_id": conversation_id,
        "artifact_tags": tags,
        "tags": tags,
        "source": "plan-mode",
        "plan_snapshot_reason": str(reason),
        "plan_approval_status": plan.approval_status,
        "plan_goal": (plan.goal or "")[:200],
    }

    commands = getattr(ctx, "commands", None)
    if commands is not None and hasattr(commands, "run"):
        try:
            result = commands.run(
                "core.create_artifact",
                data={
                    "payload": payload,
                    "content_type": "text/markdown",
                    "kind": "tool_artifact",
                    "filename": filename,
                    "conversation_id": conversation_id,
                    "metadata": metadata,
                    "trigger_derivations": True,
                },
            )
            if isinstance(result, dict):
                artifact_id = (
                    result.get("artifact_id")
                    or result.get("id")
                    or result.get("source_artifact_id")
                )
                if artifact_id:
                    return str(artifact_id)
        except Exception:
            pass

    store = getattr(ctx, "artifact_store", None)
    if store is None or not hasattr(store, "put"):
        return None
    try:
        artifact_id = store.put(
            payload=payload,
            content_type="text/markdown",
            kind="tool_artifact",
            metadata=metadata,
        )
        return str(artifact_id) if artifact_id else None
    except Exception:
        return None


def save_plan(ctx: Any, plan: PlanDocument) -> str:
    """Persist plan for the conversation; returns storage key."""
    if not plan.updated_at:
        plan = plan.model_copy(update={"updated_at": utc_now_iso()})
    key = conversation_key(resolve_conversation_id(ctx))
    payload = plan.model_dump_json()
    redis = getattr(ctx, "redis", None) if ctx is not None else None
    if redis is not None:
        try:
            redis.set(key, payload, ex=_PLAN_TTL_SECONDS)
            return key
        except Exception:
            pass
    _FALLBACK_STORE[key] = payload
    return key


def save_plan_with_artifact(
    ctx: Any,
    plan: PlanDocument,
    *,
    snapshot_reason: SnapshotReason | str,
) -> Tuple[PlanDocument, str, Optional[str]]:
    """Persist Redis SoT and dual-write a markdown artifact snapshot.

    Returns (plan_with_latest_artifact_id, storage_key, artifact_id_or_none).
    Artifact failure never blocks the Redis write.
    """
    artifact_id = snapshot_plan_artifact(ctx, plan, reason=snapshot_reason)
    if artifact_id:
        plan = plan.model_copy(update={"latest_artifact_id": artifact_id})
    key = save_plan(ctx, plan)
    return plan, key, artifact_id


def load_plan(ctx: Any) -> Optional[PlanDocument]:
    """Load plan for the conversation, or None if missing."""
    key = conversation_key(resolve_conversation_id(ctx))
    raw: Any = None
    redis = getattr(ctx, "redis", None) if ctx is not None else None
    if redis is not None:
        try:
            raw = redis.get(key)
        except Exception:
            raw = None
    if raw is None:
        raw = _FALLBACK_STORE.get(key)
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, dict):
        data = dict(raw)
    else:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if not isinstance(data, dict):
        return None
    # Backward compatible: plans written before approval_status / artifact id.
    data.setdefault("approval_status", "draft")
    data.setdefault("latest_artifact_id", "")
    return PlanDocument(**data)


def clear_fallback_store() -> None:
    """Clear process-local fallback (tests)."""
    _FALLBACK_STORE.clear()


def plan_response(plan: PlanDocument, **extra: Any) -> Dict[str, Any]:
    """Standard tool payload: structured plan + markdown."""
    out: Dict[str, Any] = {
        "plan": plan.model_dump(),
        "markdown": render_markdown(plan),
        "todo_count": len(plan.todos),
        "approval_status": plan.approval_status,
        "latest_artifact_id": plan.latest_artifact_id or None,
    }
    out.update(extra)
    return out
