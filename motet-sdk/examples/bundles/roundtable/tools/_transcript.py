"""
Motet SDK - Roundtable Example: Shared Transcript Store

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Conversation-scoped transcript for the roundtable example bundle. Every
    invite appends one turn, so panelists invited later can be shown what was
    already said and the facilitator can decide whether another round is
    needed. This is the "shared channel" that a purpose-built multi-agent
    framework would call a group chat — here it is a small store plus the
    conversation id that the runtime already threads through every command.

Dependencies:
    - pydantic: Turn / Transcript schema
    - MotetContext.redis (optional): conversation-scoped persistence

Usage:
    from ._transcript import append_turn, load_transcript, render_transcript

Notes:
    - Redis key: roundtable:transcript:{conversation_id} (24-hour TTL).
    - Falls back to a process-local dict when redis is unavailable (tests/dev).
    - Pure helpers (render_transcript) are importable without a live runtime.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

_TRANSCRIPT_KEY_PREFIX = "roundtable:transcript:"
_TRANSCRIPT_TTL_SECONDS = 24 * 3600
_MAX_TURNS = 40
_FALLBACK_STORE: Dict[str, str] = {}


class Turn(BaseModel):
    """One contribution from one invited agent."""

    round: int = Field(default=1, description="1-based round number")
    agent_id: str = Field(..., description="Agent that spoke, e.g. roundtable.researcher")
    question: str = Field(default="", description="Prompt the facilitator posed")
    response: str = Field(default="", description="What the agent replied")
    at: str = Field(default="", description="ISO-8601 UTC timestamp")


class Transcript(BaseModel):
    """Ordered record of a roundtable discussion."""

    version: int = Field(default=1, description="Schema version")
    topic: str = Field(default="", description="Topic under discussion")
    turns: List[Turn] = Field(default_factory=list, description="Turns in order")


def utc_now_iso() -> str:
    """Return current UTC time as ISO-8601."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def resolve_conversation_id(ctx: Any) -> str:
    """Best-effort conversation id from MotetContext."""
    if ctx is None:
        return "default"
    try:
        if hasattr(ctx, "resolve_conversation_id"):
            cid = ctx.resolve_conversation_id()
            if cid:
                return str(cid)
    except Exception:
        pass
    cid = getattr(ctx, "conversation_id", None)
    return str(cid).strip() if cid else "default"


def conversation_key(conversation_id: str) -> str:
    """Redis / fallback key for a conversation's transcript."""
    cid = (conversation_id or "").strip() or "default"
    return f"{_TRANSCRIPT_KEY_PREFIX}{cid}"


def load_transcript(ctx: Any) -> Transcript:
    """Load the transcript for this conversation, or an empty one."""
    key = conversation_key(resolve_conversation_id(ctx))
    raw: Any = None
    redis = getattr(ctx, "redis", None) if ctx is not None else None
    if redis is not None:
        try:
            raw = redis.get(key)
        except Exception:
            raw = None
    if raw is None:
        raw = _FALLBACK_STORE.get(key)
    if not raw:
        return Transcript()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, dict):
        data: Any = dict(raw)
    else:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return Transcript()
    if not isinstance(data, dict):
        return Transcript()
    try:
        return Transcript(**data)
    except Exception:
        return Transcript()


def save_transcript(ctx: Any, transcript: Transcript) -> str:
    """Persist the transcript for this conversation; returns storage key."""
    key = conversation_key(resolve_conversation_id(ctx))
    payload = transcript.model_dump_json()
    redis = getattr(ctx, "redis", None) if ctx is not None else None
    if redis is not None:
        try:
            redis.set(key, payload, ex=_TRANSCRIPT_TTL_SECONDS)
            return key
        except Exception:
            pass
    _FALLBACK_STORE[key] = payload
    return key


def next_round(transcript: Transcript, agent_id: str) -> int:
    """Round number for the next turn by ``agent_id``.

    A round advances per speaker rather than globally, so the facilitator is
    free to invite an uneven set each pass without the count going backwards.
    """
    spoken = sum(1 for t in transcript.turns if t.agent_id == agent_id)
    return spoken + 1


def append_turn(
    ctx: Any,
    *,
    agent_id: str,
    question: str,
    response: str,
    topic: Optional[str] = None,
) -> Transcript:
    """Append one turn and persist. Trims to the most recent ``_MAX_TURNS``."""
    transcript = load_transcript(ctx)
    turn = Turn(
        round=next_round(transcript, agent_id),
        agent_id=agent_id,
        question=question,
        response=response,
        at=utc_now_iso(),
    )
    turns = [*transcript.turns, turn][-_MAX_TURNS:]
    updates: Dict[str, Any] = {"turns": turns}
    if topic and not transcript.topic:
        updates["topic"] = topic
    transcript = transcript.model_copy(update=updates)
    save_transcript(ctx, transcript)
    return transcript


def render_transcript(transcript: Transcript, *, limit: int = 0) -> str:
    """Render the transcript as prompt-ready text.

    Pass ``limit`` to include only the most recent N turns, which is what the
    invite tool uses to brief a panelist without resending the whole history.
    """
    turns = transcript.turns[-limit:] if limit > 0 else transcript.turns
    if not turns:
        return ""
    lines: List[str] = []
    for turn in turns:
        speaker = turn.agent_id.split(".", 1)[-1]
        lines.append(f"[{speaker} · round {turn.round}]")
        lines.append(turn.response.strip())
        lines.append("")
    return "\n".join(lines).rstrip()


def clear_fallback_store() -> None:
    """Clear process-local fallback (tests)."""
    _FALLBACK_STORE.clear()
