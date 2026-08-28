"""
Motet - Artifact View Tool Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-12

Description:
    Unit tests for the ADR-0120 core.artifact_view built-in tool: default
    poster + keyframe staging, timestamp/index selection, max_frames capping,
    and not_found / not_ready statuses.

Dependencies:
    - types.SimpleNamespace for lightweight artifact store stubs
    - motet.core.tools.builtin.artifact_view for tool behavior

Usage:
    pytest tests/unit/core/tools/test_artifact_view.py
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List, Optional

from motet.core.artifacts import ArtifactKind
from motet.core.tools.builtin import artifact_view as artifact_view_module


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
    def __init__(self, *, with_poster: bool = True, keyframe_timestamps: Optional[List[int]] = None) -> None:
        self._meta: dict[str, _ArtifactMeta] = {
            "video-1": _ArtifactMeta(
                artifact_id="video-1",
                kind=ArtifactKind.USER_UPLOAD.value,
                content_type="video/quicktime",
            )
        }
        if with_poster:
            self._meta["poster-1"] = _ArtifactMeta(
                artifact_id="poster-1",
                kind=ArtifactKind.DERIVED_VIDEO_POSTER.value,
                source_artifact_id="video-1",
            )
        for index, t_ms in enumerate(keyframe_timestamps or []):
            frame_id = f"kf-{index}"
            self._meta[frame_id] = _ArtifactMeta(
                artifact_id=frame_id,
                kind=ArtifactKind.DERIVED_VIDEO_KEYFRAME.value,
                source_artifact_id="video-1",
                metadata={"timestamp_ms": t_ms, "index": index},
            )

    def get_metadata(self, artifact_id: str) -> Optional[_ArtifactMeta]:
        return self._meta.get(artifact_id)

    def find_derived(self, *, source_artifact_id: str, kind: Any) -> Optional[_ArtifactMeta]:
        for meta in self._meta.values():
            if meta.source_artifact_id == source_artifact_id and meta.kind == kind.value:
                return meta
        return None

    def list(self, *, source_artifact_id: str, kind: Any, limit: int = 64) -> List[_ArtifactMeta]:
        matches = [
            meta
            for meta in self._meta.values()
            if meta.source_artifact_id == source_artifact_id and meta.kind == kind.value
        ]
        return matches[:limit]


def _patch_context(monkeypatch: Any, store: _ArtifactStoreStub) -> None:
    motet = SimpleNamespace(artifact_store=store)
    monkeypatch.setattr(artifact_view_module, "_get_motet_context_optional", lambda: motet)


def test_artifact_view_stages_poster_and_keyframes_by_default(monkeypatch: Any) -> None:
    store = _ArtifactStoreStub(keyframe_timestamps=[1000, 3000, 5000])
    _patch_context(monkeypatch, store)

    result = artifact_view_module.run({"artifact_id": "video-1"})

    assert result["status"] == "ok"
    assert result["sidecar_required"] is True
    roles = [item["role"] for item in result["staged_media"]]
    assert roles[0] == "poster"
    assert roles.count("keyframe") == 3
    timestamps = [item["timestamp_ms"] for item in result["staged_media"] if item["role"] == "keyframe"]
    assert timestamps == [1000, 3000, 5000]


def test_artifact_view_max_frames_caps_keyframes(monkeypatch: Any) -> None:
    store = _ArtifactStoreStub(keyframe_timestamps=[0, 1000, 2000, 3000, 4000])
    _patch_context(monkeypatch, store)

    result = artifact_view_module.run({"artifact_id": "video-1", "max_frames": 2})

    keyframes = [item for item in result["staged_media"] if item["role"] == "keyframe"]
    assert len(keyframes) == 2
    assert [item["timestamp_ms"] for item in keyframes] == [0, 1000]


def test_artifact_view_timestamp_selects_nearest_frame_without_poster(monkeypatch: Any) -> None:
    store = _ArtifactStoreStub(keyframe_timestamps=[1000, 3000, 5000])
    _patch_context(monkeypatch, store)

    result = artifact_view_module.run({"artifact_id": "video-1", "timestamp_ms": 2800})

    assert result["status"] == "ok"
    assert len(result["staged_media"]) == 1
    frame = result["staged_media"][0]
    assert frame["role"] == "keyframe"
    assert frame["timestamp_ms"] == 3000


def test_artifact_view_keyframe_index_clamps_to_range(monkeypatch: Any) -> None:
    store = _ArtifactStoreStub(keyframe_timestamps=[1000, 3000])
    _patch_context(monkeypatch, store)

    result = artifact_view_module.run({"artifact_id": "video-1", "keyframe_index": 7})

    assert result["status"] == "ok"
    assert len(result["staged_media"]) == 1
    assert result["staged_media"][0]["timestamp_ms"] == 3000


def test_artifact_view_not_found_for_unknown_artifact(monkeypatch: Any) -> None:
    store = _ArtifactStoreStub()
    _patch_context(monkeypatch, store)

    result = artifact_view_module.run({"artifact_id": "missing-video"})

    assert result["status"] == "not_found"


def test_artifact_view_not_ready_when_no_frames_derived(monkeypatch: Any) -> None:
    store = _ArtifactStoreStub(with_poster=False, keyframe_timestamps=[])
    _patch_context(monkeypatch, store)

    result = artifact_view_module.run({"artifact_id": "video-1"})

    assert result["status"] == "not_ready"
