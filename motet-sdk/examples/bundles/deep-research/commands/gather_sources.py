"""
Motet SDK - Deep Research Example: Parallel Source Gathering Command

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-03

Description:
Fan out search queries in parallel using motet.apply(), then deduplicate
and rank the combined results.  Demonstrates ADR-0052 parallel command
composition at scale.

Dependencies:
- motet_sdk: command decorator and MotetContext typing
- pydantic: structured command inputs

Usage:
Called as the second step of the deep-research workflow:
  deep-research.gather_sources(queries=["query1", "query2", ...])

Notes:
- Uses motet.apply() to execute search_source across all queries in
  parallel, collecting results into a unified source list.
- Deduplicates by URL, keeping each query's search order; analyze_sources
  applies the max_pages cap that bounds downstream fetch cost.
- Forwards optional provider/model_name into search_source so core.web_search
  can attempt the LLM-native path when available.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, WorkerCapability, motet


class GatherSourcesData(BaseCommandData):
    """Input for deep-research.gather_sources."""

    topic: str = Field(..., description="Original research topic")
    queries: List[str] = Field(default_factory=list, description="Search queries to execute")
    max_results_per_query: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum results per search query",
    )
    provider: Optional[str] = Field(
        default=None,
        description="Optional LLM provider forwarded to search_source / core.web_search",
    )
    model_name: Optional[str] = Field(
        default=None,
        description="Optional LLM model name forwarded to search_source / core.web_search",
    )


@motet.command(
    timeout_seconds=120,
    required_capabilities=[WorkerCapability.TOOL_EXECUTION],
)
def gather_sources(data: GatherSourcesData, motet: MotetContext) -> Dict[str, Any]:
    """Fan out web searches in parallel and deduplicate results."""
    if not data.queries:
        return {
            "topic": data.topic,
            "sources": [],
            "source_count": 0,
            "queries_executed": 0,
        }

    from .search_source import search_source

    inputs = [{"query": q} for q in data.queries]
    command_template: Dict[str, Any] = {"max_results": data.max_results_per_query}
    if data.provider:
        command_template["provider"] = data.provider
    if data.model_name:
        command_template["model_name"] = data.model_name

    try:
        results = motet.apply(
            search_source,
            inputs=inputs,
            command_template=command_template,
        )
    except Exception as exc:
        results = [
            {"ok": False, "query": q, "results": [], "error": f"apply failed: {exc}"}
            for q in data.queries
        ]

    seen_urls: set = set()
    sources: List[Dict[str, Any]] = []
    for search_result in results:
        if not isinstance(search_result, dict) or not search_result.get("ok"):
            continue
        for item in search_result.get("results", []):
            url = item.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                sources.append(
                    {
                        "url": url,
                        "title": item.get("title", ""),
                        "snippet": item.get("snippet", ""),
                        "query": search_result.get("query", ""),
                    }
                )

    return {
        "topic": data.topic,
        "sources": sources,
        "source_count": len(sources),
        "queries_executed": len(data.queries),
    }
