"""
Motet - Agentic Loop Intents

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Intent payloads the in-process ReAct iteration returns when Turn Runtime
    must write Redis. A leaf module: no checkpoint or runtime
    imports, so agentic_loop can emit intents without depending on
    orchestration/turn.

Dependencies:
    - typing only

Usage:
    from motet.core.reasoning.react.loop_intents import (
        INTENT_HANDBACK, turn_intent, is_turn_intent, calls_require_handback,
    )

    if calls_require_handback(calls, external_names=handback_names):
        return turn_intent(INTENT_HANDBACK, unique_tool_calls=calls, ...)

Notes:
    - Driver calls runtime.materialize_intent when is_turn_intent(result).
    - Keep this module a leaf (no react sibling or checkpoints imports).
    - calls_require_handback matches classify_turn_ownership HANDBACK_ALL so
      the loop never imports motet.core.checkpoints; persist still asserts
      with the canonical classifier before writing Redis.
"""

from __future__ import annotations

from typing import AbstractSet, Any, Dict, Iterable

TURN_INTENT_KEY = "__turn_intent__"

INTENT_HANDBACK = "handback"
INTENT_NESTED_WORKFLOW = "nested_workflow"
INTENT_BUDGET_STOP = "budget_stop"


def is_turn_intent(result: Any) -> bool:
    """True when the loop returned a persist-me intent instead of a terminal dict."""
    return isinstance(result, dict) and TURN_INTENT_KEY in result


def turn_intent(kind: str, **fields: Any) -> Dict[str, Any]:
    """Build a loop intent payload. Runtime materialize_intent writes Redis."""
    payload: Dict[str, Any] = {TURN_INTENT_KEY: kind}
    payload.update(fields)
    return payload


def calls_require_handback(
    calls: Iterable[dict],
    *,
    external_names: AbstractSet[str],
) -> bool:
    """True when any call names an externally-owned tool (HANDBACK_ALL)."""
    if not external_names:
        return False
    for call in calls:
        name = str(call.get("tool_name") or "").strip()
        if name and name in external_names:
            return True
    return False


__all__ = [
    "INTENT_BUDGET_STOP",
    "INTENT_HANDBACK",
    "INTENT_NESTED_WORKFLOW",
    "TURN_INTENT_KEY",
    "calls_require_handback",
    "is_turn_intent",
    "turn_intent",
]
