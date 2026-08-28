"""
Motet - Developer Docs Lexical Search

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Title, heading, and body substring search over the numbered onboarding
    corpus. Used by ``GET /api/v1/developer-docs/search``. This is not
    artifact RAG and not hybrid ranking — every query token must appear
    in the document, and title / heading hits rank above body hits.

Dependencies:
    - re: Tokenize queries and collapse snippet whitespace
    - motet.core.developer_docs.corpus: List/read numbered markdown
    - motet.core.developer_docs.taxonomy: Section labels on hits

Usage:
    from motet.core.developer_docs.search import search_docs

    hits = search_docs("worker targeting")
    for hit in hits:
        print(hit.id, hit.snippet)

Notes:
    - The corpus is small enough to read on each query. Do not index these
      files into the artifact store.
    - Agents still use known-id ``core.docs_read``; this surface is for
      the human docs rail.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence

from .corpus import _iter_headings, list_all_docs
from .taxonomy import OTHER_SECTION, NAV_SECTIONS, nav_section_id

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_WS_RE = re.compile(r"\s+")
_MIN_QUERY_CHARS = 2
_MAX_RESULTS = 20
_SNIPPET_RADIUS = 56


@dataclass(frozen=True)
class SearchHit:
    """One lexical match against a numbered onboarding doc."""

    id: str
    title: str
    section: str
    section_title: str
    snippet: str
    heading: Optional[str]
    score: int


def _tokens(text: str) -> List[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _section_title(section_id: str) -> str:
    for section in NAV_SECTIONS:
        if section.id == section_id:
            return section.title
    if section_id == OTHER_SECTION.id:
        return OTHER_SECTION.title
    return section_id


def _haystack(text: str) -> str:
    return (text or "").lower()


def _contains_all(haystack: str, tokens: Sequence[str]) -> bool:
    return all(token in haystack for token in tokens)


def _count_hits(haystack: str, tokens: Sequence[str]) -> int:
    return sum(haystack.count(token) for token in tokens)


def _snippet(body: str, tokens: Sequence[str]) -> str:
    lowered = body.lower()
    index = -1
    for token in tokens:
        found = lowered.find(token)
        if found >= 0 and (index < 0 or found < index):
            index = found
    if index < 0:
        compact = _WS_RE.sub(" ", body).strip()
        return compact[: _SNIPPET_RADIUS * 2]
    start = max(0, index - _SNIPPET_RADIUS)
    end = min(len(body), index + _SNIPPET_RADIUS)
    excerpt = _WS_RE.sub(" ", body[start:end]).strip()
    if start > 0:
        excerpt = f"…{excerpt}"
    if end < len(body):
        excerpt = f"{excerpt}…"
    return excerpt


def _first_heading_hit(
    markdown: str, tokens: Sequence[str], *, skip: Optional[str] = None
) -> Optional[str]:
    skip_h = _haystack(skip or "")
    headings = [
        title
        for _, _, title in _iter_headings(markdown)
        if _haystack(title) != skip_h
    ]
    for title in headings:
        if _contains_all(_haystack(title), tokens):
            return title
    for title in headings:
        if any(token in _haystack(title) for token in tokens):
            return title
    return None


def _score(title: str, headings: Sequence[str], body: str, tokens: Sequence[str]) -> int:
    title_h = _haystack(title)
    heading_h = " ".join(_haystack(heading) for heading in headings)
    body_h = _haystack(body)
    score = 0
    if _contains_all(title_h, tokens):
        score += 100
    score += 20 * _count_hits(title_h, tokens)
    score += 10 * min(_count_hits(heading_h, tokens), 8)
    score += min(_count_hits(body_h, tokens), 15)
    return score


def search_docs(query: str, *, limit: int = _MAX_RESULTS) -> List[SearchHit]:
    """Return lexical hits for ``query``, highest score first."""
    tokens = _tokens(query)
    if len((query or "").strip()) < _MIN_QUERY_CHARS or not tokens:
        return []

    hits: List[SearchHit] = []
    for meta in list_all_docs():
        try:
            markdown = meta.path.read_text(encoding="utf-8")
        except OSError:
            continue
        blob = f"{meta.title}\n{markdown}"
        if not _contains_all(_haystack(blob), tokens):
            continue
        headings = [title for _, _, title in _iter_headings(markdown)]
        section_id = nav_section_id(meta.id)
        hits.append(
            SearchHit(
                id=meta.id,
                title=meta.title,
                section=section_id,
                section_title=_section_title(section_id),
                snippet=_snippet(markdown, tokens),
                heading=_first_heading_hit(markdown, tokens, skip=meta.title),
                score=_score(meta.title, headings, markdown, tokens),
            )
        )

    hits.sort(key=lambda hit: (-hit.score, hit.title.lower()))
    return hits[: max(1, min(int(limit), _MAX_RESULTS))]


__all__ = ["SearchHit", "search_docs"]
