"""
Motet - Text Artifact Preparation Strategy

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Implements the built-in plain-text preparation strategy by lifting
    the previous paragraph-aware chunker into the artifact preparation
    package. The strategy emits PreparedArtifactChunk records with TextCoord
    coordinates and strategy metadata for generic indexing.

Dependencies:
    - re for paragraph, heading, and page marker parsing
    - motet.core.artifacts.preparation models for the canonical chunk contract
    - hashing helpers for deterministic content and cache hashes

Usage:
    strategy = TextPreparationStrategy()
    plan = strategy.plan(context)
    result = strategy.prepare(plan, context)

Notes:
    - Character budgets remain a lightweight token approximation for worker
      safety; tokenizer-dependent tuning belongs in later retrieval evaluation.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ..hashing import canonical_json_hash, chunk_cache_key, source_bytes_sha256, text_content_hash
from ..models import (
    ArtifactFeatureMatch,
    ArtifactPrepManifest,
    ArtifactPrepPlan,
    ArtifactPrepResult,
    ArtifactPrepState,
    ArtifactPrepStep,
    PreparedArtifactChunk,
    TextCoord,
)
from ..strategy import ArtifactPrepContext

TEXT_STRATEGY_ID = "text_default"
TEXT_STRATEGY_VERSION = "1.0.0"

_PAGE_MARKER_RE = re.compile(r"(?:^|\n)\s*(?:---\s*)?(?:page|Page)\s+(\d+)\b")
_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*$")


def _utf8_len(value: str) -> int:
    return len(value.encode("utf-8", errors="ignore"))


def _page_for_offset(text_prefix: str) -> Optional[int]:
    matches = list(_PAGE_MARKER_RE.finditer(text_prefix))
    if not matches:
        return None
    try:
        return int(matches[-1].group(1))
    except (TypeError, ValueError):
        return None


def _heading_path_for_offset(text_prefix: str) -> list[str]:
    headings: list[str] = []
    for line in text_prefix.splitlines():
        match = _HEADING_RE.match(line)
        if not match:
            continue
        level = len(match.group(1))
        title = match.group(2).strip()
        headings = headings[: max(0, level - 1)]
        headings.append(title)
    return headings


def _split_paragraphs(text: str) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n+", text) if part.strip()]
    return paragraphs or [text.strip()] if text.strip() else []


def _slice_long_text(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    slices: list[str] = []
    cursor = 0
    text_len = len(text)
    step_back = max(0, min(overlap_chars, max_chars // 2))
    while cursor < text_len:
        end = min(cursor + max_chars, text_len)
        slices.append(text[cursor:end].strip())
        if end >= text_len:
            break
        cursor = max(end - step_back, cursor + 1)
    return [part for part in slices if part]


def normalize_text_payload(payload: Any) -> str:
    """Normalize an artifact payload into text for text-like strategies."""

    if payload is None:
        return ""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="ignore")
    if isinstance(payload, str):
        return payload
    return str(payload)


class TextPreparationStrategy:
    """Built-in paragraph-aware text preparation strategy."""

    manifest = ArtifactPrepManifest(
        strategy_id=TEXT_STRATEGY_ID,
        strategy_version=TEXT_STRATEGY_VERSION,
        handles=[
            ArtifactFeatureMatch(content_types=["text/*", "application/xml"], extensions=[".txt", ".md", ".markdown"]),
            ArtifactFeatureMatch(kinds=["derived_text"], content_types=["text/plain", "text/*"]),
        ],
        priority=10,
        cost_class="cheap",
        produces_chunk_kinds=["text", "section"],
    )

    def plan(self, context: ArtifactPrepContext) -> ArtifactPrepPlan:
        config_hash = canonical_json_hash(
            {
                "chunk_size": int(context.config.get("chunk_size", 3200)),
                "chunk_overlap": int(context.config.get("chunk_overlap", 400)),
                "strategy": TEXT_STRATEGY_ID,
                "strategy_version": TEXT_STRATEGY_VERSION,
            }
        )
        return ArtifactPrepPlan(
            source_artifact_id=getattr(context.artifact, "id", None),
            strategy_id=TEXT_STRATEGY_ID,
            strategy_version=TEXT_STRATEGY_VERSION,
            prep_decision_source="dispatch",
            steps=[ArtifactPrepStep(name="chunk_text", parameters=context.config)],
            expected_chunk_kinds=["text", "section"],
            canonical_config_hash=config_hash,
        )

    def prepare(self, plan: ArtifactPrepPlan, context: ArtifactPrepContext) -> ArtifactPrepResult:
        text = normalize_text_payload(context.payload)
        chunks = chunk_text_to_prepared_chunks(
            text,
            source_artifact_id=(
                str(context.source_artifact_id).strip()
                if str(context.source_artifact_id or "").strip()
                else str(getattr(context.artifact, "source_artifact_id", None) or getattr(context.artifact, "id"))
            ),
            derived_artifact_id=(
                str(getattr(context.artifact, "id"))
                if str(getattr(getattr(context.artifact, "kind", ""), "value", getattr(context.artifact, "kind", "")))
                == "derived_text"
                else None
            ),
            tenant_id=context.tenant_id,
            principal_id=context.principal_id,
            motet_id=context.motet_id,
            conversation_id=context.conversation_id,
            role=context.role,
            content_type=context.payload_info.content_type,
            filename=context.payload_info.filename,
            artifact_tags=list(context.artifact_tags)
            if context.artifact_tags
            else list((getattr(context.artifact, "metadata", {}) or {}).get("tags") or []),
            created_at=float(getattr(context.artifact, "created_at", 0.0) or 0.0),
            expires_at=getattr(context.artifact, "expires_at", None),
            chunk_size=int(context.config.get("chunk_size", 3200)),
            chunk_overlap=int(context.config.get("chunk_overlap", 400)),
            prep_strategy_id=plan.strategy_id,
            prep_strategy_version=plan.strategy_version,
            canonical_config_hash=plan.canonical_config_hash,
            source_content_hash=context.payload_info.content_hash or "",
        )
        state: ArtifactPrepState = "prep_complete" if chunks else "prep_failed"
        cache_key = chunks[0].chunk_cache_key if chunks else ""
        return ArtifactPrepResult(
            plan=plan,
            prep_state=state,
            chunks=chunks,
            derived_artifact_ids=[chunk.derived_artifact_id for chunk in chunks if chunk.derived_artifact_id],
            diagnostics=[] if chunks else ["empty_text"],
            chunk_cache_key=cache_key,
        )


def chunk_text_to_prepared_chunks(
    text: str,
    *,
    source_artifact_id: str,
    derived_artifact_id: Optional[str],
    tenant_id: str,
    principal_id: str,
    motet_id: str,
    conversation_id: str,
    role: str = "user",
    content_type: str = "text/plain",
    filename: Optional[str] = None,
    artifact_tags: Optional[list[str]] = None,
    created_at: float = 0.0,
    expires_at: Optional[float] = None,
    chunk_size: int = 3200,
    chunk_overlap: int = 400,
    prep_strategy_id: str = TEXT_STRATEGY_ID,
    prep_strategy_version: str = TEXT_STRATEGY_VERSION,
    canonical_config_hash: str = "",
    source_content_hash: str = "",
    extraction_method: Optional[str] = None,
) -> list[PreparedArtifactChunk]:
    """Split text into PreparedArtifactChunk records with text coordinates."""

    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    max_chars = max(256, int(chunk_size or 3200))
    overlap_chars = max(0, min(int(chunk_overlap or 0), max_chars // 2))
    paragraphs = _split_paragraphs(normalized)

    raw_chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        paragraph_parts = (
            _slice_long_text(paragraph, max_chars=max_chars, overlap_chars=overlap_chars)
            if len(paragraph) > max_chars
            else [paragraph]
        )
        for part in paragraph_parts:
            candidate = f"{current}\n\n{part}".strip() if current else part
            if len(candidate) <= max_chars:
                current = candidate
                continue
            if current:
                raw_chunks.append(current)
            current = part
    if current:
        raw_chunks.append(current)

    if overlap_chars and len(raw_chunks) > 1:
        overlapped: list[str] = []
        previous_tail = ""
        for raw in raw_chunks:
            combined = f"{previous_tail}\n\n{raw}".strip() if previous_tail else raw
            overlapped.append(combined)
            previous_tail = raw[-overlap_chars:].strip()
        raw_chunks = overlapped

    final_config_hash = canonical_config_hash or canonical_json_hash(
        {"chunk_size": max_chars, "chunk_overlap": overlap_chars, "strategy": prep_strategy_id}
    )
    final_source_hash = source_content_hash or source_bytes_sha256(normalized.encode("utf-8", errors="ignore"))
    cache_key = chunk_cache_key(
        source_content_hash=final_source_hash,
        strategy_id=prep_strategy_id,
        strategy_version=prep_strategy_version,
        canonical_config_hash=final_config_hash,
    )

    chunks: list[PreparedArtifactChunk] = []
    search_start = 0
    byte_cursor = 0
    for index, chunk in enumerate(raw_chunks):
        plain_chunk = chunk.strip()
        if not plain_chunk:
            continue
        char_start = normalized.find(plain_chunk[: min(len(plain_chunk), 80)], search_start)
        if char_start < 0:
            char_start = search_start
        char_end = min(len(normalized), char_start + len(plain_chunk))
        byte_start = _utf8_len(normalized[:char_start])
        byte_end = max(byte_start, _utf8_len(normalized[:char_end]))
        byte_cursor = max(byte_cursor, byte_end)
        search_start = max(char_end, search_start)
        page_number = _page_for_offset(normalized[:char_start])
        heading_path = _heading_path_for_offset(normalized[:char_start])
        chunks.append(
            PreparedArtifactChunk(
                source_artifact_id=source_artifact_id,
                derived_artifact_id=derived_artifact_id,
                chunk_index=index,
                chunk_kind="section" if heading_path else "text",
                content_text=plain_chunk,
                content_hash=text_content_hash(plain_chunk),
                coordinates=TextCoord(
                    byte_start=byte_start,
                    byte_end=byte_cursor,
                    page_number=page_number,
                    heading_path=heading_path,
                    extraction_method=extraction_method,
                ),
                content_type=content_type,
                filename=filename,
                artifact_tags=list(artifact_tags or []),
                tenant_id=tenant_id,
                principal_id=principal_id,
                motet_id=motet_id,
                role=role or "user",
                conversation_id=conversation_id,
                modality="text",
                confidence=1.0,
                prep_strategy_id=prep_strategy_id,
                prep_strategy_version=prep_strategy_version,
                chunk_cache_key=cache_key,
                created_at=float(created_at or 0.0),
                expires_at=expires_at,
            )
        )
    return chunks

