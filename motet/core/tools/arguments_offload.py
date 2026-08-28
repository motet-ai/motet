"""
Motet - Tool Arguments Offload Helpers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Helpers for capping ToolInvocation.arguments_json in conversational memory while
    preserving full, unmodified argument JSON for provider tool-call replay.

    Oversized arguments are stored as ArtifactKind.TOOL_ARGUMENTS artifacts. Memory /
    transcript rows keep a small valid-JSON preview plus ``arguments_artifact_id``.
    On transcript replay, hydrate the full string before building Message.tool_calls
    so providers (esp. xAI Responses) receive unmodified arguments.

Dependencies:
    - hashlib / json: hash and preview serialization
    - motet.core.types: ToolCallRequest / ToolCallResult / TranscriptItem

Usage:
    from motet.core.tools.arguments_offload import plan_arguments_storage, hydrate_transcript_tool_arguments

    inline, digest, needs_artifact, truncated = plan_arguments_storage(full_json, 8192)
    items = hydrate_transcript_tool_arguments(items, fetch_arguments=store.get)

Notes:
    - Never slice JSON mid-string for provider replay; that yields invalid JSON and
      triggers xAI ``Invalid tool arguments... EOF while parsing a string``.
    - Fail-closed: if an offloaded artifact is missing, omit the tool-call/result pair.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, List, Optional, Tuple

import structlog

from ..types import ToolCallRequest, ToolCallResult, TranscriptItem

logger = structlog.get_logger(__name__)

ARGUMENTS_OFFLOADED_MARKER = "_motet_arguments_offloaded"
_LEGACY_TRUNCATE_MARKER = "...[truncated]"


def hash_arguments_json(arguments_json: str) -> str:
    """SHA-256 hex digest of the full (unmodified) arguments JSON string."""
    return hashlib.sha256(arguments_json.encode("utf-8")).hexdigest()


def build_arguments_preview(*, full_bytes: int, arguments_hash: str) -> str:
    """
    Small valid JSON preview stored inline when full args are offloaded.

    Must remain valid JSON — never append a free-text truncation marker.
    """
    return json.dumps(
        {
            ARGUMENTS_OFFLOADED_MARKER: True,
            "bytes": int(full_bytes),
            "arguments_hash": arguments_hash,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )


def plan_arguments_storage(
    full_arguments_json: str,
    max_args_bytes: int,
) -> Tuple[str, str, bool, bool]:
    """
    Decide inline vs artifact storage for tool arguments.

    Returns:
        (inline_arguments_json, arguments_hash, needs_artifact, arguments_truncated)

        - ``arguments_hash`` is always of the *full* unmodified JSON.
        - When ``needs_artifact`` is True, caller must store ``full_arguments_json``
          and set ``arguments_artifact_id``; ``inline_arguments_json`` is a preview.
    """
    full = full_arguments_json if isinstance(full_arguments_json, str) else "{}"
    digest = hash_arguments_json(full)
    raw_bytes = len(full.encode("utf-8"))
    cap = max(1, int(max_args_bytes or 8192))
    if raw_bytes <= cap:
        return full, digest, False, False
    preview = build_arguments_preview(full_bytes=raw_bytes, arguments_hash=digest)
    return preview, digest, True, True


def _decode_artifact_payload(payload: Any) -> Optional[str]:
    if payload is None:
        return None
    if isinstance(payload, str):
        return payload
    if isinstance(payload, (bytes, bytearray)):
        try:
            return bytes(payload).decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("tool_arguments_artifact_decode_failed")
            return None
    return None


def arguments_unsafe_for_provider_replay(
    arguments_json: str,
    *,
    arguments_artifact_id: Optional[str] = None,
) -> bool:
    """
    True when inline arguments must not be sent to a provider without hydration.

    Covers legacy mid-string truncation and offload previews without an artifact.
    """
    if arguments_artifact_id:
        return False
    text = arguments_json if isinstance(arguments_json, str) else ""
    if _LEGACY_TRUNCATE_MARKER in text:
        return True
    try:
        parsed = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return True
    if isinstance(parsed, dict) and parsed.get(ARGUMENTS_OFFLOADED_MARKER):
        return True
    return False


def hydrate_transcript_tool_arguments(
    items: List[TranscriptItem],
    *,
    fetch_arguments: Callable[[str], Optional[Any]],
) -> List[TranscriptItem]:
    """
    Hydrate offloaded tool-call arguments for provider replay.

    - When ``arguments_artifact_id`` is set, replace ``arguments_json`` with the
      full unmodified artifact payload.
    - When hydration fails, or legacy truncated/invalid args are present without
      an artifact, omit the ToolCallRequest and its matching ToolCallResult
      (schema-correct omit; never replay broken JSON).
    """
    if not items:
        return []

    omit_call_ids: set[str] = set()
    hydrated: List[TranscriptItem] = []

    for it in items:
        if not isinstance(it, ToolCallRequest):
            hydrated.append(it)
            continue

        artifact_id = getattr(it, "arguments_artifact_id", None)
        if artifact_id:
            try:
                payload = fetch_arguments(str(artifact_id))
            except Exception as e:
                logger.warning(
                    "tool_arguments_artifact_fetch_failed",
                    artifact_id=artifact_id,
                    call_id=it.call_id,
                    error=str(e),
                    exc_info=True,
                )
                payload = None
            full = _decode_artifact_payload(payload)
            if full is None or not str(full).strip():
                logger.warning(
                    "tool_arguments_artifact_missing_omitting_call",
                    artifact_id=artifact_id,
                    call_id=it.call_id,
                    tool_name=it.tool_name,
                )
                omit_call_ids.add(it.call_id)
                continue
            try:
                json.loads(full)
            except json.JSONDecodeError:
                logger.warning(
                    "tool_arguments_artifact_invalid_json_omitting_call",
                    artifact_id=artifact_id,
                    call_id=it.call_id,
                    tool_name=it.tool_name,
                )
                omit_call_ids.add(it.call_id)
                continue
            hydrated.append(it.model_copy(update={"arguments_json": full}))
            continue

        if arguments_unsafe_for_provider_replay(it.arguments_json, arguments_artifact_id=None):
            logger.warning(
                "tool_arguments_unsafe_omitting_call",
                call_id=it.call_id,
                tool_name=it.tool_name,
                arguments_preview=(it.arguments_json or "")[:120],
            )
            omit_call_ids.add(it.call_id)
            continue

        hydrated.append(it)

    if not omit_call_ids:
        return hydrated

    out: List[TranscriptItem] = []
    for it in hydrated:
        if isinstance(it, ToolCallRequest) and it.call_id in omit_call_ids:
            continue
        if isinstance(it, ToolCallResult) and it.call_id in omit_call_ids:
            continue
        out.append(it)
    return out


__all__ = [
    "ARGUMENTS_OFFLOADED_MARKER",
    "arguments_unsafe_for_provider_replay",
    "build_arguments_preview",
    "hash_arguments_json",
    "hydrate_transcript_tool_arguments",
    "plan_arguments_storage",
]
