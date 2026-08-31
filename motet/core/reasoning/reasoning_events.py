"""
Motet - Reasoning Step Events

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-29

Description:
    Shared helper to emit reasoning_step events for UI display and observability.
    Used by the agent loop and by ``core.spawn_agents`` (a tool, not a
    strategy) so both publish the same ``reasoning_step`` shape to EventBus
    and the task/trace Redis stream.

Dependencies:
    - structlog: Structured logging
    - MotetContext: publish_event, stream_event

Usage:
    from motet.core.reasoning.reasoning_events import emit_reasoning_event

    emit_reasoning_event(
        motet,
        strategy="agentic_loop",
        step=1,
        thought="Starting agentic loop iteration 1",
        stream_key=stream_key,
    )

Notes:
    - Events are published to EventBus (system-wide) and optionally to Redis stream.
    - ``strategy`` is the executor label. The only remaining executor is
      ``agentic_loop``. The tool name goes on ``action``.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)


def emit_reasoning_event(
    motet: Any,
    strategy: str,
    step: int,
    thought: str,
    action: Optional[str] = None,
    observation: Optional[str] = None,
    stream_key: Optional[str] = None,
    goal: Optional[str] = None,
    spawn_children: Optional[list] = None,
) -> None:
    """
    Emit a reasoning step event for UI display and observability (ADR-0050).

    Publishes to both EventBus (system-wide observability) and, when stream_key
    is set, to the Redis task/trace stream for frontend display.

    Args:
        motet: MotetContext instance (task_id, publish_event, stream_event).
        strategy: Executor label. Always ``agentic_loop``; not a tool name.
        step: Step or iteration number.
        thought: Description of what's happening (thinking phase).
        action: Optional action being taken (e.g., tool execution).
        observation: Optional result of the action (truncated to 200 chars when streamed).
        stream_key: Optional Redis stream key for task/trace-level streaming.
        goal: Optional user goal/query (e.g. data.query) for UI and telemetry.
    """
    try:
        task_id = getattr(motet, "task_id", None) or ""
        trace_id = getattr(motet, "task_id", None) or task_id
        obs_truncated = (observation[:200] + "...") if observation and len(observation) > 200 else observation

        event_data: dict[str, Any] = {
            "kind": "reasoning_step",
            "task_id": task_id,
            "trace_id": trace_id,
            "source": "reasoning",
            "strategy": strategy,
            "step": step,
            "thought": thought,
            "action": action,
            "observation": obs_truncated,
        }
        if goal is not None and goal:
            event_data["goal"] = goal[:500] + "..." if len(goal) > 500 else goal
        if spawn_children:
            event_data["spawn_children"] = spawn_children

        # Publish to EventBus for system-wide observability
        motet.publish_event(event_data)

        # Stream to task/trace stream for frontend display when key is provided
        if stream_key:
            try:
                motet.stream_event(
                    "reasoning_step",
                    stream_key=stream_key,
                    data=json.dumps(event_data),
                )
            except Exception as stream_err:
                logger.debug(
                    "reasoning_event_stream_failed",
                    strategy=strategy,
                    step=step,
                    error=str(stream_err),
                )
    except Exception as e:
        logger.warning(
            "reasoning_event_emission_failed",
            strategy=strategy,
            step=step,
            error=str(e),
        )
