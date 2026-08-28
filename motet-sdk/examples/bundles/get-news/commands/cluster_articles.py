"""
Motet SDK - Workflow Pipeline Example Clustering Command

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-03-17

Description:
Cluster fetched articles into rough story groups based on keyword overlap so
the digest step can summarize by cluster.  Headline extraction prioritizes
structured links from the browser tool (content-area links sorted by text
length) over raw content line-parsing heuristics.

Dependencies:
- motet_sdk: command decorator and MotetContext typing
- pydantic: structured command inputs

Usage:
Call via command execution:
  get-news.cluster_articles(topic="AI", articles=[...], min_overlap_terms=4)

Notes:
- When articles include structured links (from core.http_get_browser with
  include_links=True), headlines and descriptions are extracted from the
  link text, which is significantly cleaner than raw page content.
- Falls back to content-based heuristics when links are absent.
- Production quality clustering should use embeddings or semantic search.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Set

from pydantic import BaseModel, Field

from motet_sdk import BaseCommandData, MotetContext, motet


STOP_WORDS = {
    # English function words
    "the", "and", "for", "that", "with", "this", "from", "have", "about",
    "http", "https", "www", "com", "news", "org", "net",
    # Web / aggregator UI terms
    "hide", "comments", "points", "login", "submit", "show", "ask",
    "past", "read", "watch", "live", "more", "also", "subscribe",
    "section", "article", "headline", "headlines", "story",
    # Time references embedded in content
    "ago", "mins", "min", "hours", "hour", "days", "day", "week", "year",
    # High-frequency English
    "says", "said", "say", "new", "first", "last", "top", "get", "out",
    "has", "had", "was", "were", "been", "are", "not", "but", "all",
    "can", "its", "who", "what", "when", "where", "how", "why",
    "will", "may", "would", "could", "should", "one", "two", "three",
    # Outlet / platform names that appear in scraped content
    "reuters", "associated", "press", "bbc", "guardian", "hacker",
    "breaking", "latest", "update", "updates", "coverage",
}


class LinkItem(BaseModel):
    """Structured link extracted by the browser tool."""

    url: str = Field(default="", description="Link URL")
    text: str = Field(default="", description="Link visible text")


class ArticleItem(BaseModel):
    """Normalized article item."""

    url: str = Field(..., description="Article URL")
    title: str = Field(default="", description="Article title")
    content: str = Field(default="", description="Extracted content")
    published_at: str | None = Field(default=None, description="Publication timestamp if available")
    source_kind: str = Field(default="search", description="Source classification")
    links: List[LinkItem] = Field(default_factory=list, description="Structured links from browser")


class ClusterArticlesData(BaseCommandData):
    """Input for get-news.cluster_articles."""

    topic: str = Field(..., description="Topic being aggregated")
    articles: List[ArticleItem] = Field(default_factory=list, description="Fetched article list")
    min_overlap_terms: int = Field(
        default=4,
        ge=1,
        le=10,
        description="Minimum keyword overlap needed to join an existing cluster (applies to search articles only)",
    )


def _keywords(text: str) -> Set[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text.lower())
    return {w for w in words if w not in STOP_WORDS}


_SKIP_PREFIXES = (
    "live",
    "breaking",
    "more coverage",
    "most read",
    "most watched",
    "most popular",
    "also in",
    "subscribe",
    "sign up",
    "sign in",
    "you are viewing",
    "you are on",
    "you are at",
    "this is a modal",
    "this video is",
    "this video may",
    "error code:",
    "featured content",
    "close modal",
    "session id:",
    "player element",
    "list ",
    "published ",
    "item ",
)

_CREDIT_FRAGMENTS = (
    "getty images",
    "via getty",
    "/afp",
    "ap photo",
    "/reuters",
    "/epa",
)


def _is_skip_line(line: str) -> bool:
    """Return True if the line is boilerplate, not a real headline."""
    lower = line.lower()
    # Known non-headline prefixes
    for prefix in _SKIP_PREFIXES:
        if lower.startswith(prefix):
            return True
    # Timestamp patterns like "2 MINS AGO", "40 min ago"
    if re.match(r"^\d+\s*(mins?|hrs?|hours?)\s*(ago)?", lower):
        return True
    return False


def _is_credit_line(line: str) -> bool:
    """Return True if the line looks like a photo/wire credit."""
    lower = line.lower()
    return any(frag in lower for frag in _CREDIT_FRAGMENTS)


_CAPTION_FRAGMENTS = (
    "at the site of",
    "at the grave of",
    "at the graveside",
    "in this photo",
    "in a photo",
    "in an image",
    "at a press conference",
    "at the scene",
    "walks past",
    "stands near",
    "stands in front",
    "holds a",
    "hold a",
    "holding a",
    "is seen",
    "are seen",
    "smoke rises",
    "smoke and flames",
    "a view of",
    "an aerial view",
    "a man walks",
    "a woman walks",
    "people walk",
    "file photo",
    "handout photo",
    "displaced ",
    "emergency workers at",
    "rescue team",
    "destruction after",
)


def _is_caption_line(line: str) -> bool:
    """Return True if the line looks like an image/photo caption."""
    lower = line.lower()
    return any(frag in lower for frag in _CAPTION_FRAGMENTS)


def _clean_link_text(text: str) -> str:
    """Strip HTML artifacts and normalize whitespace from link text."""
    cleaned = re.sub(r"<[^>]+>", "", text).strip()
    return re.sub(r"\s+", " ", cleaned)


def _is_headline_candidate(text: str, min_len: int = 25, max_len: int = 180) -> bool:
    """Shared filter: return True if text could be a news headline."""
    if len(text) < min_len or len(text) > max_len:
        return False
    if _is_skip_line(text):
        return False
    if _is_credit_line(text):
        return False
    if _is_caption_line(text):
        return False
    if text == text.upper() and len(text) > 3:
        return False
    if text.count("|") > 1:
        return False
    if not text[0].isupper():
        return False
    if text.count("/") > 3 or text.count("=") > 2:
        return False
    return True


def _extract_headline_from_links(links: list, fallback: str) -> str:
    """Extract the best headline from structured link data.

    Links are pre-sorted by the browser tool: content-area links first,
    then other links, both sorted longest-text-first.  This naturally
    surfaces article headlines before navigation text.
    """
    for link in links:
        raw = link.text if isinstance(link, LinkItem) else (link.get("text") or "")
        text = _clean_link_text(raw)
        if not text:
            continue
        if _is_headline_candidate(text):
            return text
    return fallback


def _extract_description_from_links(links: list, headline: str) -> str:
    """Extract a brief description from links, skipping the headline itself."""
    headline_lower = headline.lower().strip()
    for link in links:
        raw = link.text if isinstance(link, LinkItem) else (link.get("text") or "")
        text = _clean_link_text(raw)
        if not text:
            continue
        if text.lower().strip() == headline_lower:
            continue
        if len(headline_lower) > 20 and text[:20].lower() == headline_lower[:20]:
            continue
        if _is_headline_candidate(text, min_len=30, max_len=300):
            return text[:200]
    return ""


def _extract_lead_headline(content: str, fallback: str) -> str:
    """Pull the first plausible news headline from scraped page content.

    Content-based fallback used when no structured links are available.
    Walks the content line-by-line and returns the first line that looks like
    a real story headline — skipping timestamps, all-caps nav labels, photo
    credits, image captions, and other boilerplate.
    """
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not _is_headline_candidate(line):
            continue
        return line
    return fallback


def _headline_keywords(headline: str) -> Set[str]:
    """Extract keywords from a headline only (short text, stricter filtering)."""
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", headline.lower())
    return {w for w in words if w not in STOP_WORDS and len(w) > 2}


def _merge_front_page_clusters(
    clusters: List[Dict[str, Any]], min_headline_overlap: int = 3
) -> List[Dict[str, Any]]:
    """Merge front-page clusters whose lead headlines cover the same story.

    Compares HEADLINE keywords (not full content) to avoid collapsing
    everything into one cluster.  Two clusters merge when their headlines
    share >= min_headline_overlap non-stop words.
    """
    merged: List[Dict[str, Any]] = []
    used: List[bool] = [False] * len(clusters)

    for i in range(len(clusters)):
        if used[i]:
            continue
        group = dict(clusters[i])
        group["items"] = list(group["items"])
        group["keywords"] = set(group["keywords"])
        group["headline_kw"] = _headline_keywords(group["headline"])

        for j in range(i + 1, len(clusters)):
            if used[j]:
                continue
            other_kw = _headline_keywords(clusters[j]["headline"])
            overlap = len(group["headline_kw"].intersection(other_kw))
            if overlap >= min_headline_overlap:
                used[j] = True
                group["items"].extend(clusters[j]["items"])
                group["keywords"].update(clusters[j]["keywords"])
                group["headline_kw"].update(other_kw)

        group.pop("headline_kw", None)
        merged.append(group)

    return merged


def _extract_description(content: str, headline: str, max_len: int = 200) -> str:
    """Extract a brief description from article content, skipping the headline itself.

    Content-based fallback used when link-based description extraction
    doesn't produce a result.
    """
    headline_lower = headline.lower().strip()
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not _is_headline_candidate(line, min_len=30, max_len=300):
            continue
        if line.lower().strip() == headline_lower:
            continue
        if len(headline_lower) > 20 and line[:20].lower() == headline_lower[:20]:
            continue
        return line[:max_len]
    return ""


@motet.command(timeout_seconds=45)
def cluster_articles(data: ClusterArticlesData, motet: MotetContext) -> Dict[str, Any]:
    """Cluster articles by source type, then merge by headline similarity.

    Strategy:
    - Front-page sources are initially given one cluster each with the lead
      headline extracted from the scraped content.
    - A merge pass then combines front-page clusters whose headlines cover the
      same story (compared by headline keywords, not full content, to avoid
      collapsing everything).
    - Search / article results are grouped by keyword overlap so stories about
      the same specific topic from different sources are surfaced together.
    """
    clusters: List[Dict[str, Any]] = []

    front_page_articles = [a for a in data.articles if a.source_kind == "front_page"]
    search_articles = [a for a in data.articles if a.source_kind != "front_page"]

    # ── Front pages: one cluster per outlet, then merge similar headlines ───
    fp_clusters: List[Dict[str, Any]] = []
    for article in front_page_articles:
        links = article.links or []
        if links:
            lead = _extract_headline_from_links(links, article.title)
            desc = _extract_description_from_links(links, lead) or _extract_description(
                article.content, lead
            )
        else:
            lead = _extract_lead_headline(article.content, article.title)
            desc = _extract_description(article.content, lead)
        keyset = _keywords(f"{lead} {article.content}")
        item = article.model_dump()
        item["_lead_headline"] = lead
        item["_description"] = desc
        fp_clusters.append(
            {
                "keywords": keyset,
                "headline": lead,
                "items": [item],
            }
        )

    clusters.extend(_merge_front_page_clusters(fp_clusters))

    # ── Search / article results: keyword-overlap clustering ─────────────────
    for article in search_articles:
        keyset = _keywords(f"{article.title} {article.content}")
        best_idx = -1
        best_overlap = 0

        for idx, cluster in enumerate(clusters):
            if cluster["items"][0].get("source_kind") == "front_page":
                continue
            overlap = len(keyset.intersection(cluster["keywords"]))
            if overlap > best_overlap:
                best_overlap = overlap
                best_idx = idx

        if best_idx >= 0 and best_overlap >= data.min_overlap_terms:
            clusters[best_idx]["items"].append(article.model_dump())
            clusters[best_idx]["keywords"].update(keyset)
        else:
            links = article.links or []
            if links:
                lead = _extract_headline_from_links(links, article.title)
            else:
                lead = _extract_lead_headline(article.content, article.title)
            clusters.append({"keywords": set(keyset), "headline": lead, "items": [article.model_dump()]})

    # ── Normalize ────────────────────────────────────────────────────────────
    normalized_clusters: List[Dict[str, Any]] = []
    for idx, cluster in enumerate(clusters, start=1):
        keywords_sorted = sorted(cluster["keywords"])
        headline = (
            cluster.get("headline")
            or cluster["items"][0].get("title")
            or f"{data.topic} story {idx}"
        )
        normalized_clusters.append(
            {
                "cluster_id": f"story-{idx}",
                "headline": headline,
                "keywords": keywords_sorted[:10],
                "items": cluster["items"],
                "size": len(cluster["items"]),
            }
        )

    return {
        "topic": data.topic,
        "clusters": normalized_clusters,
        "cluster_count": len(normalized_clusters),
        "article_count": len(data.articles),
        "bundle": "get-news",
        "task_id": motet.task_id,
    }
