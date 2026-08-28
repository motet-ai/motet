"""
Motet - Developer Docs Corpus

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Filesystem access for product-owned developer onboarding markdown
    (``docs/developer_onboarding/``, override ``MOTET_DEVELOPER_DOCS_DIR``).
    Shared by the HTTP developer-docs API (full numbered corpus) and
    ``core.docs_read`` (allowlisted agent-facing subset, windowed reads).

    This is not artifact RAG: docs are versioned product source shipped with
    the runtime, not tenant-scoped uploads.

Dependencies:
    - os / pathlib: Docs root resolution
    - re: Safe id pattern and heading parse
    - structlog: Missing-dir and read-error logs
    - motet.core.developer_docs.allowlist: Agent-facing catalog

Usage:
    from motet.core.developer_docs.corpus import (
        list_all_docs,
        read_doc_text,
        list_agent_facing,
        read_agent_facing,
    )

    items = list_all_docs()
    text = read_doc_text("11-workflow-system")
    window = read_agent_facing(doc_id="11-workflow-system", section="YAML structure")

Notes:
    - Doc ids are filename stems matching ``NN-slug`` / ``NNa-slug``.
    - Reads resolve the file then require it stay under the docs root
      (path-traversal guard).
    - Document-frequency / hybrid ranking is out of scope; listing and
      known-id read are the MVP.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import structlog

from .allowlist import AGENT_FACING_DOCS, get_agent_facing_doc

logger = structlog.get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DEFAULT_DOCS_DIR = _REPO_ROOT / "docs" / "developer_onboarding"

# Numbered onboarding files only (e.g. 00-landing-page.md, 07a-agent-loop.md).
SAFE_DOC_PATTERN = re.compile(r"^(\d{2}[a-z]?-[a-z0-9-]+)\.md$")

_DEFAULT_MAX_CHARS = 12_000
_MAX_CHARS_CAP = 80_000


class DeveloperDocsError(Exception):
    """Base error for developer-docs corpus access."""


class InvalidDocId(DeveloperDocsError):
    """Doc id failed the safe filename pattern."""


class DocNotFound(DeveloperDocsError):
    """Doc id is well-formed but the file is missing."""


class DocsDirUnavailable(DeveloperDocsError):
    """Docs root does not exist on this runtime."""


class DocNotAgentFacing(DeveloperDocsError):
    """Doc exists (or would) but is not in the agent-facing allowlist."""


class SectionNotFound(DeveloperDocsError):
    """Requested heading was not found in the document."""

    def __init__(self, doc_id: str, section: str, available: Sequence[str]) -> None:
        self.doc_id = doc_id
        self.section = section
        self.available = list(available)
        super().__init__(f"Section {section!r} not found in {doc_id}")


@dataclass(frozen=True)
class DocMeta:
    """List entry for one onboarding markdown file."""

    id: str
    filename: str
    title: str
    path: Path


def get_docs_dir() -> Optional[Path]:
    """Return the resolved docs root if it exists, else None."""
    raw = os.environ.get("MOTET_DEVELOPER_DOCS_DIR")
    if raw:
        path = Path(raw)
        if not path.is_absolute():
            path = (_REPO_ROOT / path).resolve()
        else:
            path = path.resolve()
    else:
        path = _DEFAULT_DOCS_DIR.resolve()
    if not path.exists() or not path.is_dir():
        logger.warning("developer_docs_dir_missing", path=str(path))
        return None
    return path


def is_safe_doc_id(doc_id: str) -> bool:
    """True when ``doc_id`` matches the numbered onboarding filename stem."""
    return bool(SAFE_DOC_PATTERN.match(f"{doc_id}.md"))


def _title_from_first_line(file_path: Path) -> str:
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            first = handle.readline()
    except OSError:
        return ""
    line = (first or "").strip()
    if line.startswith("#"):
        return line.lstrip("#").strip()
    return ""


def _id_to_title(doc_id: str) -> str:
    slug = re.sub(r"^\d{2}[a-z]?-", "", doc_id)
    return slug.replace("-", " ").strip() or doc_id


def _resolved_doc_path(docs_dir: Path, doc_id: str) -> Path:
    if not is_safe_doc_id(doc_id):
        raise InvalidDocId(f"Invalid doc id: {doc_id}")
    file_path = (docs_dir / f"{doc_id}.md").resolve()
    docs_root = docs_dir.resolve()
    if not file_path.is_relative_to(docs_root):
        raise InvalidDocId(f"Invalid doc id: {doc_id}")
    return file_path


def list_all_docs() -> List[DocMeta]:
    """List numbered onboarding docs for the human HTTP surface."""
    docs_dir = get_docs_dir()
    if docs_dir is None:
        return []
    items: List[DocMeta] = []
    for path in sorted(docs_dir.iterdir()):
        if not path.is_file() or path.suffix != ".md":
            continue
        match = SAFE_DOC_PATTERN.match(path.name)
        if not match:
            continue
        doc_id = match.group(1)
        title = _title_from_first_line(path) or _id_to_title(doc_id)
        items.append(DocMeta(id=doc_id, filename=path.name, title=title, path=path))
    return items


def read_doc_text(doc_id: str) -> str:
    """Return full markdown for a numbered doc id (HTTP get-by-id)."""
    docs_dir = get_docs_dir()
    if docs_dir is None:
        raise DocsDirUnavailable("Developer docs not available")
    file_path = _resolved_doc_path(docs_dir, doc_id)
    if not file_path.is_file():
        raise DocNotFound(f"Doc not found: {doc_id}")
    try:
        return file_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("developer_doc_read_error", path=str(file_path), error=str(exc))
        raise DocsDirUnavailable("Failed to read doc") from exc


def _normalize_heading(text: str) -> str:
    lowered = (text or "").lower().replace("`", "")
    cleaned = re.sub(r"[^a-z0-9\s-]", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def _heading_slug(text: str) -> str:
    return _normalize_heading(text).replace(" ", "-")


def _heading_matches(heading: str, query: str) -> bool:
    q_norm = _normalize_heading(query)
    h_norm = _normalize_heading(heading)
    if not q_norm or not h_norm:
        return False
    if q_norm == h_norm:
        return True
    if _heading_slug(query) == _heading_slug(heading):
        return True
    if q_norm in h_norm:
        return True
    return False


def _iter_headings(markdown: str) -> List[Tuple[int, int, str]]:
    """Return (char_offset, level, title) for ATX headings outside fences."""
    headings: List[Tuple[int, int, str]] = []
    in_fence = False
    offset = 0
    for line in markdown.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line.rstrip("\n"))
            if match:
                headings.append((offset, len(match.group(1)), match.group(2).strip()))
        offset += len(line)
    return headings


def _section_window(markdown: str, section: str, doc_id: str) -> Tuple[str, str]:
    """Slice markdown from the matching heading through the next same-or-higher heading."""
    headings = _iter_headings(markdown)
    titles = [title for _, _, title in headings]
    for index, (start, level, title) in enumerate(headings):
        if not _heading_matches(title, section):
            continue
        end = len(markdown)
        for later_start, later_level, _ in headings[index + 1 :]:
            if later_level <= level:
                end = later_start
                break
        return markdown[start:end], title
    raise SectionNotFound(doc_id, section, titles)


def _apply_window(
    text: str, *, offset_chars: int, max_chars: Optional[int]
) -> Dict[str, Any]:
    total = len(text)
    offset = min(max(offset_chars, 0), total)
    limit = max_chars if max_chars is not None else _DEFAULT_MAX_CHARS
    limit = max(1, min(int(limit), _MAX_CHARS_CAP))
    window = text[offset : offset + limit]
    next_offset = offset + len(window)
    truncated = next_offset < total
    return {
        "offset_chars": offset,
        "returned_chars": len(window),
        "total_chars": total,
        "truncated": truncated,
        "next_offset_chars": next_offset if truncated else None,
        "text": window,
    }


def list_agent_facing() -> List[Dict[str, Any]]:
    """Catalog entries for ``core.docs_read`` when ``doc_id`` is omitted."""
    listed = {item.id: item for item in list_all_docs()}
    items: List[Dict[str, Any]] = []
    for entry in AGENT_FACING_DOCS:
        meta = listed.get(entry.id)
        items.append(
            {
                "id": entry.id,
                "filename": f"{entry.id}.md",
                "title": meta.title if meta is not None else _id_to_title(entry.id),
                "summary": entry.summary,
                "suggested_sections": list(entry.suggested_sections),
                "available": meta is not None,
            }
        )
    return items


def read_agent_facing(
    *,
    doc_id: Optional[str] = None,
    section: Optional[str] = None,
    offset_chars: int = 0,
    max_chars: Optional[int] = None,
) -> Dict[str, Any]:
    """Read an allowlisted doc with optional heading slice and char window.

    When ``doc_id`` is omitted, returns the agent-facing catalog (no body).
    """
    if not doc_id:
        return {
            "mode": "list",
            "items": list_agent_facing(),
            "hint": (
                "Pass doc_id to read a document. Optional section matches a "
                "heading (for example 'YAML structure'). Use offset_chars / "
                "max_chars to page long pages."
            ),
        }

    entry = get_agent_facing_doc(doc_id)
    if entry is None:
        if not is_safe_doc_id(doc_id):
            raise InvalidDocId(f"Invalid doc id: {doc_id}")
        raise DocNotAgentFacing(
            f"Doc '{doc_id}' is not in the agent-facing catalog. "
            f"Available: {', '.join(d.id for d in AGENT_FACING_DOCS)}"
        )

    body = read_doc_text(doc_id)
    matched_section: Optional[str] = None
    if section:
        body, matched_section = _section_window(body, section, doc_id)

    window = _apply_window(body, offset_chars=offset_chars, max_chars=max_chars)
    listed = {item.id: item for item in list_all_docs()}
    meta = listed.get(doc_id)
    result: Dict[str, Any] = {
        "mode": "read",
        "doc_id": doc_id,
        "filename": f"{doc_id}.md",
        "title": meta.title if meta is not None else _id_to_title(doc_id),
        "summary": entry.summary,
        "section": matched_section,
        **window,
    }
    if window["truncated"]:
        result["hint"] = (
            f"Truncated; call again with offset_chars={window['next_offset_chars']} "
            "to continue."
        )
    return result


__all__ = [
    "SAFE_DOC_PATTERN",
    "DeveloperDocsError",
    "InvalidDocId",
    "DocNotFound",
    "DocsDirUnavailable",
    "DocNotAgentFacing",
    "SectionNotFound",
    "DocMeta",
    "get_docs_dir",
    "is_safe_doc_id",
    "list_all_docs",
    "read_doc_text",
    "list_agent_facing",
    "read_agent_facing",
]
