"""
Motet - Agentic Loop Artifact View Sidecar Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-02

Description:
    Unit tests for ADR-0120 Phase 3 sidecar handling in the agentic loop:
    sidecar message construction from core.artifact_view results (correlation
    metadata, MediaParts), eviction of stale sidecar images with text
    breadcrumbs retained, attachment detection, and required-tool pinning.

Dependencies:
    - motet.core.reasoning.react.loop_skills helpers
    - motet.core.reasoning.react.loop_discovery helpers
    - motet.core.types Message/TextPart/MediaPart canonical parts

Usage:
    pytest tests/unit/core/reasoning/test_agentic_loop_artifact_view_sidecar.py
"""

from __future__ import annotations

from typing import Any, Dict

from motet.core.reasoning.react.loop_discovery import (
    ensure_tool_filter_required_tools,
)
from motet.core.reasoning.react.loop_skills import (
    _ARTIFACT_VIEW_SIDECAR_KIND,
    build_artifact_view_sidecar,
    conversation_has_attachments,
    evict_stale_artifact_view_sidecars,
)
from motet.core.types import MediaPart, TextPart


def _staged_result(frame_ids: list[str]) -> Dict[str, Any]:
    return {
        "status": "ok",
        "artifact_id": "video-1",
        "sidecar_required": True,
        "timestamps_ms": ["1000", "3000"],
        "staged_media": [
            {"artifact_id": frame_id, "mime_type": "image/jpeg", "timestamp_ms": 1000}
            for frame_id in frame_ids
        ],
    }


def test_build_sidecar_carries_media_parts_and_correlation_metadata() -> None:
    tool_call = {"tool_call_id": "call-7", "tool_name": "core.artifact_view"}

    sidecar = build_artifact_view_sidecar(tool_call, _staged_result(["kf-0", "kf-1"]), current_iteration=3)

    assert sidecar is not None
    assert sidecar.role == "user"
    assert sidecar.metadata["content_kind"] == _ARTIFACT_VIEW_SIDECAR_KIND
    assert sidecar.metadata["tool_call_id"] == "call-7"
    assert sidecar.metadata["artifact_id"] == "video-1"
    assert sidecar.metadata["iteration"] == 3
    media = [p for p in sidecar.content_parts if isinstance(p, MediaPart)]
    assert [p.artifact_id for p in media] == ["kf-0", "kf-1"]
    text = [p for p in sidecar.content_parts if isinstance(p, TextPart)]
    assert len(text) == 1
    assert "call-7" in text[0].text
    assert "not a new user message" in text[0].text


def test_build_sidecar_returns_none_without_sidecar_required_or_media() -> None:
    tool_call = {"tool_call_id": "call-1"}

    assert build_artifact_view_sidecar(tool_call, {"status": "ok"}, current_iteration=1) is None
    assert (
        build_artifact_view_sidecar(
            tool_call,
            {"status": "ok", "sidecar_required": True, "staged_media": []},
            current_iteration=1,
        )
        is None
    )
    # Staged entries without artifact IDs yield no MediaParts -> no sidecar.
    assert (
        build_artifact_view_sidecar(
            tool_call,
            {
                "status": "ok",
                "sidecar_required": True,
                "staged_media": [{"mime_type": "image/jpeg"}],
            },
            current_iteration=1,
        )
        is None
    )


def test_evict_stale_sidecars_drops_images_keeps_breadcrumb() -> None:
    tool_call = {"tool_call_id": "call-2"}
    sidecar = build_artifact_view_sidecar(tool_call, _staged_result(["kf-0"]), current_iteration=1)
    assert sidecar is not None
    history = [sidecar]

    # Within max age: untouched.
    evict_stale_artifact_view_sidecars(history, current_iteration=2)
    assert any(isinstance(p, MediaPart) for p in sidecar.content_parts)

    # Beyond max age: images evicted, text breadcrumb retained.
    evict_stale_artifact_view_sidecars(history, current_iteration=3)
    assert not any(isinstance(p, MediaPart) for p in sidecar.content_parts)
    assert any(isinstance(p, TextPart) for p in sidecar.content_parts)


def test_conversation_has_attachments_checks_latest_user_message() -> None:
    history_with = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi", "attachments": [{"artifact_id": "a1"}]},
        {"role": "assistant", "content": "hello"},
    ]
    history_without = [
        {"role": "user", "content": "hi", "attachments": [{"artifact_id": "a1"}]},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "thanks"},
    ]

    assert conversation_has_attachments(history_with) is True
    # Only the latest user message counts.
    assert conversation_has_attachments(history_without) is False
    assert conversation_has_attachments([]) is False


def test_ensure_tool_filter_required_tools_appends_without_duplicates() -> None:
    metadata = {"required_tools": ["core.search_artifacts"], "other": "x"}

    out = ensure_tool_filter_required_tools(metadata, ["core.artifact_read", "core.search_artifacts"])

    assert out["required_tools"] == ["core.search_artifacts", "core.artifact_read"]
    assert out["other"] == "x"
    # Original metadata is not mutated.
    assert metadata["required_tools"] == ["core.search_artifacts"]
