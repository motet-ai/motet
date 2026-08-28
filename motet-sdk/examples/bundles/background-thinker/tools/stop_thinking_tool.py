"""
Motet SDK - Background Thinker Example: Stop Thinking Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-03

Description:
LLM-callable counterpart to start_thinking_tool: cancels, suspends, or
resumes a background thinking schedule from conversation context.  The
agent can only invoke tools, so this wrapper makes the schedule lifecycle
reachable from chat (the stop_thinking command remains available for API
and CLI callers).

Demonstrates @motet.tool combined with get_motet_context() for principal-scoped
memory lookup and built-in tool composition via ctx.tools.execute (ADR-0089).

Dependencies:
- motet_sdk: @motet.tool decorator, get_motet_context
- pydantic: tool parameter schema
- core.manage_schedule: built-in tool performing the lifecycle action

Usage:
The agent calls this tool when the user wants thinking to stop:
  "Stop thinking about distributed consensus."
  → background-thinker.stop_thinking_tool(
        topic="distributed consensus",
        action="cancel"
    )

Notes:
- Registered as background-thinker.stop_thinking_tool via the bundle loader.
- Resolves schedule_id from the principal-scoped schedule_tracking memory
  written by start_thinking / start_thinking_tool, unless one is supplied.
- core.manage_schedule enforces tenant/principal ownership, so schedules must
  be created with the caller's identity (see motet.schedules.create).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from motet_sdk import get_motet_context, motet


class StopThinkingToolParams(BaseModel):
    """Input for stop_thinking_tool."""

    topic: str = Field(..., description="Topic whose background thinking should change state")
    action: str = Field(
        default="cancel",
        description="Lifecycle action: cancel, suspend, or resume",
    )
    schedule_id: Optional[str] = Field(
        default=None,
        description="Schedule ID to act on directly, skipping topic lookup",
    )


def _fmt(res: Dict[str, Any]) -> str:
    topic = res.get("topic", "?")
    action = res.get("action", "?")
    status = res.get("status", "?")
    return f"stop_thinking_tool(topic={topic!r}, action={action}, status={status})"


def _as_dict(item: Any) -> Dict[str, Any]:
    if hasattr(item, "model_dump"):
        dumped = item.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    return item if isinstance(item, dict) else {}


def _find_schedule_id(ctx: Any, topic: str) -> Optional[str]:
    """Resolve the schedule_id stored for a topic by start_thinking."""
    memory = getattr(ctx, "memory", None)
    if memory is None:
        return None

    candidates = []
    principal_id = getattr(ctx, "principal_id", None)
    if principal_id and hasattr(memory, "recall_principal"):
        try:
            candidates = list(
                memory.recall_principal(
                    principal_id=principal_id,
                    limit=50,
                    types=["schedule_tracking"],
                    motet_context=ctx,
                )
                or []
            )
        except Exception:
            candidates = []

    if not candidates:
        try:
            candidates = list(
                memory.recall(
                    query=f"background thinking schedule {topic}",
                    tags=["background-thinker-schedule"],
                    limit=10,
                )
                or []
            )
        except Exception:
            return None

    topic_l = topic.lower()
    for item in candidates:
        dumped = _as_dict(item)
        tags = set(dumped.get("tags") or [])
        if "background-thinker-schedule" not in tags and dumped.get("type") != "schedule_tracking":
            continue
        raw_meta = dumped.get("metadata")
        meta: Dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        if str(meta.get("topic") or "").lower() == topic_l:
            sid = meta.get("schedule_id")
            if sid:
                return str(sid)
    return None


@motet.tool(
    description=(
        "Stop, pause, or resume autonomous background thinking on a topic.  "
        "Use when the user asks to stop thinking about something, pause "
        "reflections, or resume paused reflections.  Accepts 'topic' (string, "
        "required), 'action' (cancel, suspend, or resume — default cancel), and "
        "optional 'schedule_id'."
    ),
    name="stop_thinking_tool",
    schema=StopThinkingToolParams,
    observation_formatter=_fmt,
    category="background-thinking",
    cost_class="low",
    keywords=["stop", "cancel", "suspend", "resume", "schedule", "background"],
)
def stop_thinking_tool(params: Dict[str, Any]) -> Dict[str, Any]:
    """Cancel, suspend, or resume a background thinking schedule by topic."""
    parsed = StopThinkingToolParams(**(params or {}))
    action = (parsed.action or "cancel").strip().lower()

    try:
        ctx = get_motet_context()
    except Exception:
        ctx = None

    if not ctx or not getattr(ctx, "tools", None):
        return {
            "topic": parsed.topic,
            "action": action,
            "status": "error",
            "error": "Schedule management not available in current context.",
        }

    sid = parsed.schedule_id or _find_schedule_id(ctx, parsed.topic)
    if not sid:
        return {
            "topic": parsed.topic,
            "action": action,
            "status": "error",
            "error": f"No active background thinking schedule found for topic: {parsed.topic}",
        }

    try:
        result = ctx.tools.execute(
            "core.manage_schedule",
            {"schedule_id": sid, "action": action},
        )
    except Exception as exc:
        return {
            "topic": parsed.topic,
            "action": action,
            "schedule_id": sid,
            "status": "error",
            "error": f"Failed to {action} schedule: {exc}",
        }

    # manage_schedule reports failures in its payload rather than raising.
    failed = isinstance(result, dict) and (result.get("status") == "error" or result.get("error"))
    return {
        "topic": parsed.topic,
        "action": action,
        "schedule_id": sid,
        "status": "error" if failed else "success",
        "result": result,
        "error": result.get("error") if isinstance(result, dict) else None,
    }
