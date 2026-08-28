"""
Motet - Developer Documentation API

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Serves developer onboarding markdown files from docs/developer_onboarding/
    for the ops-dashboard. The list endpoint returns exclusive nav sections
    (Home, Start, Concepts, Build, Runtime, State, Operate, Surfaces, Guides)
    plus a flattened items array in the same order, and the Motet product
    version so the docs rail can label the catalog. Lexical search matches
    title and body. Path resolution and id safety live in
    ``motet.core.developer_docs``.

Dependencies:
    - fastapi: Web framework
    - pydantic: List and search response models
    - motet.core.developer_docs: Shared corpus, nav taxonomy, and lexical search
    - motet._version: Product version for the list response

Usage:
    app.include_router(developer_docs_router)
"""

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ...._version import get_version
from ....core.developer_docs.corpus import (
    DocNotFound,
    DocsDirUnavailable,
    InvalidDocId,
    list_all_docs,
    read_doc_text,
)
from ....core.developer_docs.search import search_docs
from ....core.developer_docs.taxonomy import group_docs

router = APIRouter(prefix="/api/v1/developer-docs", tags=["developer-docs"])


class DocItem(BaseModel):
    """Single developer doc entry for list response."""

    id: str = Field(
        ...,
        description="Doc id (filename without .md)",
        json_schema_extra={"example": "04-quick-start-guide"},
    )
    filename: str = Field(
        ...,
        description="Filename including .md",
        json_schema_extra={"example": "04-quick-start-guide.md"},
    )
    title: str = Field(
        ...,
        description="Display title from first heading or derived from id",
        json_schema_extra={"example": "Quick Start Guide"},
    )
    section: str = Field(
        ...,
        description="Exclusive nav section id (home, start, concepts, build, runtime, state, operate, surfaces, guides, or other)",
        json_schema_extra={"example": "start"},
    )


class DocSection(BaseModel):
    """One exclusive nav group and the docs that belong in it."""

    id: str = Field(
        ...,
        description="Section id",
        json_schema_extra={"example": "start"},
    )
    title: str = Field(
        ...,
        description="Section label for the docs rail",
        json_schema_extra={"example": "Start"},
    )
    items: List[DocItem] = Field(..., description="Docs in this section, in rail order")


class DocListResponse(BaseModel):
    """Grouped list of developer onboarding docs."""

    version: str = Field(
        ...,
        description="Motet product version for the docs this process is serving",
        json_schema_extra={"example": "0.1.0"},
    )
    items: List[DocItem] = Field(
        ...,
        description="Flattened docs in taxonomy order (Start first, then remaining sections)",
    )
    sections: List[DocSection] = Field(
        ...,
        description="Exclusive nav groups; empty sections are omitted; unmapped files appear under Other",
    )


@router.get(
    "",
    response_model=DocListResponse,
    summary="List developer onboarding docs",
    description=(
        "Returns developer onboarding documents grouped into exclusive nav "
        "sections (Home, Start, Concepts, Build, Runtime, State, Operate, "
        "Surfaces, Guides). ``items`` is the same list flattened in that order. "
        "Home is the landing page; the dashboard rail heading is that entry."
    ),
    responses={
        200: {
            "description": "Grouped onboarding docs",
            "content": {
                "application/json": {
                    "example": {
                        "version": "0.1.0",
                        "items": [
                            {
                                "id": "04-quick-start-guide",
                                "filename": "04-quick-start-guide.md",
                                "title": "Quick Start Guide",
                                "section": "start",
                            }
                        ],
                        "sections": [
                            {
                                "id": "start",
                                "title": "Start",
                                "items": [
                                    {
                                        "id": "04-quick-start-guide",
                                        "filename": "04-quick-start-guide.md",
                                        "title": "Quick Start Guide",
                                        "section": "start",
                                    }
                                ],
                            }
                        ],
                    }
                }
            },
        }
    },
)
def list_docs() -> DocListResponse:
    """Return onboarding docs grouped by the product nav taxonomy."""
    sections: List[DocSection] = []
    for group in group_docs(list_all_docs()):
        section_items = [
            DocItem(
                id=meta.id,
                filename=meta.filename,
                title=meta.title,
                section=group.section_id,
            )
            for meta in group.items
        ]
        sections.append(
            DocSection(id=group.section_id, title=group.section_title, items=section_items)
        )
    items = [item for section in sections for item in section.items]
    return DocListResponse(version=get_version(), items=items, sections=sections)


class SearchHitItem(BaseModel):
    """One lexical match against a numbered onboarding doc."""

    id: str = Field(
        ...,
        description="Doc id (filename without .md)",
        json_schema_extra={"example": "08a-worker-targeting-guide"},
    )
    title: str = Field(
        ...,
        description="Display title from first heading or derived from id",
        json_schema_extra={"example": "Worker Targeting Guide"},
    )
    section: str = Field(
        ...,
        description="Exclusive nav section id",
        json_schema_extra={"example": "runtime"},
    )
    section_title: str = Field(
        ...,
        description="Section label for the docs rail",
        json_schema_extra={"example": "Runtime"},
    )
    snippet: str = Field(
        ...,
        description="Whitespace-collapsed excerpt around the first query token",
        json_schema_extra={"example": "…route a command to a specific worker…"},
    )
    heading: Optional[str] = Field(
        None,
        description="First matching heading after the document title, when one exists",
        json_schema_extra={"example": "Targeting a worker"},
    )
    score: int = Field(
        ...,
        description="Lexical rank (title matches outrank heading and body matches)",
        json_schema_extra={"example": 121},
    )


class SearchResponse(BaseModel):
    """Lexical search hits for the onboarding corpus."""

    query: str = Field(
        ...,
        description="Normalized query that produced these hits",
        json_schema_extra={"example": "worker targeting"},
    )
    items: List[SearchHitItem] = Field(..., description="Hits, highest score first")


@router.get(
    "/search",
    response_model=SearchResponse,
    summary="Search developer onboarding docs",
    description=(
        "Lexical search over numbered onboarding markdown. Every query token "
        "must appear in the title or body. Queries shorter than two characters "
        "return no hits. Title and heading matches rank above body matches. "
        "This is not artifact RAG."
    ),
    responses={
        200: {
            "description": "Lexical hits",
            "content": {
                "application/json": {
                    "example": {
                        "query": "worker targeting",
                        "items": [
                            {
                                "id": "08a-worker-targeting-guide",
                                "title": "Worker Targeting Guide",
                                "section": "runtime",
                                "section_title": "Runtime",
                                "snippet": "…route a command to a specific worker…",
                                "heading": "Targeting a worker",
                                "score": 121,
                            }
                        ],
                    }
                }
            },
        }
    },
)
def search_developer_docs(
    q: str = Query(
        "",
        description="Search tokens (AND). Queries shorter than two characters return no hits.",
        max_length=200,
        json_schema_extra={"example": "worker targeting"},
    ),
) -> SearchResponse:
    """Return lexical hits for the onboarding corpus."""
    query = (q or "").strip()
    hits = search_docs(query)
    return SearchResponse(
        query=query,
        items=[
            SearchHitItem(
                id=hit.id,
                title=hit.title,
                section=hit.section,
                section_title=hit.section_title,
                snippet=hit.snippet,
                heading=hit.heading,
                score=hit.score,
            )
            for hit in hits
        ],
    )


@router.get(
    "/{doc_id}",
    summary="Get developer doc content",
    description="Returns raw markdown content for the given doc id.",
    responses={200: {"content": {"text/markdown": {}}}, 404: {"description": "Doc not found"}},
)
def get_doc(doc_id: str) -> Response:
    try:
        content = read_doc_text(doc_id)
    except InvalidDocId as exc:
        raise HTTPException(status_code=400, detail="Invalid doc id") from exc
    except DocsDirUnavailable as exc:
        raise HTTPException(status_code=404, detail="Developer docs not available") from exc
    except DocNotFound as exc:
        raise HTTPException(status_code=404, detail="Doc not found") from exc
    return Response(content=content, media_type="text/markdown; charset=utf-8")
