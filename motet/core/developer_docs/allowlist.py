"""
Motet - Agent-Facing Developer Docs Allowlist

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Curated subset of ``docs/developer_onboarding/`` that agents may read via
    ``core.docs_read``. The HTTP developer-docs API still lists the full
    numbered corpus for humans; this allowlist is the product-owned agent
    catalog (workflow-authoring pack first). Not every onboarding page is
    agent-facing — marketing, welcome, and contributor pages stay out.

Dependencies:
    - dataclasses: Frozen catalog entries

Usage:
    from motet.core.developer_docs.allowlist import (
        AGENT_FACING_DOCS,
        agent_facing_ids,
        get_agent_facing_doc,
    )

    ids = agent_facing_ids()
    entry = get_agent_facing_doc("11-workflow-system")

Notes:
    - Keep this pack small. Hybrid ``docs_search`` is a later slice; known
      ids plus optional section headings are enough for workflow_builder.
    - Suggested sections must match headings in the markdown files (see
      ``corpus._heading_matches``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class AgentFacingDoc:
    """One curated document agents may read by id."""

    id: str
    summary: str
    suggested_sections: Tuple[str, ...] = ()


AGENT_FACING_DOCS: Tuple[AgentFacingDoc, ...] = (
    AgentFacingDoc(
        id="11-workflow-system",
        summary=(
            "Workflow model, bundle YAML contract, placeholders / dependencies, "
            "and runtime user.* register path used by core.workflow_builder."
        ),
        suggested_sections=(
            "YAML structure",
            "Runtime-authored workflows (`user.*`)",
        ),
    ),
    AgentFacingDoc(
        id="17-building-workflows",
        summary=(
            "Workflow authoring tutorial and runtime register via API, CLI, "
            "or core.workflow_builder."
        ),
        suggested_sections=("Runtime register via API or CLI",),
    ),
)


def agent_facing_ids() -> frozenset[str]:
    """Return the set of allowlisted doc ids."""
    return frozenset(doc.id for doc in AGENT_FACING_DOCS)


def get_agent_facing_doc(doc_id: str) -> Optional[AgentFacingDoc]:
    """Return the catalog entry for ``doc_id``, or None if not allowlisted."""
    for doc in AGENT_FACING_DOCS:
        if doc.id == doc_id:
            return doc
    return None


__all__ = [
    "AGENT_FACING_DOCS",
    "AgentFacingDoc",
    "agent_facing_ids",
    "get_agent_facing_doc",
]
