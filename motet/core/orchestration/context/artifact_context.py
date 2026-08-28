"""
Motet - Artifact Context Provider

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Implements attachment-derived text and multimodal content-part assembly for
    the context preparation provider pipeline. It discovers recent conversation
    artifacts, auto-includes relevant prior artifacts on the current user turn,
    attaches image media parts, injects derived document text, and injects
    derived video transcripts.

Dependencies:
    - hashlib for duplicate derived-text detection
    - time for provider timing metrics
    - motet.core.artifacts for artifact kind and store lookup
    - motet.core.media.derivation_policy for image derivation selection
    - motet.core.types for canonical text and media content parts

Usage:
    state = ArtifactContextProvider().apply(state, data=data, motet=motet, logger=logger)

Notes:
    - Historical message attachment lookups are intentionally avoided. Prior
      artifacts are re-included on the current user message to prevent
      O(history * attachments) Redis lookups as conversations grow.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, List, Optional

from ...artifacts import ArtifactKind, get_artifact_store
from ...media.derivation_policy import get_derivation_kind, select_image_derivation
from ...types import MediaPart, TextPart
from .types import ContextPipelineState


class ArtifactContextProvider:
    """Inject derived artifact text and multimodal content parts."""

    name = "artifact_context"

    def apply(
        self,
        state: ContextPipelineState,
        *,
        data: Any,
        motet: Any,
        logger: Any,
    ) -> ContextPipelineState:
        try:
            fallback_store = get_artifact_store()
            artifact_store = getattr(motet, "artifact_store", None) or fallback_store

            recent_artifacts: Dict[str, Dict[str, Any]] = {}
            for msg in state.messages[:-1]:
                attachments = getattr(msg, "attachments", None)
                if attachments:
                    for att in attachments:
                        if isinstance(att, dict):
                            artifact_id = str(att.get("artifact_id") or "")
                            if artifact_id:
                                recent_artifacts[artifact_id] = att

            logger.info(
                "prepare_context_artifacts_from_history",
                conversation_id=motet.conversation_id,
                artifacts_found=len(recent_artifacts),
                artifact_ids=list(recent_artifacts.keys())[:5],
            )

            if motet.conversation_id:
                try:
                    conversation_artifacts = artifact_store.list(
                        kind=ArtifactKind.USER_UPLOAD,
                        conversation_id=motet.conversation_id,
                        limit=10,
                    )
                    logger.info(
                        "prepare_context_artifacts_from_store",
                        conversation_id=motet.conversation_id,
                        artifacts_found=len(conversation_artifacts),
                        artifact_ids=[a.id for a in conversation_artifacts[:5]],
                    )
                    for meta in conversation_artifacts:
                        artifact_id = meta.id
                        if artifact_id and artifact_id not in recent_artifacts:
                            recent_artifacts[artifact_id] = {
                                "artifact_id": artifact_id,
                                "filename": meta.metadata.get("filename", "file"),
                                "content_type": meta.content_type,
                                "bytes": meta.bytes,
                            }
                except Exception as e:
                    logger.warning("conversation_artifact_query_failed", error=str(e), exc_info=True)
            else:
                logger.info(
                    "prepare_context_no_conversation_id",
                    reason="conversation_id_missing",
                    motet_conversation_id=getattr(motet, "conversation_id", None),
                )

            last_msg = state.messages[-1] if state.messages else None
            artifact_list: List[Dict[str, Any]] = []
            logger.info(
                "prepare_context_checking_last_message",
                conversation_id=motet.conversation_id,
                has_last_msg=last_msg is not None,
                last_msg_role=last_msg.role if last_msg else None,
                recent_artifacts_count=len(recent_artifacts),
            )

            if last_msg and last_msg.role == "user" and recent_artifacts:
                current_attachments = getattr(last_msg, "attachments", None) or []
                logger.info(
                    "prepare_context_last_message_attachments",
                    conversation_id=motet.conversation_id,
                    has_current_attachments=bool(current_attachments),
                    current_attachment_count=len(current_attachments),
                    recent_artifacts_to_include=len(recent_artifacts),
                )

                current_artifact_ids = {
                    str(att.get("artifact_id", ""))
                    for att in current_attachments
                    if isinstance(att, dict) and att.get("artifact_id")
                }
                artifacts_to_include = [
                    artifact
                    for artifact in recent_artifacts.values()
                    if str(artifact.get("artifact_id", "")) not in current_artifact_ids
                ]

                image_artifacts = [
                    a for a in artifacts_to_include if str(a.get("content_type", "")).startswith("image/")
                ]
                document_artifacts = []
                other_artifacts = []
                document_types = {
                    "application/pdf",
                    "application/msword",
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "text/plain",
                    "text/markdown",
                    "text/html",
                }
                document_extensions = {".pdf", ".doc", ".docx", ".pptx", ".txt", ".md", ".html", ".xlsx", ".xls"}

                for artifact in artifacts_to_include:
                    if str(artifact.get("content_type", "")).startswith("image/"):
                        continue
                    content_type = str(artifact.get("content_type", "")).lower()
                    filename = str(artifact.get("filename", "")).lower()
                    is_document = content_type in document_types or any(
                        filename.endswith(ext) for ext in document_extensions
                    )
                    if is_document:
                        document_artifacts.append(artifact)
                    else:
                        other_artifacts.append(artifact)

                artifact_list = (image_artifacts[-5:] + document_artifacts[-3:] + other_artifacts[-1:])[:9]

                if artifact_list:
                    image_count = sum(
                        1 for artifact in artifact_list if str(artifact.get("content_type", "")).startswith("image/")
                    )
                    document_count = len(artifact_list) - image_count
                    logger.info(
                        "auto_included_conversation_artifacts",
                        conversation_id=motet.conversation_id,
                        artifact_count=len(artifact_list),
                        image_count=image_count,
                        document_count=document_count,
                        artifact_ids=[a.get("artifact_id") for a in artifact_list],
                        current_attachments_count=len(current_attachments),
                        reason="content_parts_only_not_attachments",
                    )
                elif not artifacts_to_include:
                    logger.info(
                        "prepare_context_skipping_auto_include",
                        conversation_id=motet.conversation_id,
                        reason="all_artifacts_already_in_current_message",
                    )
                else:
                    logger.info(
                        "prepare_context_no_artifacts_to_include",
                        conversation_id=motet.conversation_id,
                        recent_artifacts_count=len(recent_artifacts),
                        artifacts_to_include_count=len(artifacts_to_include),
                        reason="filtered_out_or_empty",
                    )
            else:
                logger.info(
                    "prepare_context_no_artifacts_available",
                    conversation_id=motet.conversation_id,
                    has_last_msg=last_msg is not None,
                    last_msg_role=last_msg.role if last_msg else None,
                    recent_artifacts_count=len(recent_artifacts),
                    reason="no_artifacts_found_or_not_user_message",
                )

            self._apply_current_message_artifacts(
                state,
                artifact_list=artifact_list,
                artifact_store=artifact_store,
                last_msg=last_msg,
                motet=motet,
                logger=logger,
            )
        except Exception as e:
            logger.warning("attachment_processing_failed", error=str(e))

        return state

    def _apply_current_message_artifacts(
        self,
        state: ContextPipelineState,
        *,
        artifact_list: List[Dict[str, Any]],
        artifact_store: Any,
        last_msg: Any,
        motet: Any,
        logger: Any,
    ) -> None:
        t0 = time.perf_counter()
        injected_text_artifact_ids: set[str] = set()
        injected_text_hashes: set[str] = set()
        injected_image_artifact_ids: set[str] = set()
        derived_text_cache: Dict[str, Optional[str]] = {}
        derived_transcript_cache: Dict[str, Optional[str]] = {}
        derived_image_cache: Dict[str, Optional[Any]] = {}
        _VIDEO_MAX_KEYFRAMES = 4

        for msg in state.messages:
            attachments = getattr(msg, "attachments", None)
            is_current_message = msg is last_msg
            effective_attachments = list(attachments or [])
            if is_current_message and artifact_list:
                effective_attachments = effective_attachments + artifact_list
            if not effective_attachments:
                continue

            content_parts = list(getattr(msg, "content_parts", None) or [])
            if not content_parts:
                content_parts.append(TextPart(text=msg.content))

            for att in effective_attachments:
                if not isinstance(att, dict):
                    continue

                content_type = str(att.get("content_type") or "")
                artifact_id = str(att.get("artifact_id") or "")
                if not artifact_id:
                    continue

                filename = str(att.get("filename") or "")
                is_image, content_type = self._is_image_attachment(
                    content_type,
                    filename,
                    artifact_id=artifact_id,
                    logger=logger,
                )
                if is_image:
                    if not is_current_message:
                        continue

                    derived_artifact_id, image_part_content_type = self._resolve_image_artifact(
                        artifact_id=artifact_id,
                        content_type=content_type,
                        message_content=getattr(msg, "content", ""),
                        cache=derived_image_cache,
                        artifact_store=artifact_store,
                        motet=motet,
                        logger=logger,
                    )
                    content_parts.append(
                        MediaPart(
                            media_type="image",
                            mime_type=image_part_content_type,
                            artifact_id=derived_artifact_id,
                            detail="auto",
                        )
                    )
                    injected_image_artifact_ids.add(derived_artifact_id)
                    continue

                is_video, content_type = self._is_video_attachment(
                    content_type,
                    filename,
                    artifact_id=artifact_id,
                    logger=logger,
                )
                if is_video:
                    if not is_current_message:
                        continue

                    transcript_id = self._resolve_derived_video_transcript_id(
                        att=att,
                        artifact_id=artifact_id,
                        filename=filename,
                        content_type=content_type,
                        cache=derived_transcript_cache,
                        artifact_store=artifact_store,
                        motet=motet,
                        logger=logger,
                    )
                    if not transcript_id:
                        logger.debug(
                            "prepare_context_skipping_video",
                            artifact_id=artifact_id,
                            filename=filename,
                            reason="no_derived_video_transcript_available",
                        )
                        self._inject_attachment_metadata(
                            content_parts=content_parts,
                            artifact_id=artifact_id,
                            content_type=content_type,
                            filename=filename,
                            bytes_value=att.get("bytes"),
                            content_status="pending_video_transcript",
                            pending_message=(
                                "The uploaded video is available by metadata, but a derived transcript "
                                "is not available yet. You can acknowledge the video exists, but do not "
                                "claim to know spoken content until a transcript is provided."
                            ),
                            logger=logger,
                        )
                        # ADR-0120 Phase 3 §1: only transcript-less videos get keyframes
                        # injected by default. Transcript-bearing videos rely on the model
                        # pulling frames via core.artifact_view when pixels are needed.
                        self._inject_video_keyframes(
                            content_parts=content_parts,
                            artifact_id=artifact_id,
                            artifact_store=artifact_store,
                            injected_image_artifact_ids=injected_image_artifact_ids,
                            max_keyframes=_VIDEO_MAX_KEYFRAMES,
                            logger=logger,
                        )
                        continue

                    if transcript_id in injected_text_artifact_ids:
                        logger.debug(
                            "prepare_context_skipping_duplicate_transcript",
                            artifact_id=artifact_id,
                            derived_transcript_id=transcript_id,
                            filename=filename,
                            reason="derived_transcript_already_in_context",
                        )
                        continue

                    if self._has_existing_attachment_text(
                        content_parts,
                        text_id=transcript_id,
                        filename=filename,
                    ):
                        logger.debug(
                            "prepare_context_skipping_duplicate_transcript_in_message",
                            artifact_id=artifact_id,
                            derived_transcript_id=transcript_id,
                            filename=filename,
                            reason="attachment_transcript_already_present",
                        )
                        injected_text_artifact_ids.add(transcript_id)
                        continue

                    self._inject_derived_text(
                        content_parts=content_parts,
                        artifact_id=artifact_id,
                        text_id=transcript_id,
                        content_type=content_type,
                        filename=filename,
                        injected_text_artifact_ids=injected_text_artifact_ids,
                        injected_text_hashes=injected_text_hashes,
                        artifact_store=artifact_store,
                        logger=logger,
                        attachment_role="video transcript",
                    )
                    continue

                if not is_current_message:
                    continue

                text_id = self._resolve_derived_text_id(
                    att=att,
                    artifact_id=artifact_id,
                    filename=filename,
                    content_type=content_type,
                    cache=derived_text_cache,
                    artifact_store=artifact_store,
                    motet=motet,
                    logger=logger,
                )
                if not text_id:
                    logger.debug(
                        "prepare_context_skipping_document",
                        artifact_id=artifact_id,
                        filename=filename,
                        reason="no_derived_text_available",
                    )
                    self._inject_attachment_metadata(
                        content_parts=content_parts,
                        artifact_id=artifact_id,
                        content_type=content_type,
                        filename=filename,
                        bytes_value=att.get("bytes"),
                        logger=logger,
                    )
                    continue

                if text_id in injected_text_artifact_ids:
                    logger.debug(
                        "prepare_context_skipping_duplicate_text",
                        artifact_id=artifact_id,
                        derived_text_id=text_id,
                        filename=filename,
                        reason="derived_text_already_in_context",
                    )
                    continue

                if self._has_existing_attachment_text(content_parts, text_id=text_id, filename=filename):
                    logger.debug(
                        "prepare_context_skipping_duplicate_text_in_message",
                        artifact_id=artifact_id,
                        derived_text_id=text_id,
                        filename=filename,
                        reason="attachment_text_already_present",
                    )
                    injected_text_artifact_ids.add(text_id)
                    continue

                self._inject_derived_text(
                    content_parts=content_parts,
                    artifact_id=artifact_id,
                    text_id=text_id,
                    content_type=content_type,
                    filename=filename,
                    injected_text_artifact_ids=injected_text_artifact_ids,
                    injected_text_hashes=injected_text_hashes,
                    artifact_store=artifact_store,
                    logger=logger,
                )

            msg.content_parts = content_parts  # type: ignore[attr-defined]

        state.timings["attachment_processing_s"] = round(time.perf_counter() - t0, 3)

    def _is_image_attachment(
        self,
        content_type: str,
        filename: str,
        *,
        artifact_id: str,
        logger: Any,
    ) -> tuple[bool, str]:
        is_image = content_type.startswith("image/")
        if is_image or not filename:
            return is_image, content_type

        image_content_types_by_extension = {
            ".avif": "image/avif",
            ".bmp": "image/bmp",
            ".gif": "image/gif",
            ".heic": "image/heic",
            ".heif": "image/heif",
            ".jpeg": "image/jpeg",
            ".jpg": "image/jpeg",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".webp": "image/webp",
        }
        lower_filename = filename.lower()
        matched_extension = next(
            (extension for extension in image_content_types_by_extension if lower_filename.endswith(extension)),
            None,
        )
        if matched_extension is None:
            return False, content_type

        if not content_type or content_type == "application/octet-stream":
            content_type = image_content_types_by_extension[matched_extension]
        logger.debug(
            "inferred_image_content_type",
            artifact_id=artifact_id,
            filename=filename,
            inferred_content_type=content_type,
        )
        return True, content_type

    def _is_video_attachment(
        self,
        content_type: str,
        filename: str,
        *,
        artifact_id: str,
        logger: Any,
    ) -> tuple[bool, str]:
        is_video = content_type.startswith("video/")
        if is_video or not filename:
            return is_video, content_type

        video_content_types_by_extension = {
            ".avi": "video/x-msvideo",
            ".m4v": "video/x-m4v",
            ".mkv": "video/x-matroska",
            ".mov": "video/quicktime",
            ".mp4": "video/mp4",
            ".mpeg": "video/mpeg",
            ".mpg": "video/mpeg",
            ".webm": "video/webm",
        }
        lower_filename = filename.lower()
        matched_extension = next(
            (extension for extension in video_content_types_by_extension if lower_filename.endswith(extension)),
            None,
        )
        if matched_extension is None:
            return False, content_type

        if not content_type or content_type == "application/octet-stream":
            content_type = video_content_types_by_extension[matched_extension]
        logger.debug(
            "inferred_video_content_type",
            artifact_id=artifact_id,
            filename=filename,
            inferred_content_type=content_type,
        )
        return True, content_type

    def _inject_video_keyframes(
        self,
        *,
        content_parts: List[Any],
        artifact_id: str,
        artifact_store: Any,
        injected_image_artifact_ids: set[str],
        max_keyframes: int,
        logger: Any,
    ) -> None:
        """Inject poster/keyframe images for transcript-less video attachments (ADR-0120 Phase 3)."""

        poster_meta = artifact_store.find_derived(
            source_artifact_id=artifact_id,
            kind=ArtifactKind.DERIVED_VIDEO_POSTER,
        )
        keyframe_metas = artifact_store.list(
            source_artifact_id=artifact_id,
            kind=ArtifactKind.DERIVED_VIDEO_KEYFRAME,
            limit=max(1, max_keyframes) + 1,
        )

        def _timestamp(meta: Any) -> int:
            md = getattr(meta, "metadata", None) or {}
            for key in ("timestamp_ms", "time_ms", "t_ms"):
                raw = md.get(key)
                if raw is None:
                    continue
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    continue
            return int(md.get("index") or 0)

        frames: List[Any] = []
        if poster_meta is not None:
            frames.append(poster_meta)
        ordered_keyframes = sorted(keyframe_metas, key=_timestamp)
        for keyframe in ordered_keyframes[:max_keyframes]:
            if keyframe.id not in {getattr(item, "id", None) for item in frames}:
                frames.append(keyframe)

        for frame_meta in frames:
            frame_id = str(getattr(frame_meta, "id", "") or "")
            if not frame_id or frame_id in injected_image_artifact_ids:
                continue
            content_parts.append(
                MediaPart(
                    media_type="image",
                    mime_type=getattr(frame_meta, "content_type", None) or "image/jpeg",
                    artifact_id=frame_id,
                    detail="auto",
                )
            )
            injected_image_artifact_ids.add(frame_id)

        if frames:
            logger.info(
                "prepare_context_injected_video_keyframes",
                artifact_id=artifact_id,
                frame_count=len(frames),
                reason="no_transcript_available",
            )

    def _resolve_image_artifact(
        self,
        *,
        artifact_id: str,
        content_type: str,
        message_content: str,
        cache: Dict[str, Optional[Any]],
        artifact_store: Any,
        motet: Any,
        logger: Any,
    ) -> tuple[str, str]:
        derivation_name = select_image_derivation(
            message=message_content,
            task_hints=None,
            default="base",
        )
        derivation_kind = get_derivation_kind(derivation_name)

        derived_artifact_id = artifact_id
        image_part_content_type = content_type
        if derivation_kind:
            cache_key = f"{artifact_id}:{derivation_name}"
            if cache_key not in cache:
                derived_meta = artifact_store.find_derived(
                    source_artifact_id=artifact_id,
                    kind=derivation_kind,
                )
                cache[cache_key] = derived_meta
            cached_derived_meta = cache[cache_key]
            if cached_derived_meta:
                derived_artifact_id = cached_derived_meta.id
                image_part_content_type = getattr(cached_derived_meta, "content_type", None) or image_part_content_type
                logger.info(
                    "using_derived_image",
                    **motet.log_fields(
                        source_artifact_id=artifact_id,
                        derivation_name=derivation_name,
                        derived_artifact_id=derived_artifact_id,
                    ),
                )
            else:
                logger.info(
                    "derived_image_not_found_using_original",
                    **motet.log_fields(
                        source_artifact_id=artifact_id,
                        derivation_name=derivation_name,
                    ),
                )

        return derived_artifact_id, image_part_content_type

    def _resolve_derived_text_id(
        self,
        *,
        att: Dict[str, Any],
        artifact_id: str,
        filename: str,
        content_type: str,
        cache: Dict[str, Optional[str]],
        artifact_store: Any,
        motet: Any,
        logger: Any,
    ) -> Optional[str]:
        derived_ids = att.get("derived_artifact_ids", {}) or {}
        text_id = derived_ids.get("derived_text") or derived_ids.get("extracted_text")

        if text_id:
            return str(text_id)

        if artifact_id in cache:
            return cache[artifact_id]

        logger.debug(
            "prepare_context_looking_up_derived_text",
            **motet.log_fields(
                artifact_id=artifact_id,
                filename=filename,
                content_type=content_type,
            ),
        )
        derived_meta = artifact_store.find_derived(
            source_artifact_id=artifact_id,
            kind=ArtifactKind.DERIVED_TEXT,
        )
        text_id = derived_meta.id if derived_meta else None
        cache[artifact_id] = text_id
        if text_id:
            logger.debug(
                "prepare_context_found_derived_text",
                artifact_id=artifact_id,
                derived_text_id=text_id,
            )
        else:
            logger.debug(
                "prepare_context_no_derived_text",
                artifact_id=artifact_id,
                filename=filename,
                content_type=content_type,
                reason="no_derived_text_artifact_found",
            )
        return text_id

    def _resolve_derived_video_transcript_id(
        self,
        *,
        att: Dict[str, Any],
        artifact_id: str,
        filename: str,
        content_type: str,
        cache: Dict[str, Optional[str]],
        artifact_store: Any,
        motet: Any,
        logger: Any,
    ) -> Optional[str]:
        derived_ids = att.get("derived_artifact_ids", {}) or {}
        transcript_id = (
            derived_ids.get("transcript")
            or derived_ids.get("derived_video_transcript")
            or derived_ids.get("video_transcript")
        )

        if transcript_id:
            return str(transcript_id)

        if artifact_id in cache:
            return cache[artifact_id]

        logger.debug(
            "prepare_context_looking_up_derived_video_transcript",
            **motet.log_fields(
                artifact_id=artifact_id,
                filename=filename,
                content_type=content_type,
            ),
        )
        derived_meta = artifact_store.find_derived(
            source_artifact_id=artifact_id,
            kind=ArtifactKind.DERIVED_VIDEO_TRANSCRIPT,
        )
        transcript_id = derived_meta.id if derived_meta else None
        cache[artifact_id] = transcript_id
        if transcript_id:
            logger.debug(
                "prepare_context_found_derived_video_transcript",
                artifact_id=artifact_id,
                derived_transcript_id=transcript_id,
            )
        else:
            logger.debug(
                "prepare_context_no_derived_video_transcript",
                artifact_id=artifact_id,
                filename=filename,
                content_type=content_type,
                reason="no_derived_video_transcript_artifact_found",
            )
        return transcript_id

    def _has_existing_attachment_text(
        self,
        content_parts: List[Any],
        *,
        text_id: str,
        filename: str,
    ) -> bool:
        for part in content_parts:
            text = getattr(part, "text", None)
            if not isinstance(text, str):
                continue
            if f"artifact_id='{text_id}'" in text or f"<attachment filename='{filename}'>" in text:
                return True
        return False

    def _inject_derived_text(
        self,
        *,
        content_parts: List[Any],
        artifact_id: str,
        text_id: str,
        content_type: str,
        filename: str,
        injected_text_artifact_ids: set[str],
        injected_text_hashes: set[str],
        artifact_store: Any,
        logger: Any,
        attachment_role: str = "extracted text artifact",
    ) -> None:
        try:
            text_content = artifact_store.get(text_id)
            if not text_content:
                logger.warning(
                    "prepare_context_derived_text_empty",
                    artifact_id=artifact_id,
                    derived_text_id=text_id,
                )
                return
            if isinstance(text_content, bytes):
                text_content = text_content.decode("utf-8", errors="ignore")

            max_chars = 50000
            original_length = len(text_content)
            if len(text_content) > max_chars:
                text_content = (
                    text_content[:max_chars]
                    + f"\n\n[Document truncated - showing first {max_chars} characters of {len(text_content)} total]"
                )
                logger.debug(
                    "prepare_context_truncated_document",
                    artifact_id=artifact_id,
                    original_length=original_length,
                    truncated_length=max_chars,
                )

            content_hash = hashlib.sha256(text_content.encode("utf-8", errors="ignore")).hexdigest()
            if content_hash in injected_text_hashes:
                logger.debug(
                    "prepare_context_skipping_duplicate_text_hash",
                    artifact_id=artifact_id,
                    derived_text_id=text_id,
                    filename=filename,
                    reason="content_hash_already_in_context",
                )
                injected_text_artifact_ids.add(text_id)
                return

            content_parts.append(
                TextPart(
                    text=(
                        f"<attachment artifact_id='{text_id}' source_artifact_id='{artifact_id}' "
                        f"source_content_type='{content_type}' content_hash='{content_hash}' "
                        f"filename='{filename}'>\n"
                        f"[Use source_artifact_id for tools that need the original binary file; "
                        f"artifact_id is the {attachment_role}.]\n"
                        f"{text_content}\n</attachment>"
                    )
                )
            )
            injected_text_artifact_ids.add(text_id)
            injected_text_hashes.add(content_hash)
            logger.debug(
                "prepare_context_injected_document_text",
                artifact_id=artifact_id,
                filename=filename,
                content_hash=content_hash,
                text_length=len(text_content),
            )
        except Exception as e:
            logger.warning(
                "attachment_text_injection_failed",
                error=str(e),
                artifact_id=artifact_id,
                derived_text_id=text_id,
                exc_info=True,
            )

    def _inject_attachment_metadata(
        self,
        *,
        content_parts: List[Any],
        artifact_id: str,
        content_type: str,
        filename: str,
        bytes_value: Any,
        logger: Any,
        content_status: str = "pending_text_extraction",
        pending_message: str = (
            "The uploaded artifact is available by metadata, but extracted text/RAG chunks are not available yet. "
            "You can acknowledge the artifact exists, but do not claim to have read its contents until text or chunks are provided."
        ),
    ) -> None:
        """Expose source artifact metadata when derived text/RAG chunks are not ready."""

        if self._has_existing_attachment_text(content_parts, text_id=artifact_id, filename=filename):
            return

        def _attr(value: Any) -> str:
            return str(value or "").replace("&", "&amp;").replace("'", "&#39;").replace("<", "&lt;").replace(">", "&gt;")

        content_parts.append(
            TextPart(
                text=(
                    f"<attachment artifact_id='{_attr(artifact_id)}' source_artifact_id='{_attr(artifact_id)}' "
                    f"source_content_type='{_attr(content_type)}' filename='{_attr(filename)}' "
                    f"bytes='{_attr(bytes_value)}' content_status='{_attr(content_status)}'>\n"
                    f"{pending_message}\n"
                    "</attachment>"
                )
            )
        )
        logger.info(
            "prepare_context_injected_attachment_metadata",
            artifact_id=artifact_id,
            filename=filename,
            content_type=content_type,
            content_status=content_status,
            reason="derived_content_unavailable",
        )
