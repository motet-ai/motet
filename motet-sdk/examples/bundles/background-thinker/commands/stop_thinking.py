"""
Motet SDK - Background Thinker Example: Stop Thinking Command

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-03

Description:
Manage the lifecycle of background thinking schedules: cancel, suspend,
or resume an active schedule by topic or direct schedule ID. Looks up
schedule IDs from principal-scoped memory (stored by start_thinking) and
delegates the lifecycle action to the core.manage_schedule built-in tool.

Dependencies:
- motet_sdk: command decorator and MotetContext typing
- pydantic: structured command inputs
- core.manage_schedule: built-in tool performing the lifecycle action

Usage:
  # Cancel by topic (looks up schedule ID from memory)
  background-thinker.stop_thinking(topic="quantum computing", action="cancel")

  # Suspend by schedule ID
  background-thinker.stop_thinking(schedule_id="sch_abc123", action="suspend")

  # Resume a suspended schedule
  background-thinker.stop_thinking(schedule_id="sch_abc123", action="resume")

Notes:
- The "cancel" action permanently removes the schedule.
- The "suspend" action pauses it; "resume" reactivates it.
- When managing by topic, this command recalls the schedule_id from
  principal-scoped memory stored by start_thinking / start_thinking_tool.
- core.manage_schedule enforces tenant/principal ownership; schedules must be
  created with the caller's identity (see motet.schedules.create).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, motet

from . import _memory as mem


class StopThinkingData(BaseCommandData):
    """Input for background-thinker.stop_thinking."""

    topic: Optional[str] = Field(
        default=None,
        description="Topic to stop thinking about (looks up schedule from memory)",
    )
    schedule_id: Optional[str] = Field(
        default=None,
        description="Direct schedule ID to manage (takes precedence over topic lookup)",
    )
    action: str = Field(
        default="cancel",
        description="Lifecycle action: 'cancel', 'suspend', or 'resume'",
    )


@motet.command(timeout_seconds=30)
def stop_thinking(data: StopThinkingData, motet: MotetContext) -> Dict[str, Any]:
    """Cancel, suspend, or resume a background thinking schedule."""

    sid = data.schedule_id
    if not sid and data.topic:
        sid = mem.find_schedule_id(motet, data.topic)

    if not sid:
        return {
            "status": "not_found",
            "message": "No schedule found. Provide a schedule_id or a topic with an active schedule.",
            "topic": data.topic,
            "action": data.action,
        }

    try:
        result = motet.tools.execute(
            "core.manage_schedule",
            {"schedule_id": sid, "action": data.action},
        )
        # manage_schedule reports failures in its payload rather than raising,
        # so mirror its status instead of always claiming success.
        failed = isinstance(result, dict) and (
            result.get("status") == "error" or result.get("error")
        )
        return {
            "status": "error" if failed else "success",
            "schedule_id": sid,
            "action": data.action,
            "topic": data.topic,
            "result": result,
            "error": result.get("error") if isinstance(result, dict) else None,
        }
    except Exception as exc:
        return {
            "status": "error",
            "schedule_id": sid,
            "action": data.action,
            "topic": data.topic,
            "error": str(exc),
        }
