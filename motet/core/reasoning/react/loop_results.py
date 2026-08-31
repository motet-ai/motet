"""
Motet - Agentic Loop Result / Usage Assembly

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-30

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
        emit_usage_event,
        empty_usage_accumulator,
    )

    accumulated_usage = empty_usage_accumulator()
    accumulate_usage(accumulated_usage, stream_data)
    emit_usage_event(motet, accumulated_usage, stream_key=data.stream_key)
    return build_loop_result(
        "done", tool_results, iterations_used, "stop", accumulated_usage,
    )

Notes:
    - ``build_loop_result`` is the single writer of the loop's terminal contract
      (final_response / tool_results / iterations_used / stop_reason / usage,
      plus optional media, ``finalized``, display-only ``thinking_text``,
      display-only ``tool_summaries``, and display-only ``spawn_children``).
      Callers that hand-roll that dict will drift from the loop's other exit paths.
    - ``thinking_text`` is the joined provider reasoning for this loop only. It is
      for conversation GET / Chat Explorer reload. It is not assistant content.
    - ``tool_summaries`` is the short name/status/preview list accumulated
      across this loop's tool rounds. ``core.tool_call`` rows use the
      dispatched tool or workflow name. Optional duration_ms is the tool
      wall time. Spawn persist writes it on the nested transcript row. It is
      not a tool-call / tool-result replay item.
    - ``spawn_children`` is the card-pointer list accumulated from
      ``core.spawn_agents`` envelopes this loop. It is written on the parent
      transcript row and omitted from next-turn model replay.
    - ``accumulate_usage`` mutates its first argument in place and assumes every
      token counter key is already present; agentic_loop seeds the zeroed dict at
      turn start. ``cost_usd`` is the exception: it is created on first priced
      model call so its absence stays distinguishable from a genuine zero.
    - The accumulator therefore holds a float alongside the int counters, which is
      why ``usage_accumulator`` is typed ``Dict[str, Any]`` on AgenticLoopData,
      LoopStateSnapshot, and TurnCheckpoint — a ``Dict[str, int]`` there rejects
      the cost on suspension.
    - ``_peel_usage_cost`` is the one split: tokens stay in ``usage``, dollars
      are top-level. The terminal result keeps a numeric ``0.0``; the chat
      ``usage`` frame only stamps ``cost_usd`` when priced (``> 0``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


_TOOL_SUMMARY_PREVIEW_MAX = 160
_DISPATCH_TOOL_NAME = "core.tool_call"

_USAGE_COUNTER_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "reasoning_tokens",
    "tool_time_ms",
)


def empty_usage_accumulator() -> Dict[str, Any]:
    """Zeroed token/tool-time counters. ``cost_usd`` is added on first priced call."""
    return {key: 0 for key in _USAGE_COUNTER_KEYS}


def _peel_usage_cost(accumulated_usage: Dict[str, Any]) -> tuple[Dict[str, Any], Any]:
    usage = dict(accumulated_usage or {})
    return usage, usage.pop("cost_usd", None)


def _numeric_cost_usd(raw: Any, *, priced_only: bool) -> Optional[float]:
    """Parse a cost. ``priced_only`` drops ``<= 0`` so display never shows free."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        try:
            raw = float(raw)
        except ValueError:
            return None
    if not isinstance(raw, (int, float)):
        return None
    amount = float(raw)
    if priced_only and amount <= 0:
        return None
    return amount


def priced_cost_usd(raw: Any) -> Optional[float]:
    """Priced USD for a ``usage`` frame, or None when unknown (not free)."""
    return _numeric_cost_usd(raw, priced_only=True)


def usage_token_envelope(accumulated_usage: Dict[str, Any]) -> Dict[str, Any]:
    """Token/tool-time envelope with ``cost_usd`` removed."""
    usage, _raw = _peel_usage_cost(accumulated_usage)
    return usage


def _dispatched_target_name(entry: Dict[str, Any], tool_call: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Inner tool/workflow name when the outer call is ``core.tool_call``."""
    outer = str(entry.get("tool_name") or "").strip()
    if outer != _DISPATCH_TOOL_NAME:
        return None
    stamped = entry.get("dispatched_tool_name")
    if isinstance(stamped, str) and stamped.strip() and stamped.strip() != outer:
        return stamped.strip()
    result = entry.get("result")
    if isinstance(result, dict):
        meta = result.get("meta")
        if isinstance(meta, dict):
            inner = meta.get("tool_name")
            if isinstance(inner, str) and inner.strip() and inner.strip() != outer:
                return inner.strip()
        inner = result.get("tool_name")
        if isinstance(inner, str) and inner.strip() and inner.strip() != outer:
            return inner.strip()
    if isinstance(tool_call, dict):
        params = tool_call.get("parameters")
        if not isinstance(params, dict):
            params = tool_call.get("arguments")
        if isinstance(params, dict):
            inner = params.get("tool_name")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return None


def duration_ms_from_payload(payload: Any) -> Optional[int]:
    """Non-negative tool wall time in milliseconds, or None."""
    if not isinstance(payload, dict):
        return None
    raw = payload.get("duration_ms")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int) and raw >= 0:
        return raw
    if isinstance(raw, float) and raw >= 0:
        return int(raw)
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


def _calls_by_id(tool_calls: Any) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(tool_calls, list):
        return out
    for raw in tool_calls:
        if not isinstance(raw, dict):
            continue
        call_id = str(raw.get("tool_call_id") or raw.get("call_id") or "").strip()
        if call_id:
            out[call_id] = raw
    return out


def join_thinking_text(*parts: Any) -> Optional[str]:
    """Join non-empty reasoning strings for display persist. Empty → None."""
    chunks = [part.strip() for part in parts if isinstance(part, str) and part.strip()]
    return "\n\n".join(chunks) if chunks else None


def _preview_text(value: Any) -> Optional[str]:
    """One-line preview for a tool result. Never returns argument blobs."""
    if isinstance(value, dict):
        raw = value.get("preview")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()[:_TOOL_SUMMARY_PREVIEW_MAX]
        err = value.get("error")
        if isinstance(err, str) and err.strip():
            return err.strip()[:_TOOL_SUMMARY_PREVIEW_MAX]
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()[:_TOOL_SUMMARY_PREVIEW_MAX]
    return None


def summarize_tool_results(
    tool_results: Any,
    *,
    step: Optional[int] = None,
    tool_calls: Any = None,
) -> List[Dict[str, Any]]:
    """Short {tool_name, status, preview?, step?} rows from loop tool_results.

    ``core.tool_call`` is recorded under the dispatched tool or workflow name
    so chat reload shows the capability that ran, not the dispatcher.
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(tool_results, list):
        return out
    calls = _calls_by_id(tool_calls)
    for entry in tool_results:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("tool_name") or "").strip()
        if not name:
            continue
        call_id = str(entry.get("tool_call_id") or "").strip()
        inner = _dispatched_target_name(entry, calls.get(call_id))
        if inner:
            name = inner
        status = str(entry.get("status") or "success").strip() or "success"
        row: Dict[str, Any] = {"tool_name": name, "status": status}
        if isinstance(step, int) and step > 0:
            row["step"] = step
        preview = _preview_text(entry.get("result"))
        if not preview and status in {"error", "failed"}:
            preview = _preview_text(entry.get("error") or entry.get("error_message"))
        if preview:
            row["preview"] = preview
        duration_ms = duration_ms_from_payload(entry)
        if duration_ms is None:
            duration_ms = duration_ms_from_payload(entry.get("result"))
        if duration_ms is not None:
            row["duration_ms"] = duration_ms
        out.append(row)
    return out


def extend_tool_summaries(
    summaries: List[Dict[str, Any]],
    tool_results: Any,
    *,
    step: Optional[int] = None,
    tool_calls: Any = None,
) -> None:
    """Append this iteration's short tool rows onto the loop accumulator."""
    summaries.extend(summarize_tool_results(tool_results, step=step, tool_calls=tool_calls))


def extend_spawn_children(
    accumulated: List[Dict[str, Any]],
    payload: Any,
) -> None:
    """Merge spawn card pointers from a spawn_agents envelope onto the loop.

    Early mint pointers and the completed fan-in share ``child_conversation_id``.
    The completed row overlays the early one so thinking, preview, and cost
    survive on the parent transcript.
    """
    if not isinstance(payload, dict):
        return
    raw: Any = None
    meta = payload.get("meta")
    if isinstance(meta, dict):
        raw = meta.get("spawn_children")
    if raw is None:
        inner = payload.get("result")
        if isinstance(inner, dict):
            inner_meta = inner.get("meta")
            if isinstance(inner_meta, dict):
                raw = inner_meta.get("spawn_children")
            if raw is None:
                raw = inner.get("spawn_children")
    if raw is None:
        raw = payload.get("spawn_children")
    if not isinstance(raw, list):
        return
    from motet.core.conversations.transcript_storage import overlay_spawn_child_pointer

    by_cid = {
        str(row.get("child_conversation_id") or "").strip(): index
        for index, row in enumerate(accumulated)
        if isinstance(row, dict) and str(row.get("child_conversation_id") or "").strip()
    }
    for row in raw:
        if not isinstance(row, dict):
            continue
        child_cid = str(row.get("child_conversation_id") or "").strip()
        if not child_cid:
            continue
        incoming = dict(row)
        existing_index = by_cid.get(child_cid)
        if existing_index is None:
            by_cid[child_cid] = len(accumulated)
            accumulated.append(incoming)
            continue
        accumulated[existing_index] = overlay_spawn_child_pointer(
            accumulated[existing_index], incoming
        )


def _with_usage(result: Dict[str, Any], accumulated_usage: Dict[str, Any]) -> Dict[str, Any]:
    # Same peel as usage_stream_fields. Terminal results keep a numeric 0.0
    # (local / unpriced-but-reported); chat frames omit that as unknown.
    usage, raw = _peel_usage_cost(accumulated_usage)
    result["usage"] = usage
    cost = _numeric_cost_usd(raw, priced_only=False)
    if cost is not None:
        result["cost_usd"] = cost
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
    thinking_text: Optional[str] = None,
    tool_summaries: Optional[List[Dict[str, Any]]] = None,
    spawn_children: Optional[List[Dict[str, Any]]] = None,
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
    display_thinking = join_thinking_text(thinking_text)
    if display_thinking:
        payload["thinking_text"] = display_thinking
    display_summaries = list(tool_summaries or [])
    if display_summaries:
        payload["tool_summaries"] = display_summaries
    display_spawn = list(spawn_children or [])
    if display_spawn:
        payload["spawn_children"] = display_spawn
    return _with_usage(payload, accumulated_usage)


def usage_stream_fields(accumulated_usage: Dict[str, Any]) -> Dict[str, Any]:
    """Token envelope plus top-level ``cost_usd`` when the loop has a priced total.

    ``usage`` stays tokens; a dollar amount inside it would be read as a count.
    Absent ``cost_usd`` means unknown, not free.
    """
    usage, raw = _peel_usage_cost(accumulated_usage)
    fields = {key: value for key, value in usage.items() if value is not None}
    cost = priced_cost_usd(raw)
    if cost is not None:
        fields["cost_usd"] = cost
    return fields


def emit_usage_event(
    motet: Any,
    accumulated_usage: Dict[str, Any],
    *,
    stream_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Emit the chat ``usage`` frame for this running total. Returns the fields sent."""
    fields = usage_stream_fields(accumulated_usage)
    if stream_key is not None:
        motet.stream_event("usage", stream_key=stream_key, **fields)
    else:
        motet.stream_event("usage", **fields)
    return fields


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
