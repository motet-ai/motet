"""
Motet - Note Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    No-op note tool. Echoes text into the observation for the current turn
    only. Does not persist to working, short-term, or long-term memory.
    Agents that need to remember something must call core.memory_store.

Dependencies:
    - pydantic: Data validation and model definitions
    - typing: Type hints and annotations
    - Tool registry and protocol system

Usage:
    from motet.core.tools.builtin.note import run

    result = run({"text": "Turn-local comment only"})

Notes:
    - No-op: echoes text, never writes MemoryManager / LTM
    - Use core.memory_store when the user asked to remember something
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, Field

from ..registry import ToolRegistry


class Params(BaseModel):
    text: str = Field(default="")


def _parse(ln: str, trig: str) -> Dict[str, Any]:
    return {"text": ln[len(trig):].strip()}


def _fmt(res: Dict[str, Any]) -> str:
    txt = (res.get("text") or "")
    return f"note(text={txt[:60]})"


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronous note recording (ADR-0033: gevent/eventlet compatible)."""
    # No-op: just echo the text for context/summaries; never store by default
    return {"status": "ok", "text": params.get("text", "")}


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.note",
        description=(
            "Attach a comment to this turn only. Does not persist memory. "
            "To remember a fact for later, use core.memory_store."
        ),
        func=run,
        tool_schema=Params,
        triggers=["note:"],
        priority=10,
        parse_params=_parse,
        observation_formatter=_fmt,
        category="meta",
        contextualize_observation=True,
    )


__all__ = ["register"]


