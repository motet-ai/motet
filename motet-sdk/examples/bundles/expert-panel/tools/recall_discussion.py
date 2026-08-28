"""
Motet SDK - Expert Panel Example: Recall Discussion Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-03

Description:
    Recall past expert panel discussions from memory by topic.  Searches
    across all panel agents (optimist, skeptic, synthesizer) to return
    a complete picture of prior analyses on a subject.

    Demonstrates:
    - @motet.tool for custom bundle tool registration
    - Cross-agent memory recall (reads from all panel agents' memories)
    - Recalling what an agent turn actually wrote: core.finalize_turn tags
      each response "agent:<agent_id>", so that is what this tool queries

Dependencies:
    - motet_sdk: @motet.tool decorator, MotetContext
    - pydantic: tool parameter schema

Usage:
    The agent can call this tool when the user asks about past discussions:
      "What did the panel say about remote work?"
      → expert-panel.recall_discussion(topic="remote work")

Notes:
    - Queries the "agent:expert-panel.*" tags written by core.finalize_turn.
    - Topic filtering is MemoryManager hybrid recall (query coverage,
      head-biased); this tool derives perspective from the agent identity.
    - Returns analyses from all three perspectives (optimist, skeptic,
      synthesizer) so the agent can provide a complete summary.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from motet_sdk import get_motet_context, motet

_MIN_TOPIC_RELEVANCE = 0.8


class RecallDiscussionParams(BaseModel):
    """Input for recall_discussion tool."""

    topic: str = Field(
        ...,
        description="Topic or keywords to search for in past panel discussions",
    )
    perspective: str = Field(
        default="all",
        description="Filter by perspective: 'optimist', 'skeptic', 'synthesizer', or 'all'",
    )
    limit: int = Field(
        default=6, ge=1, le=20,
        description="Maximum number of results to return",
    )


def _fmt(res: Dict[str, Any]) -> str:
    count = res.get("result_count", 0)
    topic = res.get("topic", "?")
    perspectives = res.get("perspectives_found", [])
    return f"recall_discussion(topic={topic!r}, found={count}, perspectives={perspectives})"


# core.finalize_turn tags each agent response "agent:<agent_id>".
_PANEL_AGENTS = ("optimist", "skeptic", "synthesizer")
_PERSPECTIVE_ALIASES = {"synthesis": "synthesizer", "moderator": "synthesizer", "critic": "skeptic"}


def _agent_ids_for(perspective: str) -> List[str]:
    """Map a requested perspective onto panel agent ids."""
    name = (perspective or "all").strip().lower()
    name = _PERSPECTIVE_ALIASES.get(name, name)
    if name in _PANEL_AGENTS:
        return [f"expert-panel.{name}"]
    return [f"expert-panel.{a}" for a in _PANEL_AGENTS]


def _as_dict(item: Any) -> Dict[str, Any]:
    if hasattr(item, "model_dump"):
        dumped = item.model_dump()
        return dumped if isinstance(dumped, dict) else {"content": str(item)}
    if isinstance(item, dict):
        return item
    return {"content": str(item)}


def _perspective_of(dumped: Dict[str, Any], wanted: List[str]) -> Optional[str]:
    """Derive the panel perspective from agent identity, or None if not a panel memory."""
    raw_meta = dumped.get("metadata")
    meta: Dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    agent_id = str(meta.get("agent_id") or "")
    if not agent_id:
        for tag in dumped.get("tags") or []:
            if str(tag).startswith("agent:expert-panel."):
                agent_id = str(tag).split(":", 1)[1]
                break
    if agent_id not in wanted:
        return None
    return agent_id.split(".", 1)[1]


@motet.tool(
    description=(
        "Recall past expert panel discussions from memory. Searches across "
        "all panel agents (optimist, skeptic, synthesizer) by topic. "
        "Use when the user asks about previously discussed topics. "
        "Accepts 'topic' (string, required), 'perspective' ('optimist', "
        "'skeptic', 'synthesizer', or 'all'), and 'limit' (int, default 6)."
    ),
    name="recall_discussion",
    schema=RecallDiscussionParams,
    observation_formatter=_fmt,
    category="panel",
    cost_class="low",
    # Keep these past-tense. Bare "panel"/"discussion"/"debate" made this tool
    # outrank workflow_expert-panel.discuss for "run a panel on X", so the agent
    # searched for a way to recall a discussion that had never been held.
    keywords=[
        "past panel discussion",
        "prior panel analysis",
        "previously discussed topic",
        "recall panel memory",
    ],
)
def recall_discussion(params: Dict[str, Any]) -> Dict[str, Any]:
    """Recall past expert panel discussions by topic from memory."""
    parsed = RecallDiscussionParams(**(params or {}))

    try:
        ctx = get_motet_context()
    except Exception:
        ctx = None

    if not ctx or not getattr(ctx, "memory", None):
        return {
            "topic": parsed.topic,
            "results": [],
            "result_count": 0,
            "perspectives_found": [],
            "error": "Memory not available in current context.",
        }

    agent_ids = _agent_ids_for(parsed.perspective)

    try:
        items = ctx.memory.recall(
            query=parsed.topic,
            tags=[f"agent:{agent_id}" for agent_id in agent_ids],
            # Over-fetch a little: tag filters are OR-matched and perspective
            # derivation still drops non-panel rows.
            limit=max(parsed.limit * 4, 20),
            min_relevance=_MIN_TOPIC_RELEVANCE,
        )
    except Exception as exc:
        return {
            "topic": parsed.topic,
            "results": [],
            "result_count": 0,
            "perspectives_found": [],
            "error": f"Memory recall failed: {exc}",
        }

    results: List[Dict[str, Any]] = []
    for item in items or []:
        dumped = _as_dict(item)
        perspective = _perspective_of(dumped, agent_ids)
        if perspective is None:
            continue
        raw_meta = dumped.get("metadata")
        meta: Dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        results.append(
            {
                "content": dumped.get("content", ""),
                "perspective": perspective,
                "tags": dumped.get("tags", []),
                "created_at": str(dumped.get("created_at", "")),
                "conversation_id": meta.get("conversation_id"),
            }
        )
        if len(results) >= parsed.limit:
            break

    return {
        "topic": parsed.topic,
        "results": results,
        "result_count": len(results),
        "perspectives_found": sorted({r["perspective"] for r in results}),
    }
