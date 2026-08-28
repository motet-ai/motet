"""
Motet SDK - Background Thinker Example: Reflect Command

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-21

Description:
The core scheduled command: read past insights from memory, use LLM
inference to generate deeper analysis, then store the new insight back
in memory.  Each reflection cycle builds on prior thinking, creating an
evolving chain of progressively deeper understanding.

Demonstrates motet.models.infer() for autonomous LLM reasoning and
motet.memory for persistent knowledge loops (write → schedule tick →
read → think → write).

Dependencies:
- motet_sdk: command decorator and MotetContext typing
- pydantic: structured command inputs

Usage:
Typically invoked by the scheduler (not called directly):
  # The schedule created by start_thinking calls this automatically
  background-thinker.reflect(topic="quantum computing")

Can also be called manually for a single ad-hoc reflection:
  motet.do(reflect, data=ReflectData(topic="AI safety"))

Notes:
- Each insight is tagged "background-thinker" + "insight" for retrieval
  by recall_insights and check_insights.
- The iteration number is tracked in metadata so the LLM can reference
  how many reflection cycles have occurred.
- The reflection prompt encourages the LLM to go beyond surface analysis:
  identify contradictions, form hypotheses, and suggest questions worth
  investigating further.
"""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, WorkerCapability, motet

from . import _memory as mem


class ReflectData(BaseCommandData):
    """Input for background-thinker.reflect."""

    topic: str = Field(..., description="Topic to reflect on")
    provider: str = Field(default="openai", description="LLM provider")
    model_name: str = Field(default="gpt-4o-mini", description="LLM model name")


_SYSTEM_PROMPT = """\
You are a deep thinker engaged in ongoing background reflection.  Your job \
is to build progressively deeper understanding of a topic over multiple \
thinking sessions.  Each session you receive your prior insights and must \
advance the thinking — don't repeat what you already know.

Guidelines:
- Identify patterns, contradictions, or gaps in your prior thinking.
- Form hypotheses worth testing or questions worth investigating.
- Connect ideas across different facets of the topic.
- Be specific and concrete — avoid generic platitudes.
- If this is your first reflection, lay the foundational framework.
- Output a concise insight (3-5 paragraphs) with a clear thesis.\
"""


def _format_prior_insights(items: List[Any]) -> str:
    """Format recalled memory items into a text block for the reflection prompt."""
    parts: List[str] = []
    for item in items:
        if hasattr(item, "model_dump"):
            dumped = item.model_dump()
        elif isinstance(item, dict):
            dumped = item
        else:
            dumped = {"content": str(item)}
        content = dumped.get("content", "")
        raw_meta = dumped.get("metadata")
        meta: Dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        iteration = meta.get("iteration", "?")
        parts.append(f"--- Reflection #{iteration} ---\n{content}")
    return "\n\n".join(parts) if parts else "(No prior thinking on this topic.)"


def _next_iteration(items: List[Any]) -> int:
    """Determine the next iteration number from prior insights."""
    max_iter = 0
    for item in items:
        if hasattr(item, "model_dump"):
            dumped = item.model_dump()
        elif isinstance(item, dict):
            dumped = item
        else:
            dumped = {}
        raw_meta = dumped.get("metadata")
        meta: Dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        iteration = meta.get("iteration", 0)
        if isinstance(iteration, int) and iteration > max_iter:
            max_iter = iteration
    return max_iter + 1


@motet.command(
    timeout_seconds=120,
    required_capabilities=[
        WorkerCapability.MODEL_INFERENCE,
        WorkerCapability.MEMORY_OPERATIONS,
    ],
)
def reflect(data: ReflectData, motet: MotetContext) -> Dict[str, Any]:
    """Run one reflection cycle: recall prior thinking, generate new insight, store it."""

    try:
        prior_items = mem.recall_insights(motet, topic=data.topic, limit=5)
    except Exception:
        prior_items = []

    iteration = _next_iteration(prior_items)
    prior_text = _format_prior_insights(prior_items)

    user_prompt = (
        f"TOPIC: {data.topic}\n\n"
        f"REFLECTION CYCLE: #{iteration}\n\n"
        f"PRIOR THINKING:\n{prior_text}\n\n"
        "Generate your next insight.  Build on what came before — go deeper, "
        "challenge assumptions, or explore a new angle."
    )

    result = motet.models.infer(
        provider=data.provider,
        model_name=data.model_name,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
        max_tokens=1500,
    )

    insight = result.get("content") or result.get("response") or ""

    memory_id = None
    memory_status = "pending"
    try:
        topic_words = data.topic.lower().split()[:4]
        tags = ["background-thinker", "insight"] + [w for w in topic_words if len(w) > 2]
        mem_result = motet.memory.store(
            content=insight,
            type="background_insight",
            tags=tags,
            metadata={
                "topic": data.topic,
                "iteration": iteration,
                "bundle": "background-thinker",
            },
            scope_type="principal",
        )
        memory_id = mem_result.get("memory_id") or mem_result.get("id")
        memory_status = "stored" if memory_id else "store_returned_no_id"
    except Exception as exc:
        memory_status = f"error: {exc}"

    return {
        "topic": data.topic,
        "iteration": iteration,
        "insight": insight,
        "prior_count": len(prior_items),
        "memory_id": memory_id,
        "memory_status": memory_status,
    }
