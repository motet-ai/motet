"""
Motet SDK - Background Thinker Example: Start Thinking Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-21

Description:
LLM-callable tool that creates a recurring background thinking schedule
directly from conversation context.  When the agent decides a topic
deserves ongoing autonomous reflection, it calls this tool to start the
schedule without requiring explicit user API interaction.

Demonstrates @motet.tool combined with get_motet_context().schedules.create()
for creating scheduled commands from a tool context (ADR-0089).

Dependencies:
- motet_sdk: @motet.tool decorator, get_motet_context
- pydantic: tool parameter schema

Usage:
The agent calls this tool proactively during conversation:
  "I'll start thinking about this in the background."
  → background-thinker.start_thinking_tool(
        topic="distributed consensus algorithms",
        interval_minutes=60
    )

Notes:
- Registered as background-thinker.start_thinking_tool via the bundle loader.
- Creates a recurring schedule targeting background-thinker.reflect.
- The tool stores schedule metadata in memory for later lookup by topic.
- For more options (cron, delayed mode, max_reflections), use the
  start_thinking command directly.
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, Field

from motet_sdk import get_motet_context, motet


class StartThinkingToolParams(BaseModel):
    """Input for start_thinking_tool."""

    topic: str = Field(..., description="Topic to think about in the background")
    interval_minutes: int = Field(
        default=30,
        ge=1,
        le=1440,
        description="Minutes between reflection cycles (default 30)",
    )


def _fmt(res: Dict[str, Any]) -> str:
    topic = res.get("topic", "?")
    status = res.get("status", "?")
    return f"start_thinking_tool(topic={topic!r}, status={status})"


@motet.tool(
    description=(
        "Start autonomous background thinking on a topic.  Creates a "
        "recurring schedule that periodically reflects on the topic, building "
        "progressively deeper insights stored in memory.  Use when the user "
        "mentions wanting ongoing analysis or when a topic deserves deeper "
        "thought over time.  Accepts 'topic' (string, required) and "
        "'interval_minutes' (int, default 30)."
    ),
    name="start_thinking_tool",
    schema=StartThinkingToolParams,
    observation_formatter=_fmt,
    category="background-thinking",
    cost_class="low",
    keywords=["think", "schedule", "background", "recurring", "reflect"],
)
def start_thinking_tool(params: Dict[str, Any]) -> Dict[str, Any]:
    """Create a recurring schedule for background reflection on a topic."""
    parsed = StartThinkingToolParams(**(params or {}))

    try:
        ctx = get_motet_context()
    except Exception:
        ctx = None

    if not ctx or not getattr(ctx, "schedules", None):
        return {
            "topic": parsed.topic,
            "status": "error",
            "error": "Scheduling not available in current context.",
        }

    schedule_name = f"Background Thinking: {parsed.topic}"
    target_data = {"topic": parsed.topic}

    try:
        result = ctx.schedules.create(
            target_command_type="background-thinker.reflect",
            target_command_data=target_data,
            schedule_type="recurring",
            interval_seconds=parsed.interval_minutes * 60,
            name=schedule_name,
        )
        schedule_id = result.get("schedule_id") if isinstance(result, dict) else str(result)
    except Exception as exc:
        return {
            "topic": parsed.topic,
            "status": "error",
            "error": f"Failed to create schedule: {exc}",
        }

    # Persist schedule metadata for later lookup by topic.
    if getattr(ctx, "memory", None):
        try:
            ctx.memory.store(
                content=f"Active background thinking schedule for topic: {parsed.topic}",
                type="schedule_tracking",
                tags=["background-thinker-schedule", "background-thinker"],
                metadata={
                    "topic": parsed.topic,
                    "schedule_id": schedule_id,
                    "mode": "recurring",
                    "bundle": "background-thinker",
                },
                scope_type="principal",
            )
        except Exception:
            pass

    return {
        "topic": parsed.topic,
        "schedule_id": schedule_id,
        "schedule_name": schedule_name,
        "interval_minutes": parsed.interval_minutes,
        "status": "created",
    }
