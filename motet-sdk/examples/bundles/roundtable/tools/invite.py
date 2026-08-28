"""
Motet SDK - Roundtable Example: invite Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Invite one agent to speak. This is the whole speaker-selection mechanism:
    the facilitator's model picks an agent_id and calls this tool, so the next
    speaker is a runtime decision rather than an edge declared in a workflow.
    Because the agentic loop may call the tool repeatedly within a turn,
    successive invites become discussion rounds, and each invited agent is
    briefed with the recent transcript so it can respond to what was said.

Dependencies:
    - motet_sdk: @motet.tool, get_motet_context
    - MotetContext.agents.turn → agent_turn command
    - ._transcript: conversation-scoped shared channel

Usage:
    roundtable.invite(agent_id="roundtable.researcher", question="What does the evidence say?")

Notes:
    - Refuses to invite the facilitator, which would recurse.
    - Panelists are configured with no tools, so an invited agent answers and
      stops rather than starting a panel of its own.
    - The invited agent runs as its own agent turn, so its response is
      attributed to its agent_id in streams and tagged in memory.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from motet_sdk import get_motet_context, motet

from ._transcript import append_turn, load_transcript, render_transcript

_FACILITATOR_ID = "roundtable.facilitator"
_CONTEXT_TURNS = 4


class InviteParams(BaseModel):
    """Input for invite."""

    agent_id: str = Field(
        ...,
        description="Agent to invite, exactly as returned by roundtable__roster (e.g. roundtable.researcher)",
    )
    question: str = Field(
        ...,
        description="What you want this agent to address in its own words",
    )
    topic: Optional[str] = Field(
        default=None,
        description="Overall discussion topic; recorded once on the first invite",
    )
    include_transcript: bool = Field(
        default=True,
        description="Brief the agent with recent turns so it can respond to what was already said",
    )


def _fmt(res: Dict[str, Any]) -> str:
    return (
        f"invite(agent={res.get('agent_id')!r}, round={res.get('round')}, "
        f"status={res.get('status')!r})"
    )


def _extract_response_text(result: Any) -> str:
    """Pull the assistant text out of an agent_turn result."""
    if not isinstance(result, dict):
        return str(result or "")
    for key in ("final_response", "response", "content", "text"):
        val = result.get(key)
        if isinstance(val, str) and val.strip():
            return val
    data = result.get("data")
    if isinstance(data, dict):
        for key in ("final_response", "response", "content", "text"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val
    return ""


@motet.tool(
    description=(
        "Invite one agent to speak on the current topic and return what it "
        "said. Call roundtable__roster first to get valid ids. Call this once "
        "per speaker; call it again to run another round after you have read "
        "the replies. Accepts 'agent_id' (string, required), 'question' "
        "(string, required), 'topic' (string, optional), and "
        "'include_transcript' (bool, default true)."
    ),
    name="invite",
    schema=InviteParams,
    observation_formatter=_fmt,
    category="roundtable",
    cost_class="high",
    keywords=["invite agent", "ask panelist", "next speaker", "hand off to agent"],
)
def invite(params: Dict[str, Any]) -> Dict[str, Any]:
    """Run one turn with the chosen agent and record it in the transcript."""
    parsed = InviteParams(**(params or {}))
    agent_id = parsed.agent_id.strip()

    if agent_id == _FACILITATOR_ID:
        return {
            "status": "error",
            "agent_id": agent_id,
            "error": "Cannot invite the facilitator. Choose a panelist from roundtable__roster.",
        }

    try:
        ctx = get_motet_context()
    except Exception:
        ctx = None

    if ctx is None or not getattr(ctx, "agents", None):
        return {
            "status": "error",
            "agent_id": agent_id,
            "error": "Agent turn not available in current context.",
        }

    if ctx.agents.get(agent_id) is None:
        return {
            "status": "error",
            "agent_id": agent_id,
            "error": f"Unknown agent {agent_id!r}. Call roundtable__roster for valid ids.",
        }

    prompt_parts = []
    if parsed.include_transcript:
        prior = render_transcript(load_transcript(ctx), limit=_CONTEXT_TURNS)
        if prior:
            prompt_parts.append(
                "So far in this discussion:\n\n"
                f"{prior}\n\n"
                "Respond to the substance above where it is relevant — agree, "
                "disagree, or add what is missing. Do not simply repeat it."
            )
    prompt_parts.append(parsed.question)
    prompt = "\n\n".join(prompt_parts)

    try:
        result = ctx.agents.turn(agent_id, messages=[{"role": "user", "content": prompt}])
    except Exception as exc:
        return {
            "status": "error",
            "agent_id": agent_id,
            "error": f"agent_turn failed for {agent_id}: {exc}",
        }

    response = _extract_response_text(result)
    if not response:
        return {
            "status": "incomplete",
            "agent_id": agent_id,
            "error": f"{agent_id} returned no text.",
            "response": "",
        }

    transcript = append_turn(
        ctx,
        agent_id=agent_id,
        question=parsed.question,
        response=response,
        topic=parsed.topic,
    )
    spoken_round = transcript.turns[-1].round if transcript.turns else 1

    return {
        "status": "ok",
        "agent_id": agent_id,
        "round": spoken_round,
        "response": response,
        "turn_count": len(transcript.turns),
    }
