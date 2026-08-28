"""
Motet - Agentic Loop Driver

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    In-process driver for the agentic ReAct loop. ``agentic_loop`` is
    one iteration of glue (model call, tools, observation) and is **not** a
    Celery command. ``run_agentic_loop`` calls that body on the current worker
    until a terminal result. Model inference, tool execution, and workflows
    remain distributed via ``motet.do``.

    Iterations are strictly serial, so dispatching each round as its own command
    did not overlap work; it copied conversation history through the result
    backend and parked an extra greenlet. Task-flow grouping is stream events
    plus ``agentic_loop_iteration`` stamped on child command metadata
    (model/tools/workflows), not a command node per round.

    Cancel: honor still gates every child ``motet.do``. Loop-head
    ``is_cancelled`` lives in Turn Runtime. This driver also
    materializes loop intents (handback / nested workflow / budget stop) so
    checkpoint writes stay in ``orchestration/turn/runtime``.

    Suspend is a normal terminal result (``suspended: True``).
    Callers that run a whole turn use ``runtime.start`` (this driver is the
    loop body behind that call).

Dependencies:
    - AgenticLoopData: in-process continuation payload
    - agentic_loop: one iteration (same MotetContext as the parent agent)
    - turn.runtime: loop-head cancel + intent materialize
    - structlog: driver / error logs

Usage:
    from motet.core.reasoning.react.loop_driver import run_agentic_loop

    return run_agentic_loop(motet, loop_data)

Notes:
    - Not a MotetContext primitive. Only the agentic loop uses this driver.
    - Continuation is an in-memory ``AgenticLoopData`` (no Celery result hop).
    - ``stamp_agentic_loop_iteration`` writes the 1-based round onto
      ``motet.metadata`` so child ``motet.do`` inherits it for cmd:meta.
    - Rebinds MotetContext at the start of each iteration, then restores the
      prior MotetContext (or clears it) when the driver returns.
    - ``agentic_loop`` must not import this module at module scope (cycle).
"""

from __future__ import annotations

from typing import Any, Dict

import structlog

from motet.core.commands.distributed_types import (
    AGENTIC_LOOP_ITERATION_META_KEY,
    parse_agentic_loop_iteration,
)
from .agentic_loop_data import AgenticLoopData

logger = structlog.get_logger(__name__)

AGENTIC_LOOP_CONTINUE_KEY = "__agentic_loop_continue__"


def stamp_agentic_loop_iteration(motet: Any, iteration: int) -> None:
    """Copy the current loop round onto ``motet.metadata`` so child ``motet.do`` inherits it."""
    n = parse_agentic_loop_iteration(iteration)
    if n is None:
        return
    meta = getattr(motet, "metadata", None)
    if meta is None:
        return
    try:
        meta[AGENTIC_LOOP_ITERATION_META_KEY] = n
    except Exception:
        logger.warning(
            "agentic_loop_iteration_stamp_failed",
            iteration=n,
            exc_info=True,
        )


def agentic_loop_continue(next_data: AgenticLoopData) -> Dict[str, Any]:
    """Ask the driver to run another in-process iteration with ``next_data``."""
    return {AGENTIC_LOOP_CONTINUE_KEY: next_data}


def is_agentic_loop_continue(result: Any) -> bool:
    return isinstance(result, dict) and AGENTIC_LOOP_CONTINUE_KEY in result


def _next_loop_data(result: Dict[str, Any]) -> AgenticLoopData:
    payload = result.get(AGENTIC_LOOP_CONTINUE_KEY)
    if isinstance(payload, AgenticLoopData):
        return payload
    if isinstance(payload, dict):
        return AgenticLoopData.model_validate(payload)
    raise ValueError(
        "agentic_loop continuation payload must be AgenticLoopData or a dict "
        f"(got {type(payload).__name__})"
    )


def _attach_loop_cache(result: Dict[str, Any], data: AgenticLoopData) -> Dict[str, Any]:
    """Surface the loop's snapshot cache on the terminal payload.

    ``core.spawn_agents`` reads these so the parent can inherit child
    ``http_get`` / ``http_get_browser`` / ``web_search`` freshness. Rail
    stops return empty ``tool_results``, so the cache cannot be rebuilt
    from the last batch alone.
    """
    payload = dict(result)
    payload["observation_cache"] = dict(getattr(data, "observation_cache", None) or {})
    payload["executed_signatures"] = list(getattr(data, "executed_signatures", None) or [])
    return payload


def _loop_bound(data: AgenticLoopData) -> int:
    remaining = int(getattr(data, "remaining_iterations", 0) or 0)
    maximum = int(getattr(data, "max_iterations", 0) or 0)
    return max(remaining, maximum, 1) + 2


def run_agentic_loop(motet: Any, data: AgenticLoopData) -> Dict[str, Any]:
    """Run ``agentic_loop`` in-process until a terminal result (or continue bound).

    Each call is one iteration on this worker. Model/tool/workflow children are
    still ``motet.do``. Suspend, budget stop, stall, and errors are terminal.
    Handback / budget-continue checkpoint writes go through Turn Runtime.
    """
    from motet.core.commands.motet_context import (
        _clear_motet_context,
        _set_motet_context,
        get_motet_context,
    )
    from motet.core.commands.response_models import CommandExecutionError
    from motet.core.orchestration.turn.runtime import (
        materialize_intent,
        raise_if_turn_cancelled,
    )
    from motet.core.reasoning.react.loop_intents import is_turn_intent

    from .agentic_loop import agentic_loop

    try:
        previous_motet = get_motet_context()
    except RuntimeError:
        previous_motet = None
    _set_motet_context(motet)
    try:
        current = data
        bound = _loop_bound(current)
        for step in range(bound):
            # Rebind each round: in-process child commands used to clear
            # MotetContext in the decorator finally; even after restore, the
            # driver owns the loop-head context for this iteration.
            _set_motet_context(motet)
            raise_if_turn_cancelled(motet)
            stamp_agentic_loop_iteration(motet, current.current_iteration)
            try:
                result = agentic_loop(current)
            except CommandExecutionError:
                logger.error(
                    "agentic_loop_iteration_failed",
                    step=step,
                    remaining_iterations=getattr(current, "remaining_iterations", None),
                    exc_info=True,
                )
                raise
            except Exception as e:
                logger.error(
                    "agentic_loop_iteration_failed",
                    step=step,
                    remaining_iterations=getattr(current, "remaining_iterations", None),
                    error=str(e),
                    exc_info=True,
                )
                raise

            if is_turn_intent(result):
                result = materialize_intent(motet, current, result)

            if not is_agentic_loop_continue(result):
                if isinstance(result, dict):
                    return _attach_loop_cache(result, current)
                return {"result": result}

            try:
                current = _next_loop_data(result)
            except Exception as e:
                logger.error(
                    "agentic_loop_continue_invalid",
                    step=step,
                    error=str(e),
                    error_type=type(e).__name__,
                    exc_info=True,
                )
                raise

            logger.info(
                "agentic_loop_continue",
                step=step,
                remaining_iterations=getattr(current, "remaining_iterations", None),
            )

        raise RuntimeError(
            f"agentic_loop driver exceeded safety bound ({bound} iterations)"
        )
    finally:
        if previous_motet is None:
            _clear_motet_context()
        else:
            _set_motet_context(previous_motet)
