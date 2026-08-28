"""
Motet SDK - Workflow Pipeline Example Per-Source Fetch Command

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-03-17

Description:
Fetch one source URL for the get-news example bundle and normalize output into
an article record. Designed to be composed in parallel from fetch_articles via
ADR-0052 helpers (motet.apply/motet.join).

Dependencies:
- motet_sdk: command decorator and MotetContext typing
- pydantic: structured command inputs

Usage:
Called by get-news.fetch_articles using motet.apply with per-source inputs.

Notes:
- Returns structured success/failure payloads instead of raising so pipeline
  aggregation can proceed even when some sources fail.
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, WorkerCapability, motet


class FetchSourceData(BaseCommandData):
    """Input for get-news.fetch_source."""

    topic: str = Field(..., description="Topic being aggregated")
    url: str = Field(..., description="Source URL to fetch")
    kind: str = Field(default="search", description="Source classification")
    fetch_tool_name: str = Field(
        default="core.http_get_browser",
        description="Browser-capable tool/workflow name to execute",
    )
    max_chars: int = Field(
        default=3500,
        ge=200,
        le=12000,
        description="Maximum characters of extracted content",
    )
    request_timeout_seconds: float = Field(
        default=40.0,
        ge=5.0,
        le=120.0,
        description="Per-source browser fetch timeout in seconds",
    )


def _extract_field(result: Dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in result and result[name]:
            return result[name]
    return None


def _extract_content(result: Dict[str, Any], max_chars: int) -> str:
    content = _extract_field(
        result,
        "main_content",
        "content",
        "text",
        "markdown",
        "body",
        "summary",
        "page_text",
    )
    if not content:
        content = str(result)
    return str(content)[:max_chars]


@motet.command(
    timeout_seconds=55,
    required_capabilities=[WorkerCapability.TOOL_EXECUTION],
)
def fetch_source(data: FetchSourceData, motet: MotetContext) -> Dict[str, Any]:
    """Fetch and normalize one source URL."""
    try:
        tool_result = motet.tools.execute(
            data.fetch_tool_name,
            {
                "url": data.url,
                "max_chars": data.max_chars,
                "timeout": data.request_timeout_seconds,
                "include_links": True,
            },
        )
        if not isinstance(tool_result, dict):
            tool_result = {"raw_result": tool_result}

        links_raw = _extract_field(tool_result, "links") or []
        article = {
            "url": data.url,
            "title": _extract_field(tool_result, "title", "page_title") or data.url,
            "published_at": _extract_field(
                tool_result,
                "published_at",
                "published",
                "date",
                "timestamp",
            ),
            "source_kind": data.kind,
            "content": _extract_content(tool_result, data.max_chars),
            "links": links_raw[:50] if isinstance(links_raw, list) else [],
        }
        return {
            "ok": True,
            "url": data.url,
            "source_kind": data.kind,
            "article": article,
            "tool_name": data.fetch_tool_name,
        }
    except Exception as exc:
        return {
            "ok": False,
            "url": data.url,
            "source_kind": data.kind,
            "error": str(exc),
            "tool_name": data.fetch_tool_name,
        }
