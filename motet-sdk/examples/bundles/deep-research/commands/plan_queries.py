"""
Motet SDK - Deep Research Example: Query Planning Command

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-03

Description:
Use LLM inference to decompose a broad research topic into specific,
targeted search queries.  Demonstrates motet.models.infer() for
LLM-powered planning within a distributed command.

Dependencies:
- motet_sdk: command decorator and MotetContext typing
- pydantic: structured command inputs

Usage:
Called as the first step of the deep-research workflow:
  deep-research.plan_queries(topic="quantum computing breakthroughs 2026")

Notes:
- The LLM generates diverse queries covering different facets of the topic
  to ensure broad coverage when the results are searched in parallel.
- Provider and model are configurable so the bundle works across deployments.
- Returns planning_status ("planned" or "fallback_topic_only") so callers can
  tell a real plan from the degraded single-query path when JSON fails to parse.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, WorkerCapability, motet


class PlanQueriesData(BaseCommandData):
    """Input for deep-research.plan_queries."""

    topic: str = Field(..., description="Research topic to investigate")
    num_queries: int = Field(
        default=5,
        ge=2,
        le=10,
        description="Number of search queries to generate",
    )
    provider: str = Field(
        default="openai",
        description="LLM provider for query generation",
    )
    model_name: str = Field(
        default="gpt-4o-mini",
        description="Model name for query generation",
    )


_PLAN_PROMPT = """\
You are a research assistant.  Given the TOPIC below, generate exactly \
{num_queries} diverse web search queries that will help build a comprehensive \
understanding of the topic.

Rules:
- Each query should target a different facet (e.g. recent developments, \
key players, technical details, controversies, statistics).
- Queries should be specific enough to return high-quality results.
- Output ONLY a JSON array of strings — no commentary.

TOPIC: {topic}
"""


def _parse_queries(text: str, n: int) -> Optional[List[str]]:
    """Extract a JSON string list from LLM output, or None if unparsable."""
    match = re.search(r"\[.*\]", text.strip(), re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group())
        except (json.JSONDecodeError, TypeError):
            return None
        if isinstance(parsed, list) and all(isinstance(q, str) for q in parsed):
            queries = [q for q in parsed[:n] if q.strip()]
            return queries or None
    return None


@motet.command(
    timeout_seconds=60,
    required_capabilities=[WorkerCapability.MODEL_INFERENCE],
)
def plan_queries(data: PlanQueriesData, motet: MotetContext) -> Dict[str, Any]:
    """Use an LLM to decompose a topic into targeted search queries."""
    prompt = _PLAN_PROMPT.format(topic=data.topic, num_queries=data.num_queries)

    result = motet.models.infer(
        provider=data.provider,
        model_name=data.model_name,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4,
        max_tokens=800,
    )

    content = result.get("content") or result.get("response") or ""
    queries = _parse_queries(content, data.num_queries)

    # Searching the bare topic still works, but report the degrade so a thin
    # run is distinguishable from a topic that genuinely yielded one query.
    planning_status = "planned"
    if not queries:
        queries = [data.topic]
        planning_status = "fallback_topic_only"

    return {
        "topic": data.topic,
        "queries": queries,
        "query_count": len(queries),
        "planning_status": planning_status,
    }
