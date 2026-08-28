"""
Motet SDK - Workflow Pipeline Example Source Discovery Command

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-03-17

Description:
Discover candidate news source URLs for a topic as step one of the
get-news.news_aggregation example workflow.

Dependencies:
- motet_sdk: command decorator and MotetContext typing
- pydantic: structured command inputs

Usage:
Call via command execution:
  get-news.discover_sources(topic="AI regulation", max_sources=6)

Notes:
- Uses a curated source list to keep the example deterministic and safe.
- Returns structured metadata consumed by downstream workflow steps.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List
from urllib.parse import quote_plus

from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, motet


# Front pages are fetched directly for "top headlines" requests so the browser
# tool retrieves actual article listings rather than search result pages.
FRONT_PAGES = [
    "https://apnews.com/",
    "https://www.bbc.com/news",
    "https://www.reuters.com/",
    "https://www.theguardian.com/international",
    "https://www.npr.org/sections/news/",
    "https://www.aljazeera.com/",
]

# Search templates are used when the caller provides a specific topic.
SEARCH_TEMPLATES = [
    "https://apnews.com/search?q={query}",
    "https://www.bbc.com/search?q={query}",
    "https://www.reuters.com/site-search/?query={query}",
    "https://www.theguardian.com/search?q={query}",
    "https://www.npr.org/search?query={query}",
    "https://www.aljazeera.com/search/{query}",
]

_TOP_HEADLINES_TOPICS = {"top headlines", "headlines", "news", "latest news", ""}


def _is_top_headlines(topic: str) -> bool:
    return topic.lower().strip() in _TOP_HEADLINES_TOPICS


class DiscoverSourcesData(BaseCommandData):
    """Input for get-news.discover_sources."""

    topic: str = Field(
        default="top headlines",
        description="Topic to aggregate news for (defaults to top headlines)",
    )
    max_sources: int = Field(
        default=6,
        ge=1,
        le=20,
        description="Maximum number of source URLs to return",
    )


@motet.command(timeout_seconds=30)
def discover_sources(data: DiscoverSourcesData, motet: MotetContext) -> Dict[str, Any]:
    """Build a deterministic list of source URLs for the given topic.

    Uses news outlet front pages for generic "top headlines" requests so the
    browser tool retrieves actual article listings. Uses search-result pages
    for specific topics.
    """
    topic = (data.topic or "").strip()
    # If workflow templating leaves unresolved placeholders (e.g. "{{topic}}"), fall back.
    if not topic or re.fullmatch(r"\{+\s*topic\s*\}+", topic):
        topic = "top headlines"

    if _is_top_headlines(topic):
        source_pool = [(u, "front_page") for u in FRONT_PAGES]
    else:
        query = quote_plus(topic)
        source_pool = [(t.format(query=query), "search") for t in SEARCH_TEMPLATES]

    urls_seen: set = set()
    sources: List[Dict[str, str]] = []
    for url, kind in source_pool:
        if url not in urls_seen:
            urls_seen.add(url)
            sources.append({"url": url, "kind": kind})
        if len(sources) >= data.max_sources:
            break

    return {
        "topic": topic,
        "sources": sources,
        "count": len(sources),
        "bundle": "get-news",
        "task_id": motet.task_id,
    }
