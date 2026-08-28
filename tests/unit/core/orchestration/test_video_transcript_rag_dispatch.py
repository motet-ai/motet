"""
Motet - Video Transcript RAG Dispatch Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-11

Description:
    Unit tests ensuring video transcript create/reuse always dispatches
    source-level artifact RAG re-index, and visuals defer indexing when
    transcription owns the final index.

Dependencies:
    - unittest.mock for derivation command patching
    - motet.core.commands.builtin.derivation

Usage:
    pytest tests/unit/core/orchestration/test_video_transcript_rag_dispatch.py
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from motet.core.commands.command_data_classes import (
    DeriveVideoTranscriptData,
    DeriveVideoVisualsData,
)
from motet.core.commands.builtin.derivation import (
    _video_transcription_will_own_rag_index,
    derive_video_transcript,
    derive_video_visuals,
)


class _MotetStub:
    task_id = "task-1"
    conversation_id = "conv-1"
    tenant_id = "tenant-1"
    principal_id = "principal-1"
    motet_id = "motet-1"
    command_id = "cmd-1"

    def __init__(
        self,
        *,
        rag_enabled: bool = True,
        transcription_enabled: bool = True,
        transcription_backend: str = "openai_api",
    ) -> None:
        self.stack = SimpleNamespace(
            config=SimpleNamespace(
                artifact_rag_enabled=rag_enabled,
                artifact_rag_index_on_derivation=True,
                video_transcription_enabled=transcription_enabled,
                video_transcription_backend=transcription_backend,
                artifact_rag_chunk_size=3200,
                artifact_rag_chunk_overlap=400,
                artifact_prep_json_max_depth=3,
                artifact_rag_native_text_mode="auto",
            )
        )
        self.dispatched: list[Any] = []
        self.artifact_store = SimpleNamespace(
            get_metadata=lambda _id: SimpleNamespace(
                id=_id,
                content_type="video/mp4",
                kind=SimpleNamespace(value="user_upload"),
                metadata={"filename": "clip.mp4"},
                bytes=100,
            ),
            get=lambda _id: b"video-bytes",
        )

    def dispatch(self, commands: list[Any]) -> list[str]:
        self.dispatched.extend(commands)
        return ["task-child"]

    def log_fields(self, **extra: Any) -> dict[str, Any]:
        return extra


def test_video_transcription_will_own_rag_index_when_backend_enabled() -> None:
    motet = _MotetStub(transcription_backend="openai_api")
    assert _video_transcription_will_own_rag_index(motet) is True


def test_video_transcription_will_not_own_rag_index_when_backend_disabled() -> None:
    motet = _MotetStub(transcription_backend="none")
    assert _video_transcription_will_own_rag_index(motet) is False


@patch("motet.core.commands.builtin.derivation.get_motet_context")
@patch("motet.core.commands.builtin.derivation.derive_video_transcript_artifact")
def test_derive_video_transcript_reused_dispatches_source_reindex(
    mock_derive: Any,
    mock_get_motet: Any,
) -> None:
    motet = _MotetStub()
    mock_get_motet.return_value = motet
    mock_derive.return_value = {
        "status": "success",
        "reused": True,
        "derivations": {"transcript": {"id": "transcript-1"}},
    }

    with patch(
        "motet.core.commands.builtin.derivation._dispatch_artifact_rag_source_index"
    ) as mock_source_index:
        derive_video_transcript.__wrapped__(DeriveVideoTranscriptData(source_artifact_id="video-1"))

    mock_source_index.assert_called_once()
    assert mock_source_index.call_args.kwargs["source_artifact_id"] == "video-1"
    assert mock_source_index.call_args.kwargs["reason"] == "video_transcript_reused"


@patch("motet.core.commands.builtin.derivation.get_motet_context")
@patch("motet.core.commands.builtin.derivation.derive_video_visual_artifacts")
def test_derive_video_visuals_skips_rag_when_transcription_owns_index(
    mock_derive: Any,
    mock_get_motet: Any,
) -> None:
    motet = _MotetStub(transcription_backend="openai_api")
    mock_get_motet.return_value = motet
    mock_derive.return_value = {
        "status": "success",
        "reused": True,
        "derivations": {"poster": {"id": "poster-1"}, "keyframes": [{"id": "kf-1"}]},
    }

    with patch(
        "motet.core.commands.builtin.derivation._dispatch_artifact_rag_source_index"
    ) as mock_source_index:
        derive_video_visuals.__wrapped__(
            DeriveVideoVisualsData(source_artifact_id="video-1", keyframe_strategy="scene")
        )

    mock_source_index.assert_not_called()


@patch("motet.core.commands.builtin.derivation.get_motet_context")
@patch("motet.core.commands.builtin.derivation.derive_video_visual_artifacts")
def test_derive_video_visuals_indexes_when_transcription_disabled(
    mock_derive: Any,
    mock_get_motet: Any,
) -> None:
    motet = _MotetStub(transcription_backend="none")
    mock_get_motet.return_value = motet
    mock_derive.return_value = {
        "status": "success",
        "derivations": {"poster": {"id": "poster-1"}, "keyframes": [{"id": "kf-1"}]},
    }

    with patch(
        "motet.core.commands.builtin.derivation._dispatch_artifact_rag_source_index"
    ) as mock_source_index:
        derive_video_visuals.__wrapped__(
            DeriveVideoVisualsData(source_artifact_id="video-1", keyframe_strategy="scene")
        )

    mock_source_index.assert_called_once()
    assert mock_source_index.call_args.kwargs["reason"] == "video_visuals_derived"
