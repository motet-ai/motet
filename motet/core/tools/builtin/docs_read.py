"""
Motet - Developer Docs Read Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Built-in tool ``core.docs_read`` that lets an agent list and read the
    curated developer-onboarding corpus (same filesystem as
    ``GET /api/v1/developer-docs``). Reads are windowed so long tutorials do
    not fill the context. Product docs are not tenant artifacts.

Dependencies:
    - pydantic: Parameter schema
    - motet.core.developer_docs: Allowlist + filesystem corpus
    - motet.core.tools.protocol: ok/err envelopes
    - motet.core.tools.registry: ToolRegistry

Usage:
    core.docs_read()
    core.docs_read(doc_id="11-workflow-system", section="YAML structure")
    core.docs_read(doc_id="17-building-workflows", offset_chars=12000)

Notes:
    - Omit doc_id to list the agent-facing catalog.
    - Hybrid docs_search is a later slice; this tool is known-id + heading.
    - Non-allowlisted onboarding pages (welcome, marketing) are refused.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from ...developer_docs.corpus import (
    DocNotAgentFacing,
    DocNotFound,
    DocsDirUnavailable,
    InvalidDocId,
    SectionNotFound,
    read_agent_facing,
)
from ..protocol import err, ok
from ..registry import ToolRegistry

_DEFAULT_MAX_CHARS = 12_000


class DocsReadParams(BaseModel):
    """Parameters for core.docs_read."""

    doc_id: Optional[str] = Field(
        default=None,
        description=(
            "Onboarding doc id (filename without .md), for example "
            "'11-workflow-system' or '17-building-workflows'. Omit to list "
            "the agent-facing catalog."
        ),
    )
    section: Optional[str] = Field(
        default=None,
        description=(
            "Optional heading to slice (exact title, slug, or substring), "
            "for example 'YAML structure' or 'Runtime-authored workflows'."
        ),
    )
    offset_chars: int = Field(
        default=0,
        ge=0,
        description="Character offset into the selected page or section.",
    )
    max_chars: Optional[int] = Field(
        default=None,
        ge=1,
        le=80_000,
        description=f"Maximum characters to return. Defaults to {_DEFAULT_MAX_CHARS}.",
    )


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """List or read curated developer onboarding docs."""
    try:
        parsed = DocsReadParams(**(params or {}))
    except Exception as exc:
        return err(f"validation error: {exc}")

    try:
        payload = read_agent_facing(
            doc_id=parsed.doc_id,
            section=parsed.section,
            offset_chars=parsed.offset_chars,
            max_chars=parsed.max_chars,
        )
    except InvalidDocId as exc:
        return err(str(exc))
    except DocNotAgentFacing as exc:
        return err(str(exc))
    except DocNotFound as exc:
        return err(str(exc))
    except DocsDirUnavailable as exc:
        return err(str(exc))
    except SectionNotFound as exc:
        available = "; ".join(exc.available) if exc.available else "(no headings)"
        return err(
            f"{exc}. Available headings: {available}",
            meta={"doc_id": exc.doc_id, "available_sections": list(exc.available)},
        )
    except Exception as exc:
        return err(f"docs_read failed: {exc}")

    return ok(payload)


def register(registry: ToolRegistry) -> None:
    """Register core.docs_read."""
    registry.register(
        name="core.docs_read",
        description=(
            "Read curated Motet developer documentation (workflow YAML contract, "
            "placeholders, runtime user.* register). Omit doc_id to list the "
            "catalog. Pass doc_id (e.g. 11-workflow-system, 17-building-workflows) "
            "and optional section heading; use offset_chars/max_chars to page. "
            "Product docs — not tenant artifacts. Prefer this over stuffing "
            "manuals into other tool descriptions."
        ),
        func=run,
        tool_schema=DocsReadParams,
        triggers=["docs_read:"],
        category="system",
        contextualize_observation=False,
        default_timeout_seconds=15.0,
        suggested_max_calls=4,
        cost_class="low",
        keywords=[
            "docs",
            "documentation",
            "onboarding",
            "workflow yaml",
            "workflow_builder",
            "how to",
            "authoring",
            "placeholders",
            "required_inputs",
        ],
        required_capabilities=["tool_execution"],
    )


__all__ = ["DocsReadParams", "register", "run"]
