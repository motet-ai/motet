"""
Motet SDK - Background Thinker Example: Start Thinking Command

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-21

Description:
Create a recurring or delayed schedule that periodically runs the reflect
command on a given topic.  Demonstrates motet.schedules.create() for both
recurring (interval-based or cron) and delayed (one-shot) scheduling,
plus memory persistence of schedule metadata for later lookup by topic.

Dependencies:
- motet_sdk: command decorator and MotetContext typing
- pydantic: structured command inputs

Usage:
  # Recurring: reflect every 30 minutes
  background-thinker.start_thinking(topic="quantum computing", interval_seconds=1800)

  # Cron: reflect at 9 AM every weekday
  background-thinker.start_thinking(topic="market trends", cron_expression="0 9 * * MON-FRI")

  # Delayed one-shot: reflect once in 2 hours
  background-thinker.start_thinking(topic="retrospective", mode="delayed", delay_seconds=7200)

Notes:
- Schedule metadata is stored in memory with the "background-thinker-schedule"
  tag so stop_thinking can find active schedules by topic.
- The schedule targets background-thinker.reflect, which handles the actual
  LLM reasoning and memory persistence.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, motet


class StartThinkingData(BaseCommandData):
    """Input for background-thinker.start_thinking."""

    topic: str = Field(..., description="Topic to think about in the background")
    mode: str = Field(
        default="recurring",
        description="Schedule mode: 'recurring' for periodic reflection, 'delayed' for one-shot",
    )
    interval_seconds: int = Field(
        default=1800,
        ge=60,
        description="Seconds between reflections (recurring mode, default 30 min)",
    )
    cron_expression: Optional[str] = Field(
        default=None,
        description="Cron expression for recurring mode (overrides interval_seconds if set)",
    )
    delay_seconds: int = Field(
        default=3600,
        ge=60,
        description="Seconds to wait before one-shot reflection (delayed mode, default 1 hour)",
    )
    max_reflections: Optional[int] = Field(
        default=None,
        description="Maximum number of reflection cycles (None for unlimited recurring)",
    )
    provider: str = Field(default="openai", description="LLM provider for reflections")
    model_name: str = Field(default="gpt-4o-mini", description="LLM model for reflections")


@motet.command(timeout_seconds=30)
def start_thinking(data: StartThinkingData, motet: MotetContext) -> Dict[str, Any]:
    """Create a schedule for autonomous background reflection on a topic."""

    schedule_name = f"Background Thinking: {data.topic}"
    target_data = {
        "topic": data.topic,
        "provider": data.provider,
        "model_name": data.model_name,
    }

    schedule_kwargs: Dict[str, Any] = {"name": schedule_name}

    if data.mode == "delayed":
        schedule_kwargs["schedule_type"] = "delayed"
        schedule_kwargs["scheduled_at"] = datetime.now() + timedelta(seconds=data.delay_seconds)
    else:
        schedule_kwargs["schedule_type"] = "recurring"
        if data.cron_expression:
            schedule_kwargs["cron_expression"] = data.cron_expression
        else:
            schedule_kwargs["interval_seconds"] = data.interval_seconds
        if data.max_reflections:
            schedule_kwargs["max_executions"] = data.max_reflections

    result = motet.schedules.create(
        target_command_type="background-thinker.reflect",
        target_command_data=target_data,
        **schedule_kwargs,
    )

    schedule_id = result.get("schedule_id") if isinstance(result, dict) else str(result)

    # Persist schedule metadata in memory so stop_thinking can find it by topic.
    try:
        motet.memory.store(
            content=f"Active background thinking schedule for topic: {data.topic}",
            type="schedule_tracking",
            tags=["background-thinker-schedule", "background-thinker"],
            metadata={
                "topic": data.topic,
                "schedule_id": schedule_id,
                "mode": data.mode,
                "bundle": "background-thinker",
            },
            scope_type="principal",
        )
    except Exception:
        pass

    return {
        "schedule_id": schedule_id,
        "topic": data.topic,
        "mode": data.mode,
        "schedule_name": schedule_name,
        "status": "created",
    }
