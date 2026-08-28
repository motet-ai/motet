"""
Motet - Developer Docs Nav Taxonomy

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Exclusive nav grouping for the human developer-docs surface. Filename
    numbers stay as stable ids; this map is the reading order and the
    left-rail sections in the ops dashboard. Each numbered page appears
    in exactly one section. Unmapped files fall through to Other so new
    pages are visible before they are filed.

Dependencies:
    - dataclasses: Frozen section and group records
    - motet.core.developer_docs.corpus.DocMeta: Filesystem list entries

Usage:
    from motet.core.developer_docs.taxonomy import group_docs, NAV_SECTIONS

    groups = group_docs(list_all_docs())
    for group in groups:
        print(group.section_title, [item.id for item in group.items])

Notes:
    - Home is the docs landing page (``00-landing-page``). The dashboard
      rail heading is that entry; it is not a collapse group.
    - Start / Concepts / Build / Runtime / State / Operate / Surfaces /
      Guides is the rest of the product nav. The onboarding README uses
      the same buckets.
    - Do not import this module from corpus.py (filesystem listing stays
      independent of nav order).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from .corpus import DocMeta


@dataclass(frozen=True)
class NavSection:
    """One exclusive docs-rail group."""

    id: str
    title: str


@dataclass(frozen=True)
class NavGroup:
    """A nav section plus the corpus entries that belong in it, in rail order."""

    section_id: str
    section_title: str
    items: Tuple[DocMeta, ...]


NAV_SECTIONS: Tuple[NavSection, ...] = (
    NavSection("home", "Home"),
    NavSection("start", "Start"),
    NavSection("concepts", "Concepts"),
    NavSection("build", "Build"),
    NavSection("runtime", "Runtime"),
    NavSection("state", "State"),
    NavSection("operate", "Operate"),
    NavSection("surfaces", "Surfaces"),
    NavSection("guides", "Guides"),
)

OTHER_SECTION = NavSection("other", "Other")

# (doc_id, section_id) in display order within each section.
NAV_ENTRIES: Tuple[Tuple[str, str], ...] = (
    ("00-landing-page", "home"),
    ("04-quick-start-guide", "start"),
    ("05-core-concepts-overview", "start"),
    ("15-building-your-first-command", "start"),
    ("01-operating-system-for-ai-agents", "concepts"),
    ("02-why-motet", "concepts"),
    ("03-what-motet-can-do", "concepts"),
    ("06-design-principles", "concepts"),
    ("15a-your-first-bundle", "build"),
    ("15b-bundle-scoping-and-visibility", "build"),
    ("16-command-composition-patterns", "build"),
    ("17-building-workflows", "build"),
    ("21-tool-ecosystem", "build"),
    ("18-testing-strategies", "build"),
    ("07-distributed-command-system", "runtime"),
    ("07a-agent-loop", "runtime"),
    ("07b-conversations", "runtime"),
    ("10-reasoning", "runtime"),
    ("11-workflow-system", "runtime"),
    ("08-worker-system-routing", "runtime"),
    ("08a-worker-targeting-guide", "runtime"),
    ("09-mcp-integration", "runtime"),
    ("09b-mcp-oauth-credentials", "runtime"),
    ("09a-canonical-llm-protocol", "runtime"),
    ("03a-supported-models", "runtime"),
    ("19-concurrency-primitives", "runtime"),
    ("12-scheduled-commands", "runtime"),
    ("13-streaming-responses", "runtime"),
    ("20-memory-management", "state"),
    ("20a-artifacts-and-multimodal-context", "state"),
    ("14-local-development-setup", "operate"),
    ("22-security-multi-tenancy", "operate"),
    ("23-observability-debugging", "operate"),
    ("29-configuration-reference", "operate"),
    ("30-troubleshooting-guide", "operate"),
    ("36-chat-explorer", "surfaces"),
    ("28-api-reference", "surfaces"),
    ("37-motet-cli-reference", "surfaces"),
    ("38-sdk-reference", "surfaces"),
    ("39-extending-the-cli", "surfaces"),
    ("24-advanced-concepts", "guides"),
    ("25-common-patterns", "guides"),
    ("26-example-bundles", "guides"),
    ("27-best-practices", "guides"),
    ("31-architecture-guide", "guides"),
    ("33-project-structure", "guides"),
    ("34-resources-links", "guides"),
    ("32-contributing-guide", "guides"),
)


def _validate_nav() -> None:
    seen: set[str] = set()
    section_ids = {section.id for section in NAV_SECTIONS}
    for doc_id, section_id in NAV_ENTRIES:
        if doc_id in seen:
            raise ValueError(f"Duplicate nav entry: {doc_id}")
        if section_id not in section_ids:
            raise ValueError(f"Unknown section {section_id!r} for {doc_id}")
        seen.add(doc_id)


_validate_nav()


def nav_doc_ids() -> frozenset[str]:
    """Return every doc id assigned to a named section."""
    return frozenset(doc_id for doc_id, _ in NAV_ENTRIES)


def nav_section_id(doc_id: str) -> str:
    """Return the section id for ``doc_id``, or ``other`` when unmapped."""
    for mapped_id, section_id in NAV_ENTRIES:
        if mapped_id == doc_id:
            return section_id
    return OTHER_SECTION.id


def group_docs(items: Sequence[DocMeta]) -> List[NavGroup]:
    """Partition corpus entries into exclusive nav sections.

    Empty sections are omitted. Files not in ``NAV_ENTRIES`` append under
    Other, in the order ``list_all_docs`` returned them.
    """
    by_id: Dict[str, DocMeta] = {item.id: item for item in items}
    grouped: List[NavGroup] = []
    claimed: set[str] = set()

    for section in NAV_SECTIONS:
        section_items = tuple(
            by_id[doc_id]
            for doc_id, section_id in NAV_ENTRIES
            if section_id == section.id and doc_id in by_id
        )
        if not section_items:
            continue
        claimed.update(item.id for item in section_items)
        grouped.append(
            NavGroup(
                section_id=section.id,
                section_title=section.title,
                items=section_items,
            )
        )

    leftover = tuple(item for item in items if item.id not in claimed)
    if leftover:
        grouped.append(
            NavGroup(
                section_id=OTHER_SECTION.id,
                section_title=OTHER_SECTION.title,
                items=leftover,
            )
        )
    return grouped


__all__ = [
    "NAV_ENTRIES",
    "NAV_SECTIONS",
    "OTHER_SECTION",
    "NavGroup",
    "NavSection",
    "group_docs",
    "nav_doc_ids",
    "nav_section_id",
]
