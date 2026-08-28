"""
Motet SDK - Workflow Pipeline Example Digest Command

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-03-17

Description:
Generate a concise markdown digest from clustered stories for the final step
of the get-news.news_aggregation example workflow.

Dependencies:
- motet_sdk: command decorator and MotetContext typing
- pydantic: structured command inputs

Usage:
Call via command execution:
  get-news.build_digest(topic="AI regulation", clusters=[...], max_items=5)

Notes:
- Designed for predictable output in an educational example bundle.
- Keeps both machine-readable data and human-readable markdown.
"""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field

from motet_sdk import BaseCommandData, MotetContext, motet


class ClusterItem(BaseModel):
    """Cluster payload item."""

    cluster_id: str = Field(..., description="Cluster identifier")
    headline: str = Field(default="", description="Representative headline")
    keywords: List[str] = Field(default_factory=list, description="Top cluster keywords")
    items: List[Dict[str, Any]] = Field(default_factory=list, description="Articles in the cluster")
    size: int = Field(default=0, description="Number of clustered articles")


class BuildDigestData(BaseCommandData):
    """Input for get-news.build_digest."""

    topic: str = Field(..., description="Topic being aggregated")
    clusters: List[ClusterItem] = Field(default_factory=list, description="Clustered stories")
    include_source_links: bool = Field(
        default=True,
        description="Include source links under each digest item",
    )
    max_items: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum number of digest items to render",
    )


_KNOWN_OUTLETS: Dict[str, str] = {
    "apnews.com": "AP News",
    "bbc.com": "BBC News",
    "reuters.com": "Reuters",
    "theguardian.com": "The Guardian",
    "npr.org": "NPR",
    "news.ycombinator.com": "Hacker News",
    "aljazeera.com": "Al Jazeera",
    "nytimes.com": "The New York Times",
    "washingtonpost.com": "Washington Post",
    "wsj.com": "Wall Street Journal",
    "ft.com": "Financial Times",
    "politico.com": "Politico",
    "axios.com": "Axios",
    "thehill.com": "The Hill",
}


def _source_name(url: str) -> str:
    """Return a short, human-readable outlet name from an article URL."""
    try:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or url).lstrip("www.")
    except Exception:
        host = url
    return _KNOWN_OUTLETS.get(host, host)


def _cluster_attribution(cluster: ClusterItem) -> str:
    """Return a source attribution string like 'via AP News, BBC News, Reuters'."""
    if not cluster.items:
        return "No sources"
    seen: set = set()
    source_names = []
    for item in cluster.items:
        name = _source_name(item.get("url", ""))
        if name not in seen:
            seen.add(name)
            source_names.append(name)
    if len(source_names) == 1:
        return f"via {source_names[0]}"
    return "via " + ", ".join(source_names[:4])


def _cluster_description(cluster: ClusterItem) -> str:
    """Extract a brief description from the cluster's articles.

    Looks for the _description field injected by cluster_articles, or
    falls back to the first substantive line from article content.
    """
    for item in cluster.items:
        desc = item.get("_description", "")
        if desc and len(desc) > 30:
            return desc
    return ""


@motet.command(timeout_seconds=30)
def build_digest(data: BuildDigestData, motet: MotetContext) -> Dict[str, Any]:
    """Build digest markdown and structured summary objects from clusters."""
    ranked = sorted(data.clusters, key=lambda c: c.size, reverse=True)[: data.max_items]

    digest_items: List[Dict[str, Any]] = []
    lines: List[str] = [f"# News Digest: {data.topic}", ""]

    if not ranked:
        lines.extend(["No stories were clustered for this topic.", ""])

    for idx, cluster in enumerate(ranked, start=1):
        attribution = _cluster_attribution(cluster)
        description = _cluster_description(cluster)
        sources = []
        seen_urls = set()
        for item in cluster.items:
            url = item.get("url")
            if url and url not in seen_urls:
                seen_urls.add(url)
                sources.append({"title": _source_name(url), "url": url})

        digest_item = {
            "rank": idx,
            "cluster_id": cluster.cluster_id,
            "headline": cluster.headline,
            "summary": description or attribution,
            "source_count": len(sources),
            "sources": sources,
        }
        digest_items.append(digest_item)

        lines.append(f"## {idx}. {cluster.headline}")
        if description:
            lines.append(f"{description}")
            lines.append(f"*{attribution}*")
        else:
            lines.append(f"*{attribution}*")
        if data.include_source_links and sources:
            link_parts = [f"[{s['title']}]({s['url']})" for s in sources[:4]]
            lines.append("  " + " · ".join(link_parts))
        lines.append("")

    markdown = "\n".join(lines).strip() + "\n"

    # A short preview surfaced into the LLM conversation history by
    # format_workflow_steps so the model sees the digest is complete and can
    # present it directly.  extract_text_from_mcp_result reads the "result"
    # key first; the formatter truncates at ~200 chars, so we cap at 190.
    if digest_items:
        top_headlines = [item["headline"] for item in digest_items[:3]]
        preview = " | ".join(h[:55] + "…" if len(h) > 55 else h for h in top_headlines)
        short_result = f"Digest ready ({len(digest_items)} stories): {preview}"
    else:
        short_result = "Digest ready: no stories were clustered for this topic"
    if len(short_result) > 190:
        short_result = short_result[:187] + "…"

    return {
        "topic": data.topic,
        "digest_markdown": markdown,
        "digest_items": digest_items,
        "item_count": len(digest_items),
        "result": short_result,
        "bundle": "get-news",
        "task_id": motet.task_id,
    }
