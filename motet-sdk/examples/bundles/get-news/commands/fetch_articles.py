"""
Motet SDK - Workflow Pipeline Example Fetch Command

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-03-17

Description:
Fetch article text and metadata for discovered sources using a browser-capable
tool, then normalize the result shape for clustering and digest generation.
Uses ADR-0052 command composition (`motet.apply`) to fan out per-source fetches.

Dependencies:
- motet_sdk: command decorator and MotetContext typing
- pydantic: structured command inputs

Usage:
Call via command execution:
  get-news.fetch_articles(
    topic="AI regulation",
    sources=[{"url": "https://example.com"}],
    fetch_tool_name="core.http_get_browser"
  )

Notes:
- The tool name is configurable because environments can register different
  browser-capable tools/workflows.
- Per-source failures are captured and returned; they do not fail the command.
"""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field

from motet_sdk import BaseCommandData, MotetContext, WorkerCapability, motet


class SourceItem(BaseModel):
    """Source URL entry for fetch step."""

    url: str = Field(..., description="Source URL to fetch")
    kind: str = Field(default="search", description="Source classification")


class FetchArticlesData(BaseCommandData):
    """Input for get-news.fetch_articles."""

    topic: str = Field(..., description="Topic being aggregated")
    sources: List[SourceItem] = Field(default_factory=list, description="Source URLs to fetch")
    fetch_tool_name: str = Field(
        default="core.http_get_browser",
        description="Browser-capable tool/workflow name to execute",
    )
    max_chars: int = Field(
        default=3500,
        ge=200,
        le=12000,
        description="Maximum characters of extracted content per source",
    )
    request_timeout_seconds: float = Field(
        default=40.0,
        ge=5.0,
        le=120.0,
        description="Per-source browser fetch timeout in seconds",
    )


@motet.command(
    timeout_seconds=210,
    required_capabilities=[WorkerCapability.TOOL_EXECUTION],
)
def fetch_articles(data: FetchArticlesData, motet: MotetContext) -> Dict[str, Any]:
    """Fetch sources in parallel using distributed command composition."""
    articles: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    if not data.sources:
        return {
            "topic": data.topic,
            "articles": [],
            "fetched_count": 0,
            "failure_count": 0,
            "failures": [],
            "bundle": "get-news",
            "task_id": motet.task_id,
        }

    from .fetch_source import fetch_source

    inputs = [{"url": source.url, "kind": source.kind} for source in data.sources]
    command_template = {
        "topic": data.topic,
        "fetch_tool_name": data.fetch_tool_name,
        "max_chars": data.max_chars,
        "request_timeout_seconds": data.request_timeout_seconds,
    }

    try:
        results = motet.apply(
            fetch_source,
            inputs=inputs,
            command_template=command_template,
        )
    except Exception as exc:
        # Preserve graceful behavior for the demo if orchestration-level apply fails.
        results = [
            {
                "ok": False,
                "url": source.url,
                "source_kind": source.kind,
                "error": f"parallel fetch orchestration failed: {exc}",
                "tool_name": data.fetch_tool_name,
            }
            for source in data.sources
        ]

    for item in results:
        if item.get("ok"):
            articles.append(item.get("article", {}))
        else:
            failures.append(
                {
                    "url": item.get("url"),
                    "error": item.get("error", "unknown fetch failure"),
                    "tool_name": item.get("tool_name", data.fetch_tool_name),
                }
            )

    # Keep deterministic ordering by URL in output for stable comparisons.
    articles.sort(key=lambda item: item.get("url", ""))
    failures.sort(key=lambda item: item.get("url", ""))

    return {
        "topic": data.topic,
        "articles": articles,
        "fetched_count": len(articles),
        "failure_count": len(failures),
        "failures": failures,
        "bundle": "get-news",
        "task_id": motet.task_id,
    }
