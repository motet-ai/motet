"""Stateful lifetime demo: in-memory counter.

Author note (ADR-0106 ``lifetime: stateful`` contract):

    A stateful runner script must define a top-level ``handle(params: dict)``
    callable. The Motet supervisor imports this module *once* per
    workspace container and calls ``handle`` for every dispatch. Anything
    you put at module scope (counters, loaded models, open DB
    connections) survives between calls within the same conversation.

    ``params`` is the JSON-decoded dict the LLM sent through the runner;
    return any JSON-serializable value. Non-dict returns are wrapped as
    ``{"value": ...}`` by the supervisor so the wire shape stays uniform.

    DO NOT call ``sys.exit`` or raise ``SystemExit``: that would tear
    down the supervisor and reset the counter. Raise normal exceptions
    instead — the supervisor catches them and returns a structured
    ``ok=False`` envelope while staying alive for the next call.
"""

from __future__ import annotations

from typing import Any, Dict, List

# These globals are the *whole point* of stateful mode: they live for the
# lifetime of the per-conversation container, so the LLM can call this
# runner repeatedly and watch state build up.
_count: int = 0
_history: List[str] = []


def handle(params: Dict[str, Any]) -> Dict[str, Any]:
    global _count
    label = str(params.get("label") or f"call-{_count + 1}")
    _count += 1
    _history.append(label)
    return {
        "label": label,
        "count": _count,
        "history": list(_history),
    }
