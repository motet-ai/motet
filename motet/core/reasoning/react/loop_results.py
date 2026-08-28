"""
Motet - Agentic Loop Result / Usage Assembly

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Terminal-result and usage-accumulation helpers for the agentic loop (issue
    #147). Every path that ends an agentic turn — budget stop, stall stop, fast
    path, suspension, normal completion — returns the same dict shape, and every
    model call folds its usage and its cost into one running total.
    Those two concerns live here so that both the iteration conductor
    (agentic_loop) and the execution phases (loop_execution) can reach them
    without importing each other.

    This module exists to break an import cycle. agentic_loop imports
    loop_execution at module scope; loop_execution needs the result builder.
    loop_results has no intra-package imports, so both modules can depend on
    it at module scope without a cycle.

Dependencies:
    - typing only: this module is deliberately a leaf. Do not add imports from
      sibling react modules, or the cycle it exists to break comes back.

Usage:
    from motet.core.reasoning.react.loop_results import (
        accumulate_usage,
        build_loop_result,
    )

    accumulate_usage(accumulated_usage, stream_data)
    return build_loop_result(
        "done", tool_results, iterations_used, "stop", accumulated_usage,
    )

Notes:
    -     ``build_loop_result`` is the single writer of the loop's terminal contract
      (final_response / tool_results / iterations_used / stop_reason / usage,
      plus optional media and ``finalized``). Callers that hand-roll that dict
      will drift from the loop's other exit paths.
    - ``accumulate_usage`` mutates its first argument in place and assumes every
      token counter key is already present; agentic_loop seeds the zeroed dict at
      turn start. ``cost_usd`` is the exception: it is created on first priced
      model call so its absence stays distinguishable from a genuine zero.
    - The accumulator therefore holds a float alongside the int counters, which is
      why ``usage_accumulator`` is typed ``Dict[str, Any]`` on AgenticLoopData,
      LoopStateSnapshot, and TurnCheckpoint — a ``Dict[str, int]`` there rejects
      the cost on suspension.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _with_usage(result: Dict[str, Any], accumulated_usage: Dict[str, Any]) -> Dict[str, Any]:
    # cost_usd rides in the accumulator so every model call folds into one running
    # total, but it is surfaced top-level: `usage` is the token envelope that the
    # UI and the OpenAI-compat facade read, and a dollar amount inside it would be
    # mistaken for a token count.
    usage = dict(accumulated_usage or {})
    cost_usd = usage.pop("cost_usd", None)
    result["usage"] = usage
    if cost_usd is not None:
        result["cost_usd"] = float(cost_usd)
    return result


def build_loop_result(
    final_response: str,
    tool_results: List[Dict[str, Any]],
    iterations_used: int,
    stop_reason: str,
    accumulated_usage: Dict[str, Any],
    *,
    media: Optional[List[Dict[str, Any]]] = None,
    finalized: bool = False,
) -> Dict[str, Any]:
    """Build standard loop return dict and attach usage (DRY).

    ``media`` (ADR-0113): artifact-backed media parts accumulated across iterations.
    Surfaced as a top-level ``media`` list so the terminal loop result carries images
    generated in earlier iterations even when its own ``tool_results`` is empty.

    ``finalized``: the loop asked the model for a tools-off write-up after a
    rail stop, and got text. The stop_reason stays the rail so parent
    Continue still works; spawn_agents treats this as an answer, not scaffolding.
    """
    payload = {
        "final_response": final_response,
        "tool_results": tool_results,
        "iterations_used": iterations_used,
        "stop_reason": stop_reason,
    }
    if media:
        payload["media"] = media
    if finalized:
        payload["finalized"] = True
    return _with_usage(payload, accumulated_usage)


def accumulate_usage(accumulated_usage: Dict[str, Any], usage_data: Dict[str, Any]) -> None:
    accumulated_usage["prompt_tokens"] += int(usage_data.get("prompt_tokens") or 0)
    accumulated_usage["completion_tokens"] += int(usage_data.get("completion_tokens") or 0)
    accumulated_usage["total_tokens"] += int(usage_data.get("total_tokens") or 0)
    accumulated_usage["cache_read_tokens"] += int(usage_data.get("cache_read_tokens") or 0)
    accumulated_usage["cache_creation_tokens"] += int(usage_data.get("cache_creation_tokens") or 0)
    accumulated_usage["reasoning_tokens"] += int(usage_data.get("reasoning_tokens") or 0)
    accumulated_usage["tool_time_ms"] += int(usage_data.get("tool_time_ms") or 0)

    # ADR-0018 cost, from the model command that priced this call. The key is
    # created on first sighting rather than seeded at zero: when no model call
    # reports a cost (tracking disabled or failed), the turn must surface no cost
    # at all, not a $0.00 that reads as free.
    call_cost = usage_data.get("cost_usd")
    if isinstance(call_cost, (int, float)):
        accumulated_usage["cost_usd"] = float(
            accumulated_usage.get("cost_usd") or 0.0
        ) + float(call_cost)
