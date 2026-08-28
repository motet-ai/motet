"""
Motet - Structured DOCX Artifact Preparation Strategy

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Implements a structure-aware DOCX preparation strategy. The strategy
    reads WordprocessingML blocks in document order, preserves heading paths,
    chunks on paragraph boundaries, and emits table chunks with table coordinates
    instead of flattening the whole document into raw text before chunking.

Dependencies:
    - python-docx for parsing DOCX paragraph, style, and table structure
    - motet.core.artifacts.preparation models for the canonical chunk contract
    - hashing helpers for deterministic content and cache hashes

Usage:
    strategy = DocxStructuredPreparationStrategy()
    plan = strategy.plan(context)
    result = strategy.prepare(plan, context)

Notes:
    - Byte coordinates refer to the deterministic rendered text representation
      produced by this strategy, not offsets inside the compressed DOCX archive.
    - The generic office_document strategy remains available as a fallback.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..hashing import canonical_json_hash, chunk_cache_key, structured_content_hash
from ..models import (
    ArtifactFeatureMatch,
    ArtifactPrepManifest,
    ArtifactPrepPlan,
    ArtifactPrepResult,
    ArtifactPrepStep,
    PreparedArtifactChunk,
    TableCoord,
    TextCoord,
)
from ..strategy import ArtifactPrepContext

DOCX_STRUCTURED_STRATEGY_ID = "docx_structured"
DOCX_STRUCTURED_STRATEGY_VERSION = "1.0.0"
DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_HEADING_STYLE_RE = re.compile(r"^heading\s+([1-9])\b", re.IGNORECASE)


@dataclass
class _DocxBlock:
    """Rendered DOCX block with extracted structural metadata."""

    kind: str
    text: str
    byte_start: int = 0
    byte_end: int = 0
    heading_path: list[str] = field(default_factory=list)
    table_index: Optional[int] = None
    table_headers: list[str] = field(default_factory=list)
    table_rows: list[list[str]] = field(default_factory=list)


def _utf8_len(value: str) -> int:
    return len(value.encode("utf-8", errors="ignore"))


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _heading_level(style_name: str) -> Optional[int]:
    match = _HEADING_STYLE_RE.match((style_name or "").strip())
    if not match:
        return None
    return int(match.group(1))


def _render_table(rows: list[list[str]], table_index: int) -> str:
    rendered_rows = [" | ".join(cell for cell in row).strip() for row in rows if any(cell for cell in row)]
    if not rendered_rows:
        return ""
    return f"Table {table_index}\n" + "\n".join(rendered_rows)


def _iter_docx_blocks(document: Any) -> list[_DocxBlock]:
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    blocks: list[_DocxBlock] = []
    heading_path: list[str] = []
    table_index = 0

    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            paragraph = Paragraph(child, document)
            text = _normalize_text(paragraph.text)
            if not text:
                continue

            style_name = getattr(getattr(paragraph, "style", None), "name", "") or ""
            level = _heading_level(style_name)
            if level is not None:
                heading_path = heading_path[: max(0, level - 1)]
                heading_path.append(text)
                blocks.append(_DocxBlock(kind="heading", text=text, heading_path=list(heading_path)))
            else:
                blocks.append(_DocxBlock(kind="paragraph", text=text, heading_path=list(heading_path)))
            continue

        if child.tag == qn("w:tbl"):
            table_index += 1
            table = Table(child, document)
            rows: list[list[str]] = []
            for row in table.rows:
                cells = [_normalize_text(cell.text) for cell in row.cells]
                if any(cells):
                    rows.append(cells)
            text = _render_table(rows, table_index)
            if not text:
                continue
            headers = rows[0] if rows else []
            blocks.append(
                _DocxBlock(
                    kind="table",
                    text=text,
                    heading_path=list(heading_path),
                    table_index=table_index,
                    table_headers=headers,
                    table_rows=rows,
                )
            )

    cursor = 0
    for index, block in enumerate(blocks):
        separator = "" if index == 0 else "\n\n"
        cursor += _utf8_len(separator)
        block.byte_start = cursor
        cursor += _utf8_len(block.text)
        block.byte_end = cursor

    return blocks


def _slice_long_block(block: _DocxBlock, max_chars: int) -> list[_DocxBlock]:
    parts: list[_DocxBlock] = []
    remaining = block.text
    char_offset = 0

    while remaining:
        if len(remaining) <= max_chars:
            piece = remaining.strip()
        else:
            candidate = remaining[:max_chars]
            split_at = max(candidate.rfind(". "), candidate.rfind("; "), candidate.rfind(" "))
            if split_at < max_chars // 2:
                split_at = max_chars
            piece = remaining[:split_at].strip()

        if piece:
            byte_start = block.byte_start + _utf8_len(block.text[:char_offset])
            byte_end = byte_start + _utf8_len(piece)
            parts.append(
                _DocxBlock(
                    kind=block.kind,
                    text=piece,
                    byte_start=byte_start,
                    byte_end=byte_end,
                    heading_path=list(block.heading_path),
                    table_index=block.table_index,
                    table_headers=list(block.table_headers),
                    table_rows=list(block.table_rows),
                )
            )

        char_offset += len(piece)
        remaining = remaining[len(piece) :].strip()

    return parts


def _artifact_tags(context: ArtifactPrepContext) -> list[str]:
    if context.artifact_tags:
        return list(context.artifact_tags)
    return list((getattr(context.artifact, "metadata", {}) or {}).get("tags") or [])


def _source_artifact_id(context: ArtifactPrepContext) -> str:
    if str(context.source_artifact_id or "").strip():
        return str(context.source_artifact_id).strip()
    return str(getattr(context.artifact, "source_artifact_id", None) or getattr(context.artifact, "id"))


def _source_content_hash_from_context(context: ArtifactPrepContext, payload_bytes: bytes) -> str:
    from ..hashing import effective_source_content_hash

    declared = str(context.payload_info.content_hash or "").strip()
    if declared:
        return declared
    return effective_source_content_hash(declared_hash=None, payload_bytes=payload_bytes)


class DocxStructuredPreparationStrategy:
    """Built-in structure-aware DOCX preparation strategy."""

    manifest = ArtifactPrepManifest(
        strategy_id=DOCX_STRUCTURED_STRATEGY_ID,
        strategy_version=DOCX_STRUCTURED_STRATEGY_VERSION,
        handles=[ArtifactFeatureMatch(content_types=[DOCX_CONTENT_TYPE], extensions=[".docx"])],
        priority=30,
        cost_class="moderate",
        produces_chunk_kinds=["section", "text", "table"],
        fallback_chain=["office_document", "text_default"],
    )

    def plan(self, context: ArtifactPrepContext) -> ArtifactPrepPlan:
        config_hash = canonical_json_hash(
            {
                "strategy": DOCX_STRUCTURED_STRATEGY_ID,
                "strategy_version": DOCX_STRUCTURED_STRATEGY_VERSION,
                "chunk_size": int(context.config.get("chunk_size", 3200)),
            }
        )
        return ArtifactPrepPlan(
            source_artifact_id=getattr(context.artifact, "id", None),
            strategy_id=DOCX_STRUCTURED_STRATEGY_ID,
            strategy_version=DOCX_STRUCTURED_STRATEGY_VERSION,
            prep_decision_source="dispatch",
            steps=[
                ArtifactPrepStep(name="parse_docx_blocks", parameters={"content_type": context.payload_info.content_type}),
                ArtifactPrepStep(name="chunk_docx_blocks", parameters=context.config),
            ],
            expected_chunk_kinds=["section", "text", "table"],
            canonical_config_hash=config_hash,
        )

    def prepare(self, plan: ArtifactPrepPlan, context: ArtifactPrepContext) -> ArtifactPrepResult:
        from docx import Document

        from ....media.utils import normalize_to_bytes

        payload_bytes = normalize_to_bytes(context.payload)
        try:
            document = Document(io.BytesIO(payload_bytes))
            blocks = _iter_docx_blocks(document)
        except Exception as e:
            return ArtifactPrepResult(plan=plan, prep_state="prep_failed", diagnostics=[f"docx_parse_failed: {e}"])

        if not blocks:
            return ArtifactPrepResult(plan=plan, prep_state="prep_failed", diagnostics=["empty_docx"])

        rendered_text = "\n\n".join(block.text for block in blocks)
        source_hash = _source_content_hash_from_context(context, payload_bytes)
        cache_key = chunk_cache_key(
            source_content_hash=source_hash,
            strategy_id=plan.strategy_id,
            strategy_version=plan.strategy_version,
            canonical_config_hash=plan.canonical_config_hash,
        )
        chunks = _blocks_to_chunks(
            blocks=blocks,
            context=context,
            plan=plan,
            source_hash=source_hash,
            cache_key=cache_key,
        )

        return ArtifactPrepResult(
            plan=plan,
            prep_state="prep_complete" if chunks else "prep_failed",
            chunks=chunks,
            diagnostics=[] if chunks else ["empty_docx_chunks"],
            chunk_cache_key=cache_key if chunks else "",
        )


def _blocks_to_chunks(
    *,
    blocks: list[_DocxBlock],
    context: ArtifactPrepContext,
    plan: ArtifactPrepPlan,
    source_hash: str,
    cache_key: str,
) -> list[PreparedArtifactChunk]:
    max_chars = max(256, int(context.config.get("chunk_size", 3200) or 3200))
    source_id = _source_artifact_id(context)
    base_kwargs: dict[str, Any] = {
        "source_artifact_id": source_id,
        "derived_artifact_id": None,
        "tenant_id": context.tenant_id,
        "principal_id": context.principal_id,
        "motet_id": context.motet_id,
        "role": context.role or "user",
        "conversation_id": context.conversation_id,
        "content_type": context.payload_info.content_type,
        "filename": context.payload_info.filename,
        "artifact_tags": _artifact_tags(context),
        "modality": "text",
        "confidence": 1.0,
        "prep_strategy_id": plan.strategy_id,
        "prep_strategy_version": plan.strategy_version,
        "chunk_cache_key": cache_key,
        "created_at": float(getattr(context.artifact, "created_at", 0.0) or 0.0),
        "expires_at": getattr(context.artifact, "expires_at", None),
    }

    chunks: list[PreparedArtifactChunk] = []
    current: list[_DocxBlock] = []
    current_len = 0

    def flush_current() -> None:
        nonlocal current, current_len
        if not current:
            return
        content_text = "\n\n".join(block.text for block in current).strip()
        heading_path = list(current[-1].heading_path)
        chunks.append(
            PreparedArtifactChunk(
                **base_kwargs,
                chunk_index=len(chunks),
                chunk_kind="section" if heading_path else "text",
                content_text=content_text,
                structured_payload={"heading_path": heading_path, "source_content_hash": source_hash},
                content_hash=structured_content_hash(
                    content_text=content_text,
                    structured_payload={"heading_path": heading_path},
                ),
                coordinates=TextCoord(
                    byte_start=current[0].byte_start,
                    byte_end=current[-1].byte_end,
                    heading_path=heading_path,
                    extraction_method="docx:structured",
                ),
            )
        )
        current = []
        current_len = 0

    for block in blocks:
        block_parts = _slice_long_block(block, max_chars) if len(block.text) > max_chars else [block]
        for part in block_parts:
            if part.kind == "heading" and current:
                flush_current()

            if part.kind == "table":
                flush_current()
                content_text = part.text.strip()
                table_payload = {
                    "heading_path": part.heading_path,
                    "table_index": part.table_index,
                    "headers": part.table_headers,
                    "rows": part.table_rows,
                    "source_content_hash": source_hash,
                }
                chunks.append(
                    PreparedArtifactChunk(
                        **base_kwargs,
                        chunk_index=len(chunks),
                        chunk_kind="table",
                        content_text=content_text,
                        structured_payload=table_payload,
                        content_hash=structured_content_hash(
                            content_text=content_text,
                            structured_payload=table_payload,
                        ),
                        coordinates=TableCoord(
                            workbook=context.payload_info.filename,
                            sheet=f"Table {part.table_index}" if part.table_index else None,
                            range=f"table:{part.table_index or len(chunks)}",
                            headers=part.table_headers,
                        ),
                    )
                )
                continue

            separator_len = 2 if current else 0
            projected_len = current_len + separator_len + len(part.text)
            if current and projected_len > max_chars:
                flush_current()
            current.append(part)
            current_len = current_len + (2 if current_len else 0) + len(part.text)

    flush_current()
    return chunks

