"""
Motet - Artifact Read Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Agent-facing tool that returns derived text for an artifact by ID. Resolves DERIVED_TEXT and DERIVED_VIDEO_TRANSCRIPT from source
    artifacts and supports windowed reads for long texts. When derivation was
    skipped (oversized tool-result offloads), falls back to the raw text-readable
    source payload so observation clipping can still recover the full result.

Dependencies:
    - pydantic for tool parameter schema validation
    - ToolRegistry for built-in tool registration
    - motet.artifact_store for tenant-scoped artifact reads

Usage:
    Tool call: core.artifact_read({"artifact_id": "source-1"})

Notes:
    - Prefers derived text when present; falls back to raw tool_artifact / text
      payloads when derivation was never started (oversized offload path).
    - Returns a structured not-ready result when derivation is still async for
      non-text sources (PDF, video, images).
    - Output attribution mirrors inline <attachment> parts from prepare_context.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from ...artifacts import ArtifactKind
from ..registry import ToolRegistry

_DEFAULT_MAX_CHARS = 50_000

_TEXT_READABLE_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "application/ld+json",
        "application/xml",
        "application/javascript",
        "application/x-ndjson",
        "application/x-yaml",
        "application/yaml",
    }
)


class ArtifactReadParams(BaseModel):
    """Parameters for reading derived artifact text."""

    artifact_id: str = Field(..., description="Source or derived artifact ID to read")
    offset_chars: int = Field(
        default=0,
        ge=0,
        description="Character offset into the derived text for windowed reads",
    )
    max_chars: Optional[int] = Field(
        default=None,
        ge=1,
        le=120_000,
        description="Maximum characters to return. Defaults to 50,000.",
    )


def _get_motet_context_optional() -> Any:
    try:
        from motet.core.commands.decorator import get_motet_context

        return get_motet_context()
    except Exception:
        return None


def _resolve_derived_text_id(artifact_store: Any, artifact_id: str) -> tuple[Optional[str], Optional[str]]:
    """Resolve (source_artifact_id, derived_text_id) for a source or derived ID."""

    meta = artifact_store.get_metadata(artifact_id)
    if meta is None:
        return None, None

    kind = str(getattr(meta, "kind", "") or "")
    if kind in {
        ArtifactKind.DERIVED_TEXT.value,
        ArtifactKind.DERIVED_VIDEO_TRANSCRIPT.value,
    }:
        source_id = str(getattr(meta, "source_artifact_id", None) or artifact_id)
        return source_id, artifact_id

    source_id = artifact_id
    for derived_kind in (ArtifactKind.DERIVED_TEXT, ArtifactKind.DERIVED_VIDEO_TRANSCRIPT):
        derived_meta = artifact_store.find_derived(source_artifact_id=source_id, kind=derived_kind)
        if derived_meta is not None:
            return source_id, derived_meta.id
    return source_id, None


def _is_text_readable_source(meta: Any) -> bool:
    """True when the source payload itself can be returned as text without derivation."""
    if meta is None:
        return False
    kind = str(getattr(meta, "kind", "") or "")
    if kind == ArtifactKind.TOOL_ARTIFACT.value:
        return True
    content_type = str(getattr(meta, "content_type", "") or "").lower().split(";", 1)[0].strip()
    if content_type.startswith("text/"):
        return True
    return content_type in _TEXT_READABLE_CONTENT_TYPES


def _payload_to_text(payload: Any) -> str:
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="ignore")
    return str(payload)


def _attachment_role_for_kind(kind: str, *, raw_fallback: bool = False) -> str:
    if raw_fallback:
        if kind == ArtifactKind.TOOL_ARTIFACT.value:
            return "raw tool result payload"
        return "raw artifact payload"
    if kind == ArtifactKind.DERIVED_VIDEO_TRANSCRIPT.value:
        return "video transcript"
    return "extracted text artifact"


def _windowed_ok_result(
    *,
    parsed: ArtifactReadParams,
    source_artifact_id: str,
    read_artifact_id: str,
    text: str,
    content_type: str,
    filename: str,
    role_label: str,
    derived_artifact_id: Optional[str],
    raw_fallback: bool,
) -> Dict[str, Any]:
    total_length = len(text)
    offset = min(parsed.offset_chars, total_length)
    max_chars = parsed.max_chars or _DEFAULT_MAX_CHARS
    window = text[offset : offset + max_chars]
    result: Dict[str, Any] = {
        "status": "ok",
        "artifact_id": parsed.artifact_id,
        "source_artifact_id": source_artifact_id,
        "derived_artifact_id": derived_artifact_id,
        "filename": filename,
        "content_type": content_type,
        "offset_chars": offset,
        "returned_chars": len(window),
        "total_chars": total_length,
        "truncated": offset + len(window) < total_length,
        "text": window,
        "context_text": (
            f"<attachment artifact_id='{read_artifact_id}' source_artifact_id='{source_artifact_id}' "
            f"source_content_type='{content_type}' filename='{filename}'>\n"
            f"[Use source_artifact_id for tools that need the original binary file; "
            f"artifact_id is the {role_label}.]\n"
            f"{window}\n</attachment>"
        ),
    }
    if raw_fallback:
        result["read_source"] = "raw_payload"
    return result


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Read derived text for an artifact with optional windowing."""

    parsed = ArtifactReadParams(**(params or {}))
    motet = _get_motet_context_optional()
    if motet is None:
        return {
            "status": "error",
            "error": "Motet context is required to read artifacts",
        }

    artifact_store = getattr(motet, "artifact_store", None)
    if artifact_store is None:
        return {
            "status": "error",
            "error": "Artifact store is unavailable",
        }

    source_artifact_id, derived_artifact_id = _resolve_derived_text_id(artifact_store, parsed.artifact_id)
    if not source_artifact_id:
        return {
            "status": "not_found",
            "artifact_id": parsed.artifact_id,
            "message": f"Artifact '{parsed.artifact_id}' was not found in the current tenant scope.",
        }

    source_meta = artifact_store.get_metadata(source_artifact_id)

    if not derived_artifact_id:
        # Oversized tool-result offloads store the full JSON and skip text
        # derivation by design; observation clipping still points agents here.
        if _is_text_readable_source(source_meta):
            payload = artifact_store.get(source_artifact_id)
            if payload not in (None, "", b""):
                content_type = str(
                    getattr(source_meta, "content_type", None) or "application/octet-stream"
                )
                filename = str(
                    (getattr(source_meta, "metadata", None) or {}).get("filename")
                    or source_artifact_id
                )
                kind = str(getattr(source_meta, "kind", "") or "")
                return _windowed_ok_result(
                    parsed=parsed,
                    source_artifact_id=source_artifact_id,
                    read_artifact_id=source_artifact_id,
                    text=_payload_to_text(payload),
                    content_type=content_type,
                    filename=filename,
                    role_label=_attachment_role_for_kind(kind, raw_fallback=True),
                    derived_artifact_id=None,
                    raw_fallback=True,
                )
        return {
            "status": "not_ready",
            "artifact_id": parsed.artifact_id,
            "source_artifact_id": source_artifact_id,
            "message": (
                "Derived text is not available yet for this artifact. "
                "Derivation may still be running; try again shortly."
            ),
        }

    payload = artifact_store.get(derived_artifact_id)
    if payload in (None, "", b""):
        return {
            "status": "not_ready",
            "artifact_id": parsed.artifact_id,
            "source_artifact_id": source_artifact_id,
            "derived_artifact_id": derived_artifact_id,
            "message": "Derived text exists but is empty or not yet materialized.",
        }

    derived_meta = artifact_store.get_metadata(derived_artifact_id)
    content_type = str(getattr(source_meta, "content_type", None) or "application/octet-stream")
    filename = str(
        (getattr(source_meta, "metadata", None) or {}).get("filename")
        or (getattr(derived_meta, "metadata", None) or {}).get("filename")
        or source_artifact_id
    )
    derived_kind = str(getattr(derived_meta, "kind", "") or "")
    return _windowed_ok_result(
        parsed=parsed,
        source_artifact_id=source_artifact_id,
        read_artifact_id=derived_artifact_id,
        text=_payload_to_text(payload),
        content_type=content_type,
        filename=filename,
        role_label=_attachment_role_for_kind(derived_kind),
        derived_artifact_id=derived_artifact_id,
        raw_fallback=False,
    )


def _format_observation(result: Dict[str, Any]) -> str:
    status = result.get("status", "unknown")
    text = str(result.get("text") or "").strip()
    if status == "ok" and text:
        return text
    returned = int(result.get("returned_chars") or 0)
    total = int(result.get("total_chars") or 0)
    message = str(result.get("message") or result.get("error") or "").strip()
    line = f"artifact_read(status={status}, returned_chars={returned}, total_chars={total})"
    if message:
        line = f"{line} {message}"
    return line


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.artifact_read",
        description=(
            "Read the full text of an artifact by artifact_id when prepared context is incomplete. "
            "Prefers derived text; for oversized tool results (no derivation), returns the raw "
            "tool_artifact payload. Pass source_artifact_id from attachment metadata. Supports "
            "offset_chars and max_chars for long documents or transcripts."
        ),
        func=run,
        tool_schema=ArtifactReadParams,
        category="artifacts",
        contextualize_observation=False,
        observation_formatter=_format_observation,
        default_timeout_seconds=20.0,
        suggested_max_calls=3,
        cost_class="low",
        keywords=[
            "artifact",
            "attachment",
            "read artifact",
            "full text",
            "transcript",
            "document",
            "artifact_id",
            "source_artifact_id",
        ],
        required_capabilities=["tool_execution"],
    )


__all__ = ["ArtifactReadParams", "register", "run"]
