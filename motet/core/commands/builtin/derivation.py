"""
Motet - Upload Derivation Commands

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Distributed commands for artifact derivation.
    These commands run on workers and produce derived artifacts (e.g., extracted text,
    resized images) from user-uploaded source artifacts.
    Includes parallel vision OCR for PDF pages using distributed command execution.
    Video derivation is split into derive_video_visuals (poster/keyframes) and
    derive_video_transcript (audio + pluggable transcription backend) so the two
    tracks run in parallel with independent retries.

Dependencies:
    - motet.core.commands.decorator / motet: @motet.command and MotetContext access
    - motet.core.commands.command_data_classes: DeriveUploadTextData, DeriveUploadImageData
    - motet.core.uploads.derivation_service: derivation logic (extract + store derived artifacts)
    - structlog: Structured logging

Usage:
    from uuid import uuid4
    from motet.core.commands.builtin.derivation import derive_upload_text, derive_upload_image
    from motet.core.commands.command_data_classes import DeriveUploadTextData, DeriveUploadImageData
    from motet.core.workers import global_invoker
    import asyncio

    # Text derivation
    data = DeriveUploadTextData(source_artifact_id="artifact_123")
    cmd = derive_upload_text(
        task_id=str(uuid4()),
        conversation_id="",
        tenant_id="tenant_123",
        principal_id="principal_123",
        motet_id="default",
        data=data,
    )
    result = asyncio.to_thread(global_invoker.execute_command, cmd)
    
    # Image derivation
    image_data = DeriveUploadImageData(source_artifact_id="image_123", derivation_names=["thumb", "base"])
    image_cmd = derive_upload_image(
        task_id=str(uuid4()),
        conversation_id="",
        tenant_id="tenant_123",
        principal_id="principal_123",
        motet_id="default",
        data=image_data,
    )
    result = asyncio.to_thread(global_invoker.execute_command, image_cmd)

Notes:
    - This is a hard cutover from direct Celery tasks; derivation is executed via the
      distributed command system for reuse and consistent routing/observability.
"""

from typing import Any, Dict, List

import structlog

from motet import motet
from motet.core.commands.decorator import get_motet_context
from motet.core.commands.capabilities import WorkerCapability
from motet.core.commands.command_data_classes import (
    DeriveUploadTextData,
    DeriveUploadImageData,
    DeriveVideoVisualsData,
    DeriveVideoTranscriptData,
    DeriveOfficeEmbeddedImagesData,
    OCREmbeddedImageData,
    DerivePdfPageImagesData,
    PrepareArtifactIndexData,
    OCRImagePageData,
)
from motet.core.media.derivation_service import DerivationError, derive_text_artifact, derive_image_artifacts
from motet.core.media.video_processing import (
    KeyframeStrategy,
    derive_video_transcript_artifact,
    derive_video_visual_artifacts,
)

logger = structlog.get_logger(__name__)


def _dispatch_artifact_rag_index(
    *,
    motet: Any,
    source_artifact_id: str,
    derived_artifact_id: str | None,
) -> None:
    """Dispatch artifact preparation/indexing after successful derived text creation."""

    from motet.core.commands.builtin.rag import rag_index_should_use_source_payload

    index_source_artifact = (
        rag_index_should_use_source_payload(motet, source_artifact_id=source_artifact_id, derived_artifact_id=derived_artifact_id)
        if derived_artifact_id
        else False
    )
    if not derived_artifact_id and not index_source_artifact:
        return
    cfg = getattr(getattr(motet, "stack", None), "config", None)
    if cfg is None or not bool(getattr(cfg, "artifact_rag_enabled", False)):
        return
    if not bool(getattr(cfg, "artifact_rag_index_on_derivation", True)):
        return

    try:
        from motet.core.commands.builtin.rag import prepare_artifact_index

        child = prepare_artifact_index(
            task_id=motet.task_id or "",
            conversation_id=motet.conversation_id or "",
            tenant_id=motet.tenant_id,
            principal_id=motet.principal_id,
            motet_id=motet.motet_id,
            data=PrepareArtifactIndexData(
                source_artifact_id=source_artifact_id,
                derived_artifact_id=None if index_source_artifact else derived_artifact_id,
                force_reindex=True,
            ),
        )
        task_ids = motet.dispatch([child])
        logger.info(
            "artifact_rag_index_dispatched",
            **motet.log_fields(
                source_artifact_id=source_artifact_id,
                derived_artifact_id=derived_artifact_id,
                index_source_artifact=index_source_artifact,
                child_command_id=child.command_id,
                task_ids=task_ids,
            ),
        )
    except Exception as e:
        logger.warning(
            "artifact_rag_index_dispatch_failed",
            **motet.log_fields(
                source_artifact_id=source_artifact_id,
                derived_artifact_id=derived_artifact_id,
                error=str(e),
                error_type=type(e).__name__,
            ),
            exc_info=True,
        )


def _dispatch_artifact_rag_source_index(
    *,
    motet: Any,
    source_artifact_id: str,
    reason: str,
) -> None:
    """Dispatch source-payload indexing when text derivation cannot produce a derived artifact."""

    cfg = getattr(getattr(motet, "stack", None), "config", None)
    if cfg is None or not bool(getattr(cfg, "artifact_rag_enabled", False)):
        return
    if not bool(getattr(cfg, "artifact_rag_index_on_derivation", True)):
        return

    source_meta = motet.artifact_store.get_metadata(source_artifact_id)
    source_payload = motet.artifact_store.get(source_artifact_id) if source_meta else None
    if not source_meta or source_payload in (None, "", b""):
        return

    from motet.core.commands.builtin.rag import PrepareArtifactIndexData, _build_prep_context_for_index, prepare_artifact_index
    from motet.core.artifacts.preparation import ArtifactPrepSelector

    data = PrepareArtifactIndexData(source_artifact_id=source_artifact_id, force_reindex=True)
    context = _build_prep_context_for_index(
        motet=motet,
        prepare_meta=source_meta,
        source_meta=source_meta,
        data=data,
        payload=source_payload,
        artifact_tags=[],
        cfg=cfg,
    )
    try:
        selection = ArtifactPrepSelector().select(context)
    except ValueError:
        return
    if selection.plan.strategy_id == "text_default":
        return

    child = prepare_artifact_index(
        task_id=motet.task_id or "",
        conversation_id=motet.conversation_id or "",
        tenant_id=motet.tenant_id,
        principal_id=motet.principal_id,
        motet_id=motet.motet_id,
        data=data,
    )
    task_ids = motet.dispatch([child])
    logger.info(
        "artifact_rag_source_index_dispatched",
        **motet.log_fields(
            source_artifact_id=source_artifact_id,
            strategy_id=selection.plan.strategy_id,
            reason=reason,
            child_command_id=child.command_id,
            task_ids=task_ids,
        ),
    )


def _video_transcription_will_own_rag_index(motet: Any) -> bool:
    """Return True when the transcript track will run and should own final source re-index."""

    cfg = getattr(getattr(motet, "stack", None), "config", None)
    if cfg is None or not bool(getattr(cfg, "artifact_rag_enabled", False)):
        return False
    if not bool(getattr(cfg, "video_transcription_enabled", True)):
        return False
    backend = str(getattr(cfg, "video_transcription_backend", "none") or "none").strip().lower()
    return backend != "none"


@motet.command(
    description="Extract text from an uploaded file artifact and store it as a derived text artifact for RAG and reading.",
    timeout_seconds=300,
    required_capabilities=[WorkerCapability.FILE_OPERATIONS],
)
def derive_upload_text(data: DeriveUploadTextData) -> Dict[str, Any]:
    """
    Derive extracted text from an uploaded artifact and store it as a derived artifact.

    Args:
        data: DeriveUploadTextData with the source artifact ID

    Returns:
        Dict describing the derived artifact (implementation-defined by derivation_service)

    Raises:
        DerivationError: For expected derivation failures (unsupported type, parse errors)
        Exception: For unexpected system failures (will be wrapped by command framework)
    """
    motet = get_motet_context()

    logger.info(
        "derive_upload_text_started",
        source_artifact_id=data.source_artifact_id,
        tenant_id=motet.tenant_id,
        principal_id=motet.principal_id,
        motet_id=motet.motet_id,
        task_id=motet.task_id,
        command_id=motet.command_id,
    )

    try:
        # Special-case PDFs: orchestrate via commands for page images + vision OCR (commands-only refactor)
        from motet.core.artifacts import ArtifactKind
        from motet.core.media.text_extraction import extract_pdf_text_layers, combine_text_layer_and_ocr
        from motet.core.media.utils import normalize_to_bytes
        from motet.core.media.exceptions import SourceArtifactNotFoundError, ArtifactPayloadMissingError

        source_meta = motet.artifact_store.get_metadata(data.source_artifact_id)
        if not source_meta:
            raise SourceArtifactNotFoundError(f"Source artifact {data.source_artifact_id} not found")

        source_bytes = motet.artifact_store.get(data.source_artifact_id)
        if not source_bytes:
            raise ArtifactPayloadMissingError("Source artifact payload missing")

        source_bytes = normalize_to_bytes(source_bytes)

        if source_meta.content_type == "application/pdf":
            # Prefer explicit model selection passed from upstream (e.g., Chat-X upload),
            # otherwise fall back to any model hints stored on the source artifact metadata.
            # This makes OCR model selection robust even if a worker was dispatched before new fields existed.
            model_provider = (str(getattr(data, "model_provider", None) or "").strip() or None)
            model_name = (str(getattr(data, "model_name", None) or "").strip() or None)
            model_profile_name = (str(getattr(data, "model_profile_name", None) or "").strip() or None)
            try:
                src_md = getattr(source_meta, "metadata", {}) or {}
                if not model_provider:
                    model_provider = (str(src_md.get("model_provider") or "").strip() or None)
                if not model_name:
                    model_name = (str(src_md.get("model_name") or "").strip() or None)
                if not model_profile_name:
                    model_profile_name = (str(src_md.get("model_profile_name") or "").strip() or None)
                raw_prep_hints = src_md.get("prep_hints")
                prep_hints = raw_prep_hints if isinstance(raw_prep_hints, dict) else {}
                if not model_provider:
                    model_provider = (str(prep_hints.get("model_provider") or "").strip() or None)
                if not model_name:
                    model_name = (str(prep_hints.get("model_name") or "").strip() or None)
                if not model_profile_name:
                    model_profile_name = (str(prep_hints.get("model_profile_name") or "").strip() or None)
            except Exception:
                pass  # metadata extraction best-effort; continue with defaults

            # 1) Extract text layers (fast, local)
            total_pages, text_layers = extract_pdf_text_layers(source_bytes)

            # 2) Derive per-page images (stored as artifacts for reuse/display)
            page_images_result = motet.do(
                derive_pdf_page_images,
                data=DerivePdfPageImagesData(source_artifact_id=data.source_artifact_id),
            )
            page_images = page_images_result.get("pages", [])
            dpi = page_images_result.get("dpi", 300)
            if page_images_result.get("total_pages"):
                total_pages = int(page_images_result["total_pages"])

            # 3) OCR all pages in parallel (MapCommand via motet.apply)
            ocr_inputs = [
                {
                    "image_artifact_id": p["artifact_id"],
                    "content_type": p.get("content_type", "image/png"),
                    "page_num": p.get("page_num"),
                    "source_artifact_id": data.source_artifact_id,
                    "model_provider": model_provider,
                    "model_name": model_name,
                    "model_profile_name": model_profile_name,
                }
                for p in page_images
                if p.get("artifact_id") and p.get("page_num")
            ]
            # IMPORTANT: OCR pages can legitimately take >60s under provider load.
            # The default DistributedCommand timeout is often 60s, which causes MapCommand
            # to fail the whole batch. Make the timeout configurable and use a safer default.
            import os

            try:
                ocr_timeout_seconds = int(os.getenv("MOTET_PDF_OCR_TIMEOUT_SECONDS", "180"))
            except Exception:
                ocr_timeout_seconds = 180
            ocr_timeout_seconds = max(30, min(ocr_timeout_seconds, 1800))

            try:
                ocr_batch_size_env = os.getenv("MOTET_PDF_OCR_BATCH_SIZE", "").strip()
                ocr_batch_size = int(ocr_batch_size_env) if ocr_batch_size_env else None
            except Exception:
                ocr_batch_size = None
            if ocr_batch_size is not None:
                ocr_batch_size = max(1, min(ocr_batch_size, 64))

            ocr_results = (
                motet.apply(
                    ocr_image_page,
                    inputs=ocr_inputs,
                    timeout_seconds=ocr_timeout_seconds,
                    batch_size=ocr_batch_size,
                )
                if ocr_inputs
                else []
            )

            ocr_by_page = {}
            for r in ocr_results:
                if isinstance(r, dict) and r.get("page_num"):
                    ocr_by_page[int(r["page_num"])] = (r.get("text") or "")

            # 4) Combine per page
            combined_pages = []
            for page_num in range(1, max(total_pages, 1) + 1):
                combined = combine_text_layer_and_ocr(
                    page_num=page_num,
                    text_layer=text_layers.get(page_num, ""),
                    ocr_text=ocr_by_page.get(page_num, ""),
                )
                if combined:
                    combined_pages.append(combined)

            final_text = "\n\n".join(combined_pages).strip()
            if not final_text:
                logger.info("pdf_vision_ocr_yielded_empty_text", source_artifact_id=data.source_artifact_id)
                return {"status": "skipped", "reason": "empty_text"}

            # 5) Store derived text
            derived_id = motet.artifact_store.put(
                payload=final_text,
                content_type="text/plain",
                kind=ArtifactKind.DERIVED_TEXT,
                source_artifact_id=data.source_artifact_id,
                metadata={
                    "source_filename": source_meta.metadata.get("filename"),
                    "derivation_method": "pdf_vision_ocr_v1",
                    "page_images": len(page_images),
                    "ocr_pages": len([t for t in ocr_by_page.values() if t.strip()]),
                    "dpi": dpi,
                    "ocr_model": f"{(model_provider or 'openai')}:{(model_name or 'gpt-4.1-mini')}",
                },
                ttl_seconds=(source_meta.expires_at - source_meta.created_at) if source_meta.expires_at else None,
            )

            result = {
                "status": "success",
                "id": derived_id,
                "kind": ArtifactKind.DERIVED_TEXT,
                "bytes": len(final_text),
                "source_artifact_id": data.source_artifact_id,  # Include for frontend event tracking
            }
        else:
            # Non-PDF: use the service-layer extractor (pure utilities)
            result = derive_text_artifact(
                source_artifact_id=data.source_artifact_id,
                tenant_id=motet.tenant_id,
                principal_id=motet.principal_id or None,
                motet_id=motet.motet_id,
            )
            # Ensure source_artifact_id is in result for frontend event tracking
            # This is critical for skipped/error cases where result might not have it
            if isinstance(result, dict):
                if "source_artifact_id" not in result:
                    result["source_artifact_id"] = data.source_artifact_id
                # Also ensure status is set if missing
                if "status" not in result:
                    result["status"] = "success"

        if isinstance(result, dict):
            if result.get("status") == "success":
                _dispatch_artifact_rag_index(
                    motet=motet,
                    source_artifact_id=data.source_artifact_id,
                    derived_artifact_id=str(result.get("id") or "") or None,
                )
            elif result.get("status") == "skipped" and result.get("reason") == "empty_text":
                _dispatch_artifact_rag_source_index(
                    motet=motet,
                    source_artifact_id=data.source_artifact_id,
                    reason="derived_text_empty",
                )

        logger.info(
            "derive_upload_text_completed",
            **motet.log_fields(
                source_artifact_id=data.source_artifact_id,
                derived_artifact_id=(result or {}).get("id"),
            )
        )
        return result
    except DerivationError as e:
        logger.error(
            "derive_upload_text_failed",
            **motet.log_fields(
                source_artifact_id=data.source_artifact_id,
                error=str(e),
                error_type=type(e).__name__,
            ),
            exc_info=True,
        )
        raise
    except Exception as e:
        logger.error(
            "derive_upload_text_error",
            **motet.log_fields(
                source_artifact_id=data.source_artifact_id,
                error=str(e),
                error_type=type(e).__name__,
            ),
            exc_info=True,
        )
        raise


@motet.command(
    description="Derive image variants (thumb, base, detail) from an uploaded image and store them as derived artifacts.",
    timeout_seconds=300,
    required_capabilities=[WorkerCapability.FILE_OPERATIONS],
)
def derive_upload_image(data: DeriveUploadImageData) -> Dict[str, Any]:
    """
    Derive image artifacts (thumb, base, detail) from an uploaded image and store them as derived artifacts.

    Args:
        data: DeriveUploadImageData with the source artifact ID and optional derivation names

    Returns:
        Dict describing the derived artifacts (implementation-defined by derivation_service):
        {
            "status": "success",
            "derivations": {
                "thumb": {"id": "...", "bytes": int, ...},
                "base": {"id": "...", "bytes": int, ...}
            }
        }

    Raises:
        DerivationError: For expected derivation failures (unsupported type, processing errors)
        Exception: For unexpected system failures (will be wrapped by command framework)
    """
    motet = get_motet_context()

    logger.info(
        "derive_upload_image_started",
        source_artifact_id=data.source_artifact_id,
        derivation_names=data.derivation_names,
        force_regenerate=data.force_regenerate,
        tenant_id=motet.tenant_id,
        principal_id=motet.principal_id,
        motet_id=motet.motet_id,
        task_id=motet.task_id,
        command_id=motet.command_id,
    )

    try:
        result = derive_image_artifacts(
            source_artifact_id=data.source_artifact_id,
            tenant_id=motet.tenant_id,
            principal_id=motet.principal_id or None,
            motet_id=motet.motet_id,
            derivation_names=data.derivation_names,
            force_regenerate=data.force_regenerate,
        )

        logger.info(
            "derive_upload_image_completed",
            **motet.log_fields(
                source_artifact_id=data.source_artifact_id,
                derivation_count=len(result.get("derivations", {})),
            )
        )
        # Ensure source_artifact_id is in result for frontend event tracking
        if isinstance(result, dict) and "source_artifact_id" not in result:
            result["source_artifact_id"] = data.source_artifact_id
        return result
    except DerivationError as e:
        logger.error(
            "derive_upload_image_failed",
            **motet.log_fields(
                source_artifact_id=data.source_artifact_id,
                error=str(e),
                error_type=type(e).__name__,
            ),
            exc_info=True,
        )
        raise
    except Exception as e:
        logger.error(
            "derive_upload_image_error",
            **motet.log_fields(
                source_artifact_id=data.source_artifact_id,
                error=str(e),
                error_type=type(e).__name__,
            ),
            exc_info=True,
        )
        raise


@motet.command(
    description="Extract poster and keyframe images from an uploaded video for visual preview and inspection.",
    timeout_seconds=600,
    required_capabilities=[WorkerCapability.MEDIA_PROCESSING],
)
def derive_video_visuals(data: DeriveVideoVisualsData) -> Dict[str, Any]:
    """
    Derive poster and keyframe images from an uploaded video (ADR-0118/ADR-0119).

    Runs in parallel with derive_video_transcript. When transcription is enabled,
    only the transcript track dispatches source-level RAG re-index (including
    on transcript reuse) so a late visuals re-index cannot overwrite
    transcript_segment chunks.
    """

    motet = get_motet_context()

    try:
        strategy = KeyframeStrategy(data.keyframe_strategy)
    except ValueError as exc:
        raise DerivationError(f"Invalid keyframe_strategy: {data.keyframe_strategy}") from exc

    logger.info(
        "derive_video_visuals_started",
        source_artifact_id=data.source_artifact_id,
        keyframe_strategy=data.keyframe_strategy,
        max_keyframes=data.max_keyframes,
        force_regenerate=data.force_regenerate,
        tenant_id=motet.tenant_id,
        principal_id=motet.principal_id,
        motet_id=motet.motet_id,
        task_id=motet.task_id,
        command_id=motet.command_id,
    )

    try:
        result = derive_video_visual_artifacts(
            source_artifact_id=data.source_artifact_id,
            tenant_id=motet.tenant_id,
            principal_id=motet.principal_id or None,
            motet_id=motet.motet_id,
            keyframe_strategy=strategy,
            max_keyframes=data.max_keyframes,
            force_regenerate=data.force_regenerate,
        )

        logger.info(
            "derive_video_visuals_completed",
            **motet.log_fields(
                source_artifact_id=data.source_artifact_id,
                keyframe_count=len((result.get("derivations") or {}).get("keyframes") or []),
            ),
        )

        if isinstance(result, dict) and result.get("status") == "success":
            if not _video_transcription_will_own_rag_index(motet):
                _dispatch_artifact_rag_source_index(
                    motet=motet,
                    source_artifact_id=data.source_artifact_id,
                    reason="video_visuals_derived",
                )

        if isinstance(result, dict) and "source_artifact_id" not in result:
            result["source_artifact_id"] = data.source_artifact_id
        return result
    except DerivationError as e:
        logger.error(
            "derive_video_visuals_failed",
            **motet.log_fields(
                source_artifact_id=data.source_artifact_id,
                error=str(e),
                error_type=type(e).__name__,
            ),
            exc_info=True,
        )
        raise
    except Exception as e:
        logger.error(
            "derive_video_visuals_error",
            **motet.log_fields(
                source_artifact_id=data.source_artifact_id,
                error=str(e),
                error_type=type(e).__name__,
            ),
            exc_info=True,
        )
        raise


@motet.command(
    description="Transcribe an uploaded video into a text transcript artifact for search and summarization.",
    timeout_seconds=900,
    required_capabilities=[WorkerCapability.MEDIA_PROCESSING],
)
def derive_video_transcript(data: DeriveVideoTranscriptData) -> Dict[str, Any]:
    """
    Derive a transcript artifact from an uploaded video (ADR-0119).

    Always completes (success or structured skip) so attachment tracking can
    treat the transcript track as resolved; backend failures never fail the
    command. Network-bound openai_api work retries here without ever
    re-running keyframe extraction (that lives in derive_video_visuals).
    """

    motet = get_motet_context()

    logger.info(
        "derive_video_transcript_started",
        source_artifact_id=data.source_artifact_id,
        force_regenerate=data.force_regenerate,
        tenant_id=motet.tenant_id,
        principal_id=motet.principal_id,
        motet_id=motet.motet_id,
        task_id=motet.task_id,
        command_id=motet.command_id,
    )

    try:
        result = derive_video_transcript_artifact(
            source_artifact_id=data.source_artifact_id,
            tenant_id=motet.tenant_id,
            principal_id=motet.principal_id or None,
            motet_id=motet.motet_id,
            force_regenerate=data.force_regenerate,
        )

        logger.info(
            "derive_video_transcript_completed",
            **motet.log_fields(
                source_artifact_id=data.source_artifact_id,
                status=result.get("status"),
                reason=result.get("reason"),
                transcript_id=((result.get("derivations") or {}).get("transcript") or {}).get("id"),
            ),
        )

        if isinstance(result, dict):
            status = result.get("status")
            transcript_id = ((result.get("derivations") or {}).get("transcript") or {}).get("id")
            if status == "success" and transcript_id:
                _dispatch_artifact_rag_source_index(
                    motet=motet,
                    source_artifact_id=data.source_artifact_id,
                    reason=(
                        "video_transcript_reused"
                        if result.get("reused")
                        else "video_transcript_derived"
                    ),
                )
            elif status == "skipped" and result.get("reason") in {
                "no_audio_stream",
                "no_transcript_produced",
            }:
                _dispatch_artifact_rag_source_index(
                    motet=motet,
                    source_artifact_id=data.source_artifact_id,
                    reason=f"video_transcript_{result.get('reason')}",
                )

        if isinstance(result, dict) and "source_artifact_id" not in result:
            result["source_artifact_id"] = data.source_artifact_id
        return result
    except DerivationError as e:
        logger.error(
            "derive_video_transcript_failed",
            **motet.log_fields(
                source_artifact_id=data.source_artifact_id,
                error=str(e),
                error_type=type(e).__name__,
            ),
            exc_info=True,
        )
        raise
    except Exception as e:
        logger.error(
            "derive_video_transcript_error",
            **motet.log_fields(
                source_artifact_id=data.source_artifact_id,
                error=str(e),
                error_type=type(e).__name__,
            ),
            exc_info=True,
        )
        raise


@motet.command(
    description="Extract embedded images from DOCX/PPTX uploads and store them as derived artifacts.",
    timeout_seconds=300,
    required_capabilities=[WorkerCapability.FILE_OPERATIONS],
)
def derive_office_embedded_images(data: DeriveOfficeEmbeddedImagesData) -> Dict[str, Any]:
    """
    Extract embedded images from an uploaded DOCX/PPTX document and store them as derived artifacts.

    The extracted image artifacts become source images for the existing image resize derivation command.
    MediaPart objects are still created at context-render time; this command persists the durable payloads
    and relationship metadata needed to materialize those parts later.
    """

    from motet.core.artifacts import ArtifactKind
    from motet.core.media.exceptions import ArtifactPayloadMissingError, SourceArtifactNotFoundError
    from motet.core.media.office_embedded_images import extract_office_embedded_images, is_office_embedded_image_eligible
    from motet.core.media.utils import normalize_to_bytes

    motet = get_motet_context()

    logger.info(
        "derive_office_embedded_images_started",
        **motet.log_fields(
            source_artifact_id=data.source_artifact_id,
            image_derivation_names=data.image_derivation_names,
            force_regenerate=data.force_regenerate,
        ),
    )

    source_meta = motet.artifact_store.get_metadata(data.source_artifact_id)
    if not source_meta:
        raise SourceArtifactNotFoundError(f"Source artifact {data.source_artifact_id} not found")
    if not is_office_embedded_image_eligible(source_meta.content_type):
        raise DerivationError(f"Source artifact is not supported for embedded-image extraction: {source_meta.content_type}")

    source_payload = motet.artifact_store.get(data.source_artifact_id)
    if not source_payload:
        raise ArtifactPayloadMissingError("Source artifact payload missing")
    source_bytes = normalize_to_bytes(source_payload)

    existing_by_key: dict[tuple[str, str], Any] = {}
    if not data.force_regenerate:
        try:
            existing = motet.artifact_store.list(
                kind=ArtifactKind.DERIVED_EMBEDDED_IMAGE,
                source_artifact_id=data.source_artifact_id,
                limit=1000,
            )
            for meta in existing:
                key = (
                    str(meta.metadata.get("embedded_image_path") or ""),
                    str(meta.metadata.get("checksum_sha256") or meta.checksum_sha256 or ""),
                )
                if key[0] and key[1]:
                    existing_by_key[key] = meta
        except Exception as e:
            logger.warning(
                "office_embedded_image_existing_list_failed",
                **motet.log_fields(source_artifact_id=data.source_artifact_id, error=str(e), error_type=type(e).__name__),
                exc_info=True,
            )

    extracted = extract_office_embedded_images(source_bytes, source_meta.content_type)
    embedded_images: list[dict[str, Any]] = []
    resize_children = []
    ocr_children = []

    import hashlib

    for image in extracted:
        checksum = hashlib.sha256(image.payload).hexdigest()
        existing = existing_by_key.get((image.package_path, checksum))
        if existing is not None:
            image_artifact_id = existing.id
            reused = True
        else:
            metadata = {
                "source_filename": source_meta.metadata.get("filename"),
                "source_content_type": source_meta.content_type,
                "checksum_sha256": checksum,
                **image.metadata,
            }
            image_artifact_id = motet.artifact_store.put(
                payload=image.payload,
                content_type=image.content_type,
                kind=ArtifactKind.DERIVED_EMBEDDED_IMAGE,
                source_artifact_id=data.source_artifact_id,
                metadata=metadata,
                ttl_seconds=(source_meta.expires_at - source_meta.created_at) if source_meta.expires_at else None,
            )
            reused = False

        embedded_images.append(
            {
                "artifact_id": image_artifact_id,
                "content_type": image.content_type,
                "bytes": len(image.payload),
                "ordinal": image.ordinal,
                "package_path": image.package_path,
                "reused": reused,
                "metadata": image.metadata,
            }
        )

        derivation_names = list(data.image_derivation_names or ["thumb", "base"])
        if derivation_names:
            resize_children.append(
                derive_upload_image(
                    task_id=motet.task_id or "",
                    conversation_id=motet.conversation_id or "",
                    tenant_id=motet.tenant_id,
                    principal_id=motet.principal_id,
                    motet_id=motet.motet_id,
                    data=DeriveUploadImageData(
                        source_artifact_id=image_artifact_id,
                        derivation_names=derivation_names,
                        force_regenerate=data.force_regenerate,
                    ),
                )
            )

        should_ocr = bool(image.metadata.get("embedded_image_should_ocr", True))
        if data.run_ocr and should_ocr:
            ocr_children.append(
                ocr_embedded_image(
                    task_id=motet.task_id or "",
                    conversation_id=motet.conversation_id or "",
                    tenant_id=motet.tenant_id,
                    principal_id=motet.principal_id,
                    motet_id=motet.motet_id,
                    data=OCREmbeddedImageData(
                        source_artifact_id=data.source_artifact_id,
                        image_artifact_id=image_artifact_id,
                        content_type=image.content_type,
                        model_provider=data.model_provider,
                        model_name=data.model_name,
                        model_profile_name=data.model_profile_name,
                    ),
                )
            )

    task_ids: list[str] = []
    if resize_children:
        try:
            task_ids = motet.dispatch(resize_children)
        except Exception as e:
            logger.warning(
                "office_embedded_image_derivation_dispatch_failed",
                **motet.log_fields(
                    source_artifact_id=data.source_artifact_id,
                    child_count=len(resize_children),
                    error=str(e),
                    error_type=type(e).__name__,
                ),
                exc_info=True,
            )

    ocr_task_ids: list[str] = []
    if ocr_children:
        try:
            ocr_task_ids = motet.dispatch(ocr_children)
        except Exception as e:
            logger.warning(
                "office_embedded_image_ocr_dispatch_failed",
                **motet.log_fields(
                    source_artifact_id=data.source_artifact_id,
                    child_count=len(ocr_children),
                    error=str(e),
                    error_type=type(e).__name__,
                ),
                exc_info=True,
            )

    logger.info(
        "derive_office_embedded_images_completed",
        **motet.log_fields(
            source_artifact_id=data.source_artifact_id,
            extracted_count=len(extracted),
            stored_count=len([item for item in embedded_images if not item["reused"]]),
            reused_count=len([item for item in embedded_images if item["reused"]]),
            resize_child_count=len(resize_children),
            ocr_child_count=len(ocr_children),
        ),
    )

    return {
        "status": "success",
        "source_artifact_id": data.source_artifact_id,
        "embedded_images": embedded_images,
        "resize_derivations": {
            "child_command_ids": [child.command_id for child in resize_children],
            "task_ids": task_ids,
        },
        "ocr_derivations": {
            "child_command_ids": [child.command_id for child in ocr_children],
            "task_ids": ocr_task_ids,
        },
    }


@motet.command(
    description="OCR one embedded office image, store DERIVED_OCR text, and queue artifact RAG indexing.",
    timeout_seconds=300,
    required_capabilities=[WorkerCapability.MODEL_INFERENCE],
)
def ocr_embedded_image(data: OCREmbeddedImageData) -> Dict[str, Any]:
    """
    OCR one embedded office image, store the text as DERIVED_OCR, and dispatch artifact RAG indexing.

    The OCR text is indexed against the original office document source artifact while retaining
    metadata that points back to the embedded image artifact.
    """

    from motet.core.artifacts import ArtifactKind
    from motet.core.media.exceptions import ArtifactPayloadMissingError, SourceArtifactNotFoundError

    motet = get_motet_context()

    logger.info(
        "ocr_embedded_image_started",
        **motet.log_fields(
            source_artifact_id=data.source_artifact_id,
            image_artifact_id=data.image_artifact_id,
        ),
    )

    source_meta = motet.artifact_store.get_metadata(data.source_artifact_id)
    if not source_meta:
        raise SourceArtifactNotFoundError(f"Source artifact {data.source_artifact_id} not found")
    image_meta = motet.artifact_store.get_metadata(data.image_artifact_id)
    if not image_meta:
        raise SourceArtifactNotFoundError(f"Embedded image artifact {data.image_artifact_id} not found")
    if image_meta.source_artifact_id != data.source_artifact_id:
        raise DerivationError(
            f"Embedded image {data.image_artifact_id} is not linked to source {data.source_artifact_id}"
        )

    image_payload = motet.artifact_store.get(data.image_artifact_id)
    if not image_payload:
        raise ArtifactPayloadMissingError("Embedded image payload missing")

    existing = motet.artifact_store.list(
        kind=ArtifactKind.DERIVED_OCR,
        source_artifact_id=data.source_artifact_id,
        limit=1000,
    )
    for meta in existing:
        if (getattr(meta, "metadata", {}) or {}).get("embedded_image_artifact_id") == data.image_artifact_id:
            logger.info(
                "ocr_embedded_image_reused_existing",
                **motet.log_fields(
                    source_artifact_id=data.source_artifact_id,
                    image_artifact_id=data.image_artifact_id,
                    ocr_artifact_id=meta.id,
                ),
            )
            _dispatch_artifact_rag_index(
                motet=motet,
                source_artifact_id=data.source_artifact_id,
                derived_artifact_id=meta.id,
            )
            return {
                "status": "success",
                "source_artifact_id": data.source_artifact_id,
                "image_artifact_id": data.image_artifact_id,
                "ocr_artifact_id": meta.id,
                "reused": True,
                "bytes": meta.bytes,
            }

    ocr_result = motet.do(
        ocr_image_page,
        data=OCRImagePageData(
            image_artifact_id=data.image_artifact_id,
            content_type=data.content_type,
            page_num=(getattr(image_meta, "metadata", {}) or {}).get("slide_num"),
            source_artifact_id=data.source_artifact_id,
            model_provider=data.model_provider,
            model_name=data.model_name,
            model_profile_name=data.model_profile_name,
        ),
    )
    text = str((ocr_result or {}).get("text") or "").strip()
    if not text:
        return {
            "status": "skipped",
            "reason": "empty_ocr_text",
            "source_artifact_id": data.source_artifact_id,
            "image_artifact_id": data.image_artifact_id,
        }

    image_md = getattr(image_meta, "metadata", {}) or {}
    source_md = getattr(source_meta, "metadata", {}) or {}
    metadata = {
        "source_filename": source_md.get("filename"),
        "source_content_type": source_meta.content_type,
        "embedded_image_artifact_id": data.image_artifact_id,
        "embedded_image_content_type": data.content_type,
        "derivation_method": "office_embedded_image_ocr_v1",
        "derivation_source": "embedded_image",
        "embedded_image_path": image_md.get("embedded_image_path"),
        "embedded_image_ordinal": image_md.get("embedded_image_ordinal"),
        "relationship_id": image_md.get("relationship_id"),
        "office_document_type": image_md.get("office_document_type"),
        "slide_num": image_md.get("slide_num"),
        "embedded_image_name": image_md.get("embedded_image_name"),
        "embedded_image_alt_text": image_md.get("embedded_image_alt_text"),
        "ocr_attempts": (ocr_result or {}).get("attempts"),
    }
    derived_id = motet.artifact_store.put(
        payload=text,
        content_type="text/plain",
        kind=ArtifactKind.DERIVED_OCR,
        source_artifact_id=data.source_artifact_id,
        metadata={k: v for k, v in metadata.items() if v is not None},
        ttl_seconds=(source_meta.expires_at - source_meta.created_at) if source_meta.expires_at else None,
    )

    _dispatch_artifact_rag_index(
        motet=motet,
        source_artifact_id=data.source_artifact_id,
        derived_artifact_id=derived_id,
    )

    logger.info(
        "ocr_embedded_image_completed",
        **motet.log_fields(
            source_artifact_id=data.source_artifact_id,
            image_artifact_id=data.image_artifact_id,
            ocr_artifact_id=derived_id,
            text_length=len(text),
        ),
    )

    return {
        "status": "success",
        "source_artifact_id": data.source_artifact_id,
        "image_artifact_id": data.image_artifact_id,
        "ocr_artifact_id": derived_id,
        "reused": False,
        "bytes": len(text),
    }


@motet.command(
    description="Convert a PDF into per-page PNG images stored as derived artifacts for viewing and OCR.",
    timeout_seconds=300,
    required_capabilities=[WorkerCapability.FILE_OPERATIONS],
)
def derive_pdf_page_images(data: DerivePdfPageImagesData) -> Dict[str, Any]:
    """
    Convert a PDF into per-page PNG images and store them as artifacts (ADR-0062).

    This command is designed to be reused:
    - Page images can be shown in UI (preview/debug)
    - OCR can be re-run without re-rasterizing the PDF
    """
    import io

    motet = get_motet_context()
    from motet.core.artifacts import ArtifactKind
    from motet.core.media.utils import normalize_to_bytes
    from motet.core.media.exceptions import SourceArtifactNotFoundError, ArtifactPayloadMissingError

    logger.info(
        "derive_pdf_page_images_started",
        **motet.log_fields(
            source_artifact_id=data.source_artifact_id,
            dpi=data.dpi,
            force_regenerate=data.force_regenerate,
        )
    )

    source_meta = motet.artifact_store.get_metadata(data.source_artifact_id)
    if not source_meta:
        raise SourceArtifactNotFoundError(f"Source artifact {data.source_artifact_id} not found")
    if source_meta.content_type != "application/pdf":
        raise DerivationError(f"Source artifact is not a PDF: {source_meta.content_type}")

    source_bytes = motet.artifact_store.get(data.source_artifact_id)
    if not source_bytes:
        raise ArtifactPayloadMissingError("Source artifact payload missing")
    
    source_bytes = normalize_to_bytes(source_bytes)

    # Determine total pages (for completeness checks)
    total_pages = 0
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(source_bytes)) as pdf:
            total_pages = len(pdf.pages)
    except Exception as e:  # pragma: no cover
        logger.warning("pdf_page_count_failed", error=str(e))

    # Find existing page images
    existing_by_page = {}
    try:
        existing = motet.artifact_store.list(
            kind=ArtifactKind.DERIVED_PAGE_IMAGE,
            source_artifact_id=data.source_artifact_id,
            limit=1000,
        )
        for meta in existing:
            pn = meta.metadata.get("page_num")
            if isinstance(pn, int):
                existing_by_page[pn] = meta
    except Exception as e:
        logger.warning("page_image_list_failed", error=str(e), exc_info=True)

    if (not data.force_regenerate) and total_pages and len(existing_by_page) >= total_pages:
        pages = [
            {
                "page_num": pn,
                "artifact_id": meta.id,
                "content_type": meta.content_type,
                "bytes": meta.bytes,
            }
            for pn, meta in sorted(existing_by_page.items())
            if pn <= total_pages
        ]
        logger.info(
            "derive_pdf_page_images_reused_existing",
            source_artifact_id=data.source_artifact_id,
            total_pages=total_pages,
            reused_pages=len(pages),
        )
        return {"total_pages": total_pages, "dpi": data.dpi, "pages": pages}

    import os
    import time

    t0 = time.perf_counter()

    # PDFium only (ADR-0080: Poppler/pdf2image GPL fallback removed for opaque runtime distribution).
    renderer = (os.getenv("MOTET_PDF_PAGE_IMAGE_RENDERER", "pdfium") or "pdfium").strip().lower()
    if renderer != "pdfium":
        logger.warning(
            "pdf_page_image_renderer_ignored_using_pdfium",
            renderer=renderer,
            message="Only pdfium is supported; MOTET_PDF_PAGE_IMAGE_RENDERER ignored.",
        )
        renderer = "pdfium"

    t_render_start = time.perf_counter()
    try:
        import pypdfium2 as pdfium  # type: ignore[import-not-found]
    except ImportError as e:
        raise DerivationError(
            f"pypdfium2 is required for PDF page image rendering (install with: pip install pypdfium2): {e}"
        ) from e

    # PDFium uses 72 DPI as the base user space resolution.
    scale = float(int(data.dpi)) / 72.0
    pdf = pdfium.PdfDocument(source_bytes)
    try:
        images = []
        for i in range(len(pdf)):
            page = pdf[i]
            bitmap = page.render(scale=scale)  # type: ignore[arg-type]
            images.append(bitmap.to_pil())
    finally:
        try:
            pdf.close()
        except Exception:
            pass  # best-effort resource cleanup

    t_render_end = time.perf_counter()
    if not total_pages:
        total_pages = len(images)

    pages = []
    encode_seconds = 0.0
    store_seconds = 0.0
    for page_num, img in enumerate(images, 1):
        if (not data.force_regenerate) and (page_num in existing_by_page):
            meta = existing_by_page[page_num]
            pages.append(
                {
                    "page_num": page_num,
                    "artifact_id": meta.id,
                    "content_type": meta.content_type,
                    "bytes": meta.bytes,
                }
            )
            continue

        t_encode_start = time.perf_counter()
        img_bytes_io = io.BytesIO()
        # PNG optimize=True can be very CPU-expensive and dominates runtime for many PDFs.
        # For OCR, lossless is important, but maximum compression is not; keep it fast.
        img.save(img_bytes_io, format="PNG", optimize=False, compress_level=3)
        img_bytes = img_bytes_io.getvalue()
        encode_seconds += time.perf_counter() - t_encode_start
        width, height = getattr(img, "size", (None, None))

        t_store_start = time.perf_counter()
        derived_id = motet.artifact_store.put(
            payload=img_bytes,
            content_type="image/png",
            kind=ArtifactKind.DERIVED_PAGE_IMAGE,
            source_artifact_id=data.source_artifact_id,
            metadata={
                "page_num": page_num,
                "dpi": int(data.dpi),
                "width": width,
                "height": height,
                "source_filename": source_meta.metadata.get("filename"),
            },
            ttl_seconds=None,  # Keep for reuse/display
        )
        store_seconds += time.perf_counter() - t_store_start
        pages.append(
            {
                "page_num": page_num,
                "artifact_id": derived_id,
                "content_type": "image/png",
                "bytes": len(img_bytes),
                "width": width,
                "height": height,
            }
        )

    # Single-threaded render loop (no WorkerExecutor used here)
    thread_count = 1
    timing = {
        "renderer": renderer,
        "dpi": int(data.dpi),
        "thread_count": thread_count,
        "render_seconds": (t_render_end - t_render_start),
        "encode_seconds": encode_seconds,
        "store_seconds": store_seconds,
        "total_seconds": (time.perf_counter() - t0),
    }

    logger.info(
        "derive_pdf_page_images_completed",
        **motet.log_fields(
            source_artifact_id=data.source_artifact_id,
            total_pages=total_pages,
            page_count=len(pages),
            **timing,
        )
    )

    return {
        "total_pages": total_pages,
        "dpi": int(data.dpi),
        "pages": pages,
        "source_artifact_id": data.source_artifact_id,
        # Include timing in the result so UIs/clients can display it without scraping worker logs.
        "timing": timing,
    }


@motet.command(
    description="OCR an image page with a vision model and store extracted text for search and citation.",
    timeout_seconds=60,
    required_capabilities=[WorkerCapability.MODEL_INFERENCE],
)
def ocr_image_page(data: OCRImagePageData) -> Dict[str, Any]:
    """
    Extract text from an image using vision model OCR.
    
    This command is used for parallel OCR processing of PDF pages and other images.
    It can be executed in parallel using motet.join() for multi-page documents.
    
    Args:
        data: OCRImagePageData with image artifact ID and metadata
        
    Returns:
        Dict with extracted text:
        {
            "text": str,  # Extracted text from the image
            "page_num": int,  # Page number if applicable
            "artifact_id": str  # Image artifact ID that was OCR'd
        }
        
    Raises:
        Exception: If OCR fails (will be wrapped by command framework)
    """
    from motet.core.types import Message, MediaPart, RequestContext
    from motet.core.commands.builtin.model import model_inference
    from motet.core.commands.command_data_classes import ModelInferenceData
    
    motet = get_motet_context()
    
    logger.info(
        "ocr_image_page_started",
        **motet.log_fields(
            image_artifact_id=data.image_artifact_id,
            page_num=data.page_num,
            source_artifact_id=data.source_artifact_id,
        )
    )
    
    def _looks_like_summary(text: str) -> bool:
        """
        Heuristic detector for summary/assistant-y responses when we asked for verbatim OCR.

        If it triggers, we retry once with stronger instructions and/or a stronger model.
        """
        t = (text or "").strip()
        if not t:
            return True
        lower = t.lower()
        summary_markers = (
            "the article",
            "this article",
            "discusses",
            "highlights",
            "mentions",
            "it looks like you've shared",
            "if you have any questions",
        )
        if any(m in lower for m in summary_markers):
            return True
        # For long document images, we generally expect multiple line breaks.
        if "\n" not in t and len(t) > 200:
            return True
        return False

    def _build_ocr_messages(*, strict: bool) -> list[Message]:
        system = Message(
            role="system",
            content=(
                "You are a strict OCR transcription engine. Output verbatim text only. "
                "Never summarize, paraphrase, interpret, explain, or add commentary."
            ),
        )
        user = Message(
            role="user",
            content=(
                "TRANSCRIBE VERBATIM: Extract ALL text from the image exactly as it appears.\n"
                "Rules:\n"
                "- Output ONLY the transcription text (no preamble, no explanations, no markdown).\n"
                "- Preserve line breaks, spacing, punctuation, and capitalization.\n"
                "- Do NOT summarize or paraphrase.\n"
                "- If a character/word is unreadable, output [UNK] in its place; do not guess.\n"
                + (
                    "\nHard rule: If you are about to summarize (e.g., 'The article discusses...'), stop and instead transcribe verbatim."
                    if strict
                    else ""
                )
            ),
            content_parts=[
                MediaPart(
                    media_type="image",
                    mime_type=data.content_type,
                    artifact_id=data.image_artifact_id,
                    detail="high",  # High detail for OCR accuracy
                )
            ],
        )
        return [system, user]

    def _call_ocr_model(*, provider: str, model_name: str, strict: bool) -> tuple[str, float]:
        t_start = time.perf_counter()
        # Model calls frequently exceed 60s under load for high-detail vision OCR.
        # Override the default model_inference timeout to avoid flakey "Task timed out after 60s".
        import os

        try:
            model_timeout_seconds = int(os.getenv("MOTET_OCR_MODEL_TIMEOUT_SECONDS", "180"))
        except Exception:
            model_timeout_seconds = 180
        model_timeout_seconds = max(30, min(model_timeout_seconds, 1800))

        result = motet.do(
            model_inference,
            data=ModelInferenceData(
                messages=_build_ocr_messages(strict=strict),
                model_settings={
                    "provider": provider,
                    "model_name": model_name,
                    "temperature": 0.0,  # Deterministic for OCR
                    "max_tokens": 4000,
                },
                request_context=RequestContext(
                    tenant_id=motet.tenant_id,
                    principal_id=motet.principal_id,
                    motet_id=motet.motet_id,
                    model_profile_name=(getattr(data, "model_profile_name", None) or None),
                    enable_multimodal=True,  # Ensure images are rendered even if model registry is unavailable
                    max_images=1,
                    max_image_bytes=20 * 1024 * 1024,
                ),
            ),
            timeout_seconds=model_timeout_seconds,
        )
        elapsed = time.perf_counter() - t_start
        return (result.get("content", "") or "").strip(), elapsed

    try:
        import time

        t0 = time.perf_counter()
        attempts: list[dict[str, Any]] = []

        provider = str(getattr(data, "model_provider", None) or "openai").strip().lower()
        if provider not in {"openai", "anthropic", "moonshot"}:
            provider = "openai"
        base_model = str(getattr(data, "model_name", None) or "").strip()
        if not base_model:
            base_model = "gpt-4.1-mini"

        # Attempt 1: preferred model (or default)
        extracted_text, attempt1_seconds = _call_ocr_model(provider=provider, model_name=base_model, strict=False)
        attempts.append(
            {
                "provider": provider,
                "model_name": base_model,
                "strict": False,
                "text_length": len(extracted_text),
                "seconds": attempt1_seconds,
            }
        )

        # Retry once if it looks like a summary/non-verbatim response.
        if _looks_like_summary(extracted_text):
            # Retry with stricter prompt. For OpenAI default path, we can bump model unless the caller
            # explicitly requested a model.
            retry_model = base_model
            if (not getattr(data, "model_name", None)) and provider == "openai":
                retry_model = "gpt-4.1"

            extracted_text_retry, attempt2_seconds = _call_ocr_model(provider=provider, model_name=retry_model, strict=True)
            attempts.append(
                {
                    "provider": provider,
                    "model_name": retry_model,
                    "strict": True,
                    "text_length": len(extracted_text_retry),
                    "seconds": attempt2_seconds,
                }
            )
            if extracted_text_retry:
                extracted_text = extracted_text_retry

        logger.info(
            "ocr_image_page_completed",
            **motet.log_fields(
                image_artifact_id=data.image_artifact_id,
                page_num=data.page_num,
                text_length=len(extracted_text),
                looked_like_summary=_looks_like_summary(extracted_text),
                attempts=attempts,
            )
        )

        return {
            "text": extracted_text,
            "page_num": data.page_num,
            "artifact_id": data.image_artifact_id,
            "source_artifact_id": data.source_artifact_id,  # Include for frontend event tracking
            "attempts": attempts,
            "timing": {
                "total_seconds": (time.perf_counter() - t0),
                "attempts": attempts,
            },
        }

    except Exception as e:
        logger.error(
            "ocr_image_page_failed",
            **motet.log_fields(
                image_artifact_id=data.image_artifact_id,
                page_num=data.page_num,
                error=str(e),
                error_type=type(e).__name__,
            ),
            exc_info=True,
        )
        # Return empty text on failure (allows other pages to continue)
        return {
            "text": "",
            "page_num": data.page_num,
            "artifact_id": data.image_artifact_id,
            "source_artifact_id": data.source_artifact_id,  # Include for frontend event tracking
            "error": str(e),
        }


__all__ = [
    "derive_upload_text",
    "derive_upload_image",
    "derive_video_visuals",
    "derive_video_transcript",
    "derive_office_embedded_images",
    "ocr_embedded_image",
    "derive_pdf_page_images",
    "ocr_image_page",
]



