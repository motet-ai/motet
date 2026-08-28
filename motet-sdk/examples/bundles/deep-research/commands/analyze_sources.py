"""
Motet SDK - Deep Research Example: Parallel Source Analysis Command

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-03

Description:
Fan out extract_findings across all discovered source URLs in parallel
using motet.apply(), then rank and filter the results by relevance.
Demonstrates a second layer of parallel composition — the first layer
(gather_sources) parallelized search, this layer parallelizes analysis.

Dependencies:
- motet_sdk: command decorator and MotetContext typing
- pydantic: structured command inputs

Usage:
Called as the third step of the deep-research workflow:
  deep-research.analyze_sources(
    topic="quantum computing",
    sources=[{"url": "...", "title": "...", "snippet": "..."}]
  )

Notes:
- Caps the number of pages fetched to max_pages to control cost and
  latency.  Sources are taken in order (search-rank order from
  gather_sources).
- Results are sorted by relevance (high > medium > low) for the
  synthesis step.
"""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field

from motet_sdk import BaseCommandData, MotetContext, WorkerCapability, motet


class SourceItem(BaseModel):
    """One source from gather_sources output."""

    url: str = Field(..., description="Page URL")
    title: str = Field(default="", description="Page title")
    snippet: str = Field(default="", description="Search snippet")
    query: str = Field(default="", description="Original search query")


class AnalyzeSourcesData(BaseCommandData):
    """Input for deep-research.analyze_sources."""

    topic: str = Field(..., description="Research topic")
    sources: List[SourceItem] = Field(default_factory=list, description="Sources to analyze")
    max_pages: int = Field(
        default=8,
        ge=1,
        le=20,
        description="Maximum pages to fetch and analyze",
    )
    max_chars: int = Field(
        default=4000,
        ge=500,
        le=12000,
        description="Maximum content chars per page",
    )
    fetch_timeout: float = Field(
        default=30.0,
        ge=5.0,
        le=90.0,
        description="Per-page browser timeout",
    )
    provider: str = Field(default="openai", description="LLM provider")
    model_name: str = Field(default="gpt-4o-mini", description="LLM model name")


_RELEVANCE_ORDER = {"high": 0, "medium": 1, "low": 2}


@motet.command(
    timeout_seconds=240,
    required_capabilities=[
        WorkerCapability.TOOL_EXECUTION,
        WorkerCapability.MODEL_INFERENCE,
    ],
)
def analyze_sources(data: AnalyzeSourcesData, motet: MotetContext) -> Dict[str, Any]:
    """Fetch and analyze sources in parallel, returning ranked findings."""
    capped = data.sources[: data.max_pages]

    if not capped:
        return {
            "topic": data.topic,
            "analyzed": [],
            "analyzed_count": 0,
            "high_relevance_count": 0,
        }

    from .extract_findings import extract_findings

    inputs = [
        {"url": s.url, "title": s.title, "snippet": s.snippet}
        for s in capped
    ]
    command_template = {
        "topic": data.topic,
        "max_chars": data.max_chars,
        "fetch_timeout": data.fetch_timeout,
        "provider": data.provider,
        "model_name": data.model_name,
    }

    try:
        results = motet.apply(
            extract_findings,
            inputs=inputs,
            command_template=command_template,
        )
    except Exception as exc:
        results = [
            {
                "ok": False,
                "url": s.url,
                "findings": [],
                "relevance": "low",
                "summary": f"apply failed: {exc}",
            }
            for s in capped
        ]

    analyzed = sorted(
        [r for r in results if isinstance(r, dict)],
        key=lambda r: _RELEVANCE_ORDER.get(r.get("relevance", "low"), 2),
    )

    high_count = sum(1 for r in analyzed if r.get("relevance") == "high")

    return {
        "topic": data.topic,
        "analyzed": analyzed,
        "analyzed_count": len(analyzed),
        "high_relevance_count": high_count,
    }
