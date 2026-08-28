"""
Motet SDK - Background Thinker Example: Check Insights Command

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-03

Description:
Retrieve all accumulated background insights for a topic and use LLM
inference to produce a coherent summary of everything the thinker has
concluded so far.  Demonstrates motet.memory.recall() for knowledge
retrieval combined with motet.models.infer() for multi-document synthesis.

Dependencies:
- motet_sdk: command decorator and MotetContext typing
- pydantic: structured command inputs

Usage:
  background-thinker.check_insights(topic="quantum computing")

  # Raw insights without LLM summary
  background-thinker.check_insights(topic="quantum computing", summarize=False)

Notes:
- Unlike the recall_insights tool (which returns raw memory items for
  LLM consumption), this command synthesizes the insights with an LLM
  into a structured, readable summary.
- Used as the second step of the background_reflection workflow.
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, WorkerCapability, motet

from . import _memory as mem


class CheckInsightsData(BaseCommandData):
    """Input for background-thinker.check_insights."""

    topic: str = Field(..., description="Topic to retrieve insights for")
    limit: int = Field(default=10, ge=1, le=50, description="Maximum insights to retrieve")
    summarize: bool = Field(
        default=True,
        description="Use LLM to synthesize a summary (False returns raw insights only)",
    )
    provider: str = Field(default="openai", description="LLM provider for synthesis")
    model_name: str = Field(default="gpt-4o-mini", description="LLM model for synthesis")


_SUMMARY_PROMPT = """\
You are reviewing a series of background reflections on a topic produced \
over multiple autonomous thinking sessions.  Synthesize them into a clear, \
structured summary that captures:

1. **Key Conclusions** — the strongest insights reached so far
2. **Evolving Themes** — how the thinking developed over iterations
3. **Open Questions** — unresolved questions or areas needing more thought
4. **Connections** — non-obvious links between different insights

Be concise but thorough.  Use markdown formatting.

TOPIC: {topic}
REFLECTION COUNT: {count}

REFLECTIONS:
{reflections}\
"""


@motet.command(
    timeout_seconds=90,
    required_capabilities=[
        WorkerCapability.MODEL_INFERENCE,
        WorkerCapability.MEMORY_OPERATIONS,
    ],
)
def check_insights(data: CheckInsightsData, motet: MotetContext) -> Dict[str, Any]:
    """Retrieve and optionally summarize all background insights for a topic."""

    try:
        insights = mem.recall_insights(motet, topic=data.topic, limit=data.limit)
    except Exception:
        insights = []

    if not insights:
        return {
            "topic": data.topic,
            "insight_count": 0,
            "insights": [],
            "summary": None,
            "status": "no_insights_found",
        }

    summary = None
    if data.summarize:
        reflections_text = "\n\n".join(
            f"--- Reflection #{i['iteration']} ---\n{i['content']}"
            for i in insights
        )
        prompt = _SUMMARY_PROMPT.format(
            topic=data.topic,
            count=len(insights),
            reflections=reflections_text,
        )
        result = motet.models.infer(
            provider=data.provider,
            model_name=data.model_name,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=2000,
        )
        summary = result.get("content") or result.get("response") or ""

    return {
        "topic": data.topic,
        "insight_count": len(insights),
        "insights": insights,
        "summary": summary,
        "status": "summarized" if summary else "raw",
    }
