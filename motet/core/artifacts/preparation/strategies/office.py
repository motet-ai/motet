"""
Motet - Office Document Preparation Strategy

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Provides the initial office-document preparation strategy family.
    The first implementation uses deterministic text extraction for PDF, DOCX,
    PPTX, ODT, and RTF payloads, then emits page/section-aware PreparedArtifactChunk
    records through the shared text chunking helper.

Dependencies:
    - motet.core.media.text_extraction for document text extraction
    - text strategy helpers for paragraph-aware chunk creation
    - preparation models for manifest and plan contracts

Usage:
    strategy = OfficeDocumentPreparationStrategy()
    result = strategy.prepare(strategy.plan(context), context)

Notes:
    - Table-specific chunks are enabled where extractors emit table/range markers.
    - Binary .doc remains deferred; .docx is supported directly.
"""

from __future__ import annotations

from ..hashing import canonical_json_hash
from ..models import ArtifactFeatureMatch, ArtifactPrepManifest, ArtifactPrepPlan, ArtifactPrepResult, ArtifactPrepStep
from ..strategy import ArtifactPrepContext
from .text import chunk_text_to_prepared_chunks

OFFICE_STRATEGY_ID = "office_document"
OFFICE_STRATEGY_VERSION = "1.0.0"

OFFICE_CONTENT_TYPES = [
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.oasis.opendocument.text",
    "application/rtf",
    "text/rtf",
]


class OfficeDocumentPreparationStrategy:
    """Built-in office-document preparation strategy."""

    manifest = ArtifactPrepManifest(
        strategy_id=OFFICE_STRATEGY_ID,
        strategy_version=OFFICE_STRATEGY_VERSION,
        handles=[ArtifactFeatureMatch(content_types=OFFICE_CONTENT_TYPES, extensions=[".pdf", ".docx", ".pptx", ".odt", ".rtf"])],
        priority=20,
        cost_class="moderate",
        produces_chunk_kinds=["text", "section", "table"],
        fallback_chain=["text_default"],
    )

    def plan(self, context: ArtifactPrepContext) -> ArtifactPrepPlan:
        config_hash = canonical_json_hash(
            {
                "strategy": OFFICE_STRATEGY_ID,
                "strategy_version": OFFICE_STRATEGY_VERSION,
                "content_type": context.payload_info.content_type,
                "chunk_size": int(context.config.get("chunk_size", 3200)),
                "chunk_overlap": int(context.config.get("chunk_overlap", 400)),
            }
        )
        return ArtifactPrepPlan(
            source_artifact_id=getattr(context.artifact, "id", None),
            strategy_id=OFFICE_STRATEGY_ID,
            strategy_version=OFFICE_STRATEGY_VERSION,
            prep_decision_source="dispatch",
            steps=[
                ArtifactPrepStep(name="extract_office_text", parameters={"content_type": context.payload_info.content_type}),
                ArtifactPrepStep(name="chunk_text", parameters=context.config),
            ],
            expected_chunk_kinds=["text", "section", "table"],
            canonical_config_hash=config_hash,
        )

    def prepare(self, plan: ArtifactPrepPlan, context: ArtifactPrepContext) -> ArtifactPrepResult:
        from ....media.text_extraction import extract_text_from_bytes
        from ....media.utils import normalize_to_bytes

        payload_bytes = normalize_to_bytes(context.payload)
        try:
            extracted = extract_text_from_bytes(payload_bytes, context.payload_info.content_type)
        except Exception as e:
            return ArtifactPrepResult(plan=plan, prep_state="prep_failed", diagnostics=[f"office_extract_failed: {e}"])

        chunks = chunk_text_to_prepared_chunks(
            extracted,
            source_artifact_id=str(context.source_artifact_id).strip()
            if str(context.source_artifact_id or "").strip()
            else str(getattr(context.artifact, "id")),
            derived_artifact_id=None,
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
            extraction_method=f"office:{context.payload_info.content_type}",
        )
        return ArtifactPrepResult(
            plan=plan,
            prep_state="prep_complete" if chunks else "prep_failed",
            chunks=chunks,
            diagnostics=[] if chunks else ["empty_office_text"],
            chunk_cache_key=chunks[0].chunk_cache_key if chunks else "",
        )

