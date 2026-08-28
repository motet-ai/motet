"""
Motet - Artifact View Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Agent-facing tool that stages video keyframe images for vision models. Returns plain-text metadata plus staged_media artifact
    IDs; the agentic loop appends a synthetic user sidecar message carrying
    MediaParts before the next model call.

Dependencies:
    - pydantic for tool parameter schema validation
    - ToolRegistry for built-in tool registration
    - motet.artifact_store for tenant-scoped keyframe lookup

Usage:
    Tool call: core.artifact_view({"artifact_id": "video-1", "max_frames": 3})

Notes:
    - Does not embed images in tool-result messages; sidecar delivery is loop-side.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ...artifacts import ArtifactKind
from ..registry import ToolRegistry

_DEFAULT_MAX_FRAMES = 4
_MAX_FRAMES_CAP = 8


class ArtifactViewParams(BaseModel):
    """Parameters for staging video keyframe images."""

    artifact_id: str = Field(..., description="Source video artifact ID")
    timestamp_ms: Optional[int] = Field(
        default=None,
        ge=0,
        description="Optional timestamp in milliseconds; selects the nearest keyframe",
    )
    keyframe_index: Optional[int] = Field(
        default=None,
        ge=0,
        description="Optional zero-based keyframe index within the derived keyframe set",
    )
    max_frames: int = Field(
        default=_DEFAULT_MAX_FRAMES,
        ge=1,
        le=_MAX_FRAMES_CAP,
        description="Maximum keyframe images to stage (small cap)",
    )


def _get_motet_context_optional() -> Any:
    try:
        from motet.core.commands.decorator import get_motet_context

        return get_motet_context()
    except Exception:
        return None


def _timestamp_from_metadata(meta: Any) -> int:
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


def _select_keyframes(
    keyframes: List[Any],
    *,
    timestamp_ms: Optional[int],
    keyframe_index: Optional[int],
    max_frames: int,
) -> List[Any]:
    ordered = sorted(keyframes, key=_timestamp_from_metadata)
    if not ordered:
        return []

    if keyframe_index is not None:
        idx = min(max(0, keyframe_index), len(ordered) - 1)
        return [ordered[idx]]

    if timestamp_ms is not None:
        nearest = min(ordered, key=lambda meta: abs(_timestamp_from_metadata(meta) - timestamp_ms))
        return [nearest]

    return ordered[:max_frames]


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Stage video keyframe artifact IDs for sidecar multimodal delivery."""

    parsed = ArtifactViewParams(**(params or {}))
    motet = _get_motet_context_optional()
    if motet is None:
        return {
            "status": "error",
            "error": "Motet context is required to view artifact frames",
        }

    artifact_store = getattr(motet, "artifact_store", None)
    if artifact_store is None:
        return {
            "status": "error",
            "error": "Artifact store is unavailable",
        }

    source_meta = artifact_store.get_metadata(parsed.artifact_id)
    if source_meta is None:
        return {
            "status": "not_found",
            "artifact_id": parsed.artifact_id,
            "message": f"Artifact '{parsed.artifact_id}' was not found in the current tenant scope.",
        }

    poster_meta = artifact_store.find_derived(
        source_artifact_id=parsed.artifact_id,
        kind=ArtifactKind.DERIVED_VIDEO_POSTER,
    )
    keyframe_metas = artifact_store.list(
        source_artifact_id=parsed.artifact_id,
        kind=ArtifactKind.DERIVED_VIDEO_KEYFRAME,
        limit=64,
    )
    selected = _select_keyframes(
        keyframe_metas,
        timestamp_ms=parsed.timestamp_ms,
        keyframe_index=parsed.keyframe_index,
        max_frames=parsed.max_frames,
    )

    staged: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    if poster_meta is not None and parsed.timestamp_ms is None and parsed.keyframe_index is None:
        staged.append(
            {
                "artifact_id": poster_meta.id,
                "mime_type": getattr(poster_meta, "content_type", None) or "image/jpeg",
                "timestamp_ms": 0,
                "role": "poster",
            }
        )
        seen_ids.add(poster_meta.id)

    for meta in selected:
        frame_id = str(getattr(meta, "id", "") or "")
        if not frame_id or frame_id in seen_ids:
            continue
        seen_ids.add(frame_id)
        staged.append(
            {
                "artifact_id": frame_id,
                "mime_type": getattr(meta, "content_type", None) or "image/jpeg",
                "timestamp_ms": _timestamp_from_metadata(meta),
                "role": "keyframe",
            }
        )

    if not staged:
        return {
            "status": "not_ready",
            "artifact_id": parsed.artifact_id,
            "message": "No poster or keyframe images are available yet for this video artifact.",
        }

    timestamps = [str(item.get("timestamp_ms")) for item in staged]
    return {
        "status": "ok",
        "artifact_id": parsed.artifact_id,
        "frame_count": len(staged),
        "timestamps_ms": timestamps,
        "staged_media": staged,
        "sidecar_required": True,
        "message": (
            f"Staged {len(staged)} frame(s) from artifact {parsed.artifact_id} "
            f"at {', '.join(timestamps)}ms."
        ),
    }


def _format_observation(result: Dict[str, Any]) -> str:
    status = result.get("status", "unknown")
    count = int(result.get("frame_count") or 0)
    return f"artifact_view(status={status}, frames={count})"


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.artifact_view",
        description=(
            "Stage video keyframe images from a source video artifact for visual inspection. "
            "Use when prepared context lacks pixels (silent video, on-screen text, visual details). "
            "Pass source_artifact_id from attachment metadata; optional timestamp_ms or keyframe_index."
        ),
        func=run,
        tool_schema=ArtifactViewParams,
        category="artifacts",
        contextualize_observation=True,
        observation_formatter=_format_observation,
        default_timeout_seconds=20.0,
        suggested_max_calls=2,
        cost_class="medium",
        keywords=[
            "artifact",
            "video",
            "keyframe",
            "frame",
            "view",
            "visual",
            "image",
            "silent video",
            "artifact_id",
        ],
        required_capabilities=["tool_execution"],
    )


__all__ = ["ArtifactViewParams", "register", "run"]
