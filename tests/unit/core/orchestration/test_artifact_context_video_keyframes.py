"""
Motet - Artifact Context Video Keyframe Gating Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-12

Description:
    Unit tests for ADR-0120 Phase 3 keyframe injection gating in
    ArtifactContextProvider: poster/keyframe MediaParts are injected only for
    transcript-less this-turn video attachments; transcript-bearing videos get
    transcript text and no images.

Dependencies:
    - types.SimpleNamespace for message, motet, and artifact store stubs
    - motet.core.orchestration.context.artifact_context for provider behavior

Usage:
    pytest tests/unit/core/orchestration/test_artifact_context_video_keyframes.py
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List, Optional

from motet.core.artifacts import ArtifactKind
from motet.core.orchestration.context import artifact_context as artifact_context_module
from motet.core.orchestration.context.artifact_context import ArtifactContextProvider
from motet.core.orchestration.context.types import ContextPipelineState
from motet.core.types import MediaPart


class _Logger:
    def info(self, *args: Any, **kwargs: Any) -> None:
        pass

    def debug(self, *args: Any, **kwargs: Any) -> None:
        pass

    def warning(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"unexpected warning: {args} {kwargs}")


class _ArtifactMeta:
    def __init__(
        self,
        *,
        artifact_id: str,
        kind: str,
        source_artifact_id: Optional[str] = None,
        content_type: str = "image/jpeg",
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        self.id = artifact_id
        self.kind = kind
        self.source_artifact_id = source_artifact_id
        self.content_type = content_type
        self.metadata = metadata or {}


class _ArtifactStoreStub:
    def __init__(self, *, keyframe_timestamps: List[int]) -> None:
        self._meta: dict[str, _ArtifactMeta] = {
            "poster-1": _ArtifactMeta(
                artifact_id="poster-1",
                kind=ArtifactKind.DERIVED_VIDEO_POSTER.value,
                source_artifact_id="video-1",
            )
        }
        for index, t_ms in enumerate(keyframe_timestamps):
            frame_id = f"kf-{index}"
            self._meta[frame_id] = _ArtifactMeta(
                artifact_id=frame_id,
                kind=ArtifactKind.DERIVED_VIDEO_KEYFRAME.value,
                source_artifact_id="video-1",
                metadata={"timestamp_ms": t_ms},
            )

    def find_derived(self, *, source_artifact_id: str, kind: Any) -> Optional[_ArtifactMeta]:
        for meta in self._meta.values():
            if meta.source_artifact_id == source_artifact_id and meta.kind == kind.value:
                return meta
        return None

    def list(self, **kwargs: Any) -> List[_ArtifactMeta]:
        source_artifact_id = kwargs.get("source_artifact_id")
        kind = kwargs.get("kind")
        limit = int(kwargs.get("limit") or 64)
        matches = [
            meta
            for meta in self._meta.values()
            if meta.source_artifact_id == source_artifact_id
            and kind is not None
            and meta.kind == kind.value
        ]
        return matches[:limit]


def _run_provider(
    monkeypatch: Any,
    *,
    store: _ArtifactStoreStub,
    transcript_id: Optional[str],
) -> Any:
    provider = ArtifactContextProvider()
    monkeypatch.setattr(artifact_context_module, "get_artifact_store", lambda: store)
    monkeypatch.setattr(
        provider,
        "_resolve_derived_video_transcript_id",
        lambda **kwargs: transcript_id,
    )
    injected_text: List[str] = []

    def _fake_inject_derived_text(*, content_parts: List[Any], text_id: str, **kwargs: Any) -> None:
        injected_text.append(text_id)

    monkeypatch.setattr(provider, "_inject_derived_text", _fake_inject_derived_text)

    message = SimpleNamespace(
        role="user",
        content="what does this say?",
        attachments=[
            {
                "artifact_id": "video-1",
                "filename": "IMG_2178.MOV",
                "content_type": "video/quicktime",
                "bytes": 1000,
            }
        ],
        content_parts=None,
    )
    state = ContextPipelineState(messages=[message])
    motet = SimpleNamespace(artifact_store=store, conversation_id=None)

    provider.apply(state, data=SimpleNamespace(), motet=motet, logger=_Logger())
    message.injected_text_ids = injected_text  # type: ignore[attr-defined]
    return message


def test_video_with_transcript_gets_text_and_no_keyframe_images(monkeypatch: Any) -> None:
    store = _ArtifactStoreStub(keyframe_timestamps=[1000, 3000, 5000])

    message = _run_provider(monkeypatch, store=store, transcript_id="transcript-1")

    assert message.injected_text_ids == ["transcript-1"]
    media_parts = [p for p in message.content_parts if isinstance(p, MediaPart)]
    assert media_parts == []


def test_video_without_transcript_gets_poster_and_capped_keyframes(monkeypatch: Any) -> None:
    store = _ArtifactStoreStub(keyframe_timestamps=[0, 1000, 2000, 3000, 4000, 5000])

    message = _run_provider(monkeypatch, store=store, transcript_id=None)

    assert message.injected_text_ids == []
    media_parts = [p for p in message.content_parts if isinstance(p, MediaPart)]
    # Poster plus at most 4 keyframes (_VIDEO_MAX_KEYFRAMES).
    assert len(media_parts) == 5
    assert media_parts[0].artifact_id == "poster-1"
    keyframe_ids = [p.artifact_id for p in media_parts[1:]]
    assert keyframe_ids == ["kf-0", "kf-1", "kf-2", "kf-3"]
