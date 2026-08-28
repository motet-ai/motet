"""
Motet - Developer Docs Corpus Package

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Product-owned developer onboarding corpus used by the HTTP
    ``/api/v1/developer-docs`` API and the agent tool ``core.docs_read``.
    The HTTP list is grouped by the nav taxonomy; lexical search matches
    title and body. The agent tool stays allowlisted.

Usage:
    from motet.core.developer_docs import (
        list_all_docs,
        group_docs,
        search_docs,
        read_doc_text,
        read_agent_facing,
        AGENT_FACING_DOCS,
    )
"""

from .allowlist import AGENT_FACING_DOCS, AgentFacingDoc, agent_facing_ids, get_agent_facing_doc
from .corpus import (
    SAFE_DOC_PATTERN,
    DeveloperDocsError,
    DocMeta,
    DocNotAgentFacing,
    DocNotFound,
    DocsDirUnavailable,
    InvalidDocId,
    SectionNotFound,
    get_docs_dir,
    is_safe_doc_id,
    list_agent_facing,
    list_all_docs,
    read_agent_facing,
    read_doc_text,
)
from .search import SearchHit, search_docs
from .taxonomy import (
    NAV_ENTRIES,
    NAV_SECTIONS,
    OTHER_SECTION,
    NavGroup,
    NavSection,
    group_docs,
    nav_doc_ids,
    nav_section_id,
)

__all__ = [
    "AGENT_FACING_DOCS",
    "AgentFacingDoc",
    "SAFE_DOC_PATTERN",
    "DeveloperDocsError",
    "DocMeta",
    "DocNotAgentFacing",
    "DocNotFound",
    "DocsDirUnavailable",
    "InvalidDocId",
    "SectionNotFound",
    "agent_facing_ids",
    "get_agent_facing_doc",
    "get_docs_dir",
    "group_docs",
    "is_safe_doc_id",
    "list_agent_facing",
    "list_all_docs",
    "nav_doc_ids",
    "nav_section_id",
    "read_agent_facing",
    "read_doc_text",
    "search_docs",
    "SearchHit",
    "NAV_ENTRIES",
    "NAV_SECTIONS",
    "OTHER_SECTION",
    "NavGroup",
    "NavSection",
]
