"""
Motet SDK - Roundtable Example: transcript Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Read back the discussion so far for this conversation. The facilitator
    uses it to decide whether another round is warranted and to write a
    closing synthesis grounded in what was actually said rather than in what
    it remembers saying.

Dependencies:
    - motet_sdk: @motet.tool, get_motet_context
    - ._transcript: conversation-scoped shared channel

Usage:
    roundtable.transcript()
    roundtable.transcript(limit=4)

Notes:
    - Scoped to the current conversation; a new conversation starts empty.
    - Returns both structured turns and prompt-ready markdown.
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, Field

from motet_sdk import get_motet_context, motet

from ._transcript import load_transcript, render_transcript


class TranscriptParams(BaseModel):
    """Input for transcript."""

    limit: int = Field(
        default=0, ge=0, le=40,
        description="Return only the most recent N turns; 0 returns all",
    )


def _fmt(res: Dict[str, Any]) -> str:
    return f"transcript(turns={res.get('turn_count', 0)}, speakers={res.get('speakers', [])})"


@motet.tool(
    description=(
        "Read the roundtable discussion so far in this conversation. Use it "
        "before deciding whether to run another round, and before writing "
        "your closing synthesis. Accepts 'limit' (int, default 0 = all turns)."
    ),
    name="transcript",
    schema=TranscriptParams,
    observation_formatter=_fmt,
    category="roundtable",
    cost_class="low",
    keywords=["discussion so far", "what was said", "roundtable transcript", "review rounds"],
)
def transcript(params: Dict[str, Any]) -> Dict[str, Any]:
    """Return the recorded turns for this conversation."""
    parsed = TranscriptParams(**(params or {}))

    try:
        ctx = get_motet_context()
    except Exception:
        ctx = None

    if ctx is None:
        return {
            "topic": "",
            "turns": [],
            "turn_count": 0,
            "speakers": [],
            "markdown": "",
            "error": "Context not available.",
        }

    record = load_transcript(ctx)
    turns = record.turns[-parsed.limit:] if parsed.limit > 0 else record.turns

    return {
        "topic": record.topic,
        "turns": [t.model_dump() for t in turns],
        "turn_count": len(turns),
        "speakers": sorted({t.agent_id for t in turns}),
        "markdown": render_transcript(record, limit=parsed.limit),
    }
