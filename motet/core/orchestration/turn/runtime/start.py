"""
Motet - Turn Runtime Start and Budget Continue

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-27

Description:
    Fresh-turn and budget-Continue entry for Turn Runtime.
    ``start`` runs the in-process agentic loop on the caller's MotetContext so
    ``agent_turn`` does not ``motet.do(agent_loop)`` and park a second worker
    slot for the whole turn. ``continue_after_budget`` rehydrates a
    ``budget_continue`` checkpoint with a fresh budget, then calls ``start``.

    Resume stays in ``resume.py``. The Celery ``agent_loop`` command remains
    for ``core.spawn_agents`` children that need overlapping slots, and for
    the OpenAI-compat ``hosted_tools`` hop.

Dependencies:
    - loop_driver.run_agentic_loop: in-process ReAct driver (lazy import)
    - turn_result_from_loop_payload: typed TurnResult wrapper
    - budget_continue.try_build_budget_continue_loop_data: fresh-budget rehydrate

Usage:
    from motet.core.orchestration.turn.runtime import start, continue_after_budget

    result = start(motet, loop_data)
    payload = result.payload

    continued = continue_after_budget(
        motet,
        history=history,
        stream_key=motet.stream_key,
        max_iterations=20,
    )

Notes:
    - Lazy-import the loop driver so this module can load from runtime/__init__
      without a cycle (loop_driver lazy-imports persist helpers).
    - agent_turn and resume_agent_turn branch on TurnResult.kind; extract /
      complete helpers still read ``.payload``. hosted_tools dispatches
      ``core.agent_loop``, which calls start.
    - Do not call run_agentic_loop from agent_turn or the facade.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from motet.core.orchestration.turn.runtime.result import TurnResult, turn_result_from_loop_payload


def start(motet: Any, loop_data: Any) -> TurnResult:
    """Run a fresh or rehydrated loop on this worker. Returns a typed TurnResult."""
    from motet.core.reasoning.react.loop_driver import run_agentic_loop

    payload = run_agentic_loop(motet, loop_data)
    return turn_result_from_loop_payload(payload)


def continue_after_budget(
    motet: Any,
    *,
    history: Sequence[Any],
    stream_key: str,
    max_iterations: int,
    max_model_calls: Optional[int] = None,
    input_text: Optional[str] = None,
    model_provider: Optional[str] = None,
    model_name: Optional[str] = None,
    model_profile_name: Optional[str] = None,
    enable_thinking: Optional[bool] = None,
    reasoning_effort: Optional[str] = None,
    tool_filter_metadata: Optional[Dict[str, Any]] = None,
) -> Optional[TurnResult]:
    """
    Rehydrate the latest budget_continue checkpoint and run a new turn.

    Returns None when no snapshot exists so the caller can fall back to a
    steered ``start``. Live routing overrides win over the prior snapshot.
    """
    from motet.core.orchestration.turn.budget_continue import (
        try_build_budget_continue_loop_data,
    )

    loop_data = try_build_budget_continue_loop_data(
        motet,
        history=history,
        stream_key=stream_key,
        max_iterations=max_iterations,
        max_model_calls=max_model_calls,
        input_text=input_text,
    )
    if loop_data is None:
        return None
    if model_provider:
        loop_data.model_provider = model_provider
    if model_name:
        loop_data.model_name = model_name
    if model_profile_name is not None:
        loop_data.model_profile_name = model_profile_name
    if enable_thinking is not None:
        loop_data.enable_thinking = enable_thinking
    if reasoning_effort is not None:
        loop_data.reasoning_effort = reasoning_effort
    if tool_filter_metadata and not loop_data.tool_filter_metadata:
        loop_data.tool_filter_metadata = tool_filter_metadata
    result = start(motet, loop_data)
    return result.model_copy(
        update={"conversation_history": list(loop_data.conversation_history or [])}
    )


__all__ = ["continue_after_budget", "start"]
