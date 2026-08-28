"""
Motet - Video Artifact Preparation Strategy

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Consumes video derivation outputs (keyframes and transcript artifacts)
    and emits ``video_scene`` and ``transcript_segment`` chunks for
    artifact RAG indexing. Does not re-run ffmpeg; relies on the
    derive_video_visuals and derive_video_transcript commands.
    Source-level re-index is owned by the transcript track when transcription
    is enabled (including transcript reuse) so transcript_segment chunks are
    not dropped by a late visuals re-index.

Dependencies:
    - motet.core.artifacts for listing derived artifacts
    - motet.core.artifacts.preparation models for chunk contracts

Usage:
    strategy = VideoPreparationStrategy()
    result = strategy.prepare(plan, context)
"""

from __future__ import annotations

import time
from typing import Any

from ... import get_artifact_store
from ...types import ArtifactKind
from ..hashing import canonical_json_hash, chunk_cache_key, source_bytes_sha256, text_content_hash
from ..models import (
    ArtifactFeatureMatch,
    ArtifactPrepManifest,
    ArtifactPrepPlan,
    ArtifactPrepResult,
    MediaCoord,
    PreparedArtifactChunk,
)
from ..strategy import ArtifactPrepContext

VIDEO_STRATEGY_ID = "video_default"
VIDEO_STRATEGY_VERSION = "1.0.0"


class VideoPreparationStrategy:
    """Built-in preparation strategy for uploaded video artifacts."""

    manifest = ArtifactPrepManifest(
        strategy_id=VIDEO_STRATEGY_ID,
        strategy_version=VIDEO_STRATEGY_VERSION,
        handles=[
            ArtifactFeatureMatch(
                kinds=[ArtifactKind.USER_UPLOAD.value],
                content_types=["video/*"],
            )
        ],
        priority=10,
        cost_class="moderate",
        produces_chunk_kinds=["video_scene", "transcript_segment"],
    )

    def plan(self, context: ArtifactPrepContext) -> ArtifactPrepPlan:
        return ArtifactPrepPlan(
            source_artifact_id=context.source_artifact_id or context.artifact.id,
            strategy_id=VIDEO_STRATEGY_ID,
            strategy_version=VIDEO_STRATEGY_VERSION,
            steps=[{"name": "emit_video_derivation_chunks"}],
            expected_chunk_kinds=["video_scene", "transcript_segment"],
        )

    def prepare(self, plan: ArtifactPrepPlan, context: ArtifactPrepContext) -> ArtifactPrepResult:
        store = get_artifact_store()
        source_id = context.source_artifact_id or context.artifact.id
        keyframes = store.list(
            kind=ArtifactKind.DERIVED_VIDEO_KEYFRAME,
            source_artifact_id=source_id,
            limit=60,
            tenant_id=context.tenant_id,
            principal_id=context.principal_id,
            motet_id=context.motet_id,
        )
        transcripts = store.list(
            kind=ArtifactKind.DERIVED_VIDEO_TRANSCRIPT,
            source_artifact_id=source_id,
            limit=1,
            tenant_id=context.tenant_id,
            principal_id=context.principal_id,
            motet_id=context.motet_id,
        )

        chunks: list[PreparedArtifactChunk] = []
        now = float(getattr(context.artifact, "created_at", None) or time.time())
        source_hash = (
            context.payload_info.content_hash
            or getattr(context.artifact, "checksum_sha256", None)
            or source_bytes_sha256(context.payload if isinstance(context.payload, bytes) else b"")
        )

        source_filename = str(context.payload_info.filename or "").strip()
        for index, kf in enumerate(sorted(keyframes, key=lambda m: int(m.metadata.get("index") or 0))):
            t_ms = int(kf.metadata.get("t_ms") or 0)
            # TODO(ADR-0118 follow-up): caption keyframes via a vision model
            # (ocr_image_page pattern) so scene chunks carry real semantics.
            label = f" from {source_filename}" if source_filename else ""
            content_text = f"Video scene at {t_ms}ms{label} (keyframe {kf.id})"
            chunks.append(
                PreparedArtifactChunk(
                    source_artifact_id=source_id,
                    derived_artifact_id=kf.id,
                    chunk_index=index,
                    chunk_kind="video_scene",
                    content_text=content_text,
                    structured_payload={"artifact_ref": kf.id, "t_ms": t_ms},
                    content_hash=text_content_hash(content_text),
                    coordinates=MediaCoord(
                        timestamp_start=t_ms / 1000.0,
                        timestamp_end=t_ms / 1000.0,
                        frame=index,
                    ),
                    tenant_id=context.tenant_id,
                    principal_id=context.principal_id,
                    motet_id=context.motet_id,
                    role=context.role,
                    conversation_id=context.conversation_id,
                    content_type=kf.content_type,
                    filename=context.payload_info.filename,
                    artifact_tags=list(context.artifact_tags),
                    modality="video",
                    prep_strategy_id=VIDEO_STRATEGY_ID,
                    prep_strategy_version=VIDEO_STRATEGY_VERSION,
                    chunk_cache_key=chunk_cache_key(
                        source_content_hash=str(source_hash),
                        strategy_id=VIDEO_STRATEGY_ID,
                        strategy_version=VIDEO_STRATEGY_VERSION,
                        canonical_config_hash=canonical_json_hash(
                            {"chunk_kind": "video_scene", "keyframe_id": kf.id}
                        ),
                    ),
                    created_at=now,
                )
            )

        transcript_offset = len(chunks)
        if transcripts:
            transcript_meta = transcripts[0]
            payload = store.get(
                transcript_meta.id,
                tenant_id=context.tenant_id,
                principal_id=context.principal_id,
                motet_id=context.motet_id,
            )
            segments: list[dict[str, Any]] = list(transcript_meta.metadata.get("segments") or [])
            if not segments and isinstance(payload, (str, bytes)):
                text = payload.decode("utf-8", errors="ignore") if isinstance(payload, bytes) else payload
                if text.strip():
                    segments = [{"start_ms": 0, "end_ms": 0, "text": text.strip()}]

            for seg_index, seg in enumerate(segments):
                text = str(seg.get("text") or "").strip()
                if not text:
                    continue
                start_ms = int(seg.get("start_ms") or 0)
                end_ms = int(seg.get("end_ms") or start_ms)
                chunks.append(
                    PreparedArtifactChunk(
                        source_artifact_id=source_id,
                        derived_artifact_id=transcript_meta.id,
                        chunk_index=transcript_offset + seg_index,
                        chunk_kind="transcript_segment",
                        content_text=text,
                        structured_payload={
                            "t_start_ms": start_ms,
                            "t_end_ms": end_ms,
                        },
                        content_hash=text_content_hash(text),
                        coordinates=MediaCoord(
                            timestamp_start=start_ms / 1000.0,
                            timestamp_end=end_ms / 1000.0,
                        ),
                        tenant_id=context.tenant_id,
                        principal_id=context.principal_id,
                        motet_id=context.motet_id,
                        role=context.role,
                        conversation_id=context.conversation_id,
                        content_type="text/plain",
                        filename=context.payload_info.filename,
                        artifact_tags=list(context.artifact_tags),
                        modality="video",
                        prep_strategy_id=VIDEO_STRATEGY_ID,
                        prep_strategy_version=VIDEO_STRATEGY_VERSION,
                        chunk_cache_key=chunk_cache_key(
                            source_content_hash=str(source_hash),
                            strategy_id=VIDEO_STRATEGY_ID,
                            strategy_version=VIDEO_STRATEGY_VERSION,
                            canonical_config_hash=canonical_json_hash(
                                {
                                    "chunk_kind": "transcript_segment",
                                    "segment_index": seg_index,
                                    "start_ms": start_ms,
                                }
                            ),
                        ),
                        created_at=now,
                    )
                )

        derived_ids = [kf.id for kf in keyframes]
        if transcripts:
            derived_ids.append(transcripts[0].id)

        return ArtifactPrepResult(
            plan=plan,
            prep_state="prep_complete" if chunks else "prep_partial",
            chunks=chunks,
            derived_artifact_ids=derived_ids,
            diagnostics=[] if chunks else ["no_video_derivations_found"],
        )
