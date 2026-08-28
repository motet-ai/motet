"""
Motet - Agent Turn Complete Helpers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Complete-phase helpers for `agent_turn` (GitHub issue #147 factorization).
    Owns response / usage / cost extraction, model-identity resolution,
    generated-media collection, and the post-reasoning terminal stream +
    return dict path. Extracted from turn.py with no behavior change.
    Lives at turn/complete.py. Budget-stop fallback text uses the shared
    issue #188 contract in ``budget_continue``.

Dependencies:
    - structlog: Structured logging for media validation
    - MotetContext stream_event surface (via caller-provided motet)

Usage:
    from motet.core.orchestration.turn.complete import (
        complete_agent_turn,
        extract_response_text,
        extract_turn_usage,
        _collect_generated_media,
    )

    final_response = extract_response_text(turn_result)
    return complete_agent_turn(
        motet, turn_result, final_response,
        qualified_id, parent_command_id, prepared_context_info,
    )

Notes:
    - Media helpers live here (not turn/command.py) so complete_agent_turn has no
      import cycle with command.py; turn package / orchestration.py re-export them.
    - Finalize / suspended early-return stay in agent_turn; this module only
      covers the successful-completion terminal path after observe_events exits.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import structlog

from motet.core.orchestration.turn.budget_continue import (
    BUDGET_STOP_FALLBACK_MESSAGE,
    is_budget_stop,
)

logger = structlog.get_logger(__name__)

# Matches the artifact image-link form agents emit to display generated images,
# e.g. ``![a blue cat](artifact:a1c250a1-7a1a-4cb5-9d60-04e7569f20dd)`` (ADR-0113).
_ARTIFACT_IMAGE_MD_RE = re.compile(r"!\[([^\]]*)\]\(\s*artifact:([^)\s]+)\s*\)")


def _iter_tool_result_dicts(payload: Any) -> List[Dict[str, Any]]:
    """Yield tool-result dicts from a payload, checking the common nesting shapes.

    The turn result may expose ``tool_results`` at the top level or under ``data``.
    Nested agent turns can also wrap the agent payload under ``result``.
    """
    seen: List[Dict[str, Any]] = []
    if not isinstance(payload, dict):
        return seen
    candidates: List[Any] = [
        payload.get("tool_results"),
        (payload.get("data") or {}).get("tool_results") if isinstance(payload.get("data"), dict) else None,
        (payload.get("result") or {}).get("tool_results") if isinstance(payload.get("result"), dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            for entry in candidate:
                if isinstance(entry, dict):
                    seen.append(entry)
    return seen


def _media_type_for_content_type(content_type: str) -> str:
    """Map an artifact MIME type to a canonical MediaPart media_type (ADR-0113)."""
    ct = (content_type or "").lower()
    if ct.startswith("image/"):
        return "image"
    if ct.startswith("audio/"):
        return "audio"
    if ct.startswith("video/"):
        return "video"
    return "file"


def _validate_and_enrich_media(
    parts: List[Dict[str, Any]],
    artifact_store: Any,
) -> List[Dict[str, Any]]:
    """Drop parts whose artifact is missing and fill mime/media_type from metadata.

    Best-effort and non-fatal: when the artifact store is unavailable or a lookup
    raises, the part is kept as-is rather than dropped (we only drop on a definitive
    "not found"). This prevents hallucinated/invalid ``artifact:<id>`` references from
    rendering as permanent broken placeholders, while resolving the real ``mime_type``
    for parts synthesized from text (which otherwise guess ``image``).
    """
    if artifact_store is None:
        return parts
    enriched: List[Dict[str, Any]] = []
    for part in parts:
        artifact_id = str(part.get("artifact_id") or "")
        if not artifact_id:
            continue
        try:
            meta = artifact_store.get_metadata(artifact_id)
        except Exception as exc:  # noqa: BLE001 - best-effort; keep part on lookup error
            logger.debug(
                "generated_media_metadata_lookup_failed",
                artifact_id=artifact_id,
                error=str(exc),
            )
            enriched.append(part)
            continue
        if meta is None:
            logger.info(
                "generated_media_artifact_missing_dropped",
                artifact_id=artifact_id,
            )
            continue
        content_type = getattr(meta, "content_type", None)
        if content_type:
            part.setdefault("mime_type", content_type)
            part["media_type"] = _media_type_for_content_type(content_type)
        enriched.append(part)
    return enriched


def _collect_generated_media(
    payload: Any,
    text: str = "",
    artifact_store: Any = None,
) -> List[Dict[str, Any]]:
    """Collect artifact-backed media parts produced during the turn (ADR-0113).

    Three complementary sources are merged and de-duplicated by ``artifact_id`` so
    the chat surface/UI can render generated media (e.g. images) alongside the text
    response, regardless of how the turn propagated its tool output. Earlier sources
    win on conflict (they carry richer, authoritative data):

    1. Loop accumulator: the top-level ``media`` list the agentic loop carries across
       iterations and surfaces on the terminal result (authoritative; real ``mime_type``).
    2. Structured tool_results: any ``media`` list still attached to the turn's
       ``tool_results`` (e.g. fast-path / single-iteration turns).
    3. Text fallback: ``![alt](artifact:<id>)`` references the model embedded in the
       final response, for flows where neither structured source propagated.

    When ``artifact_store`` is provided, collected parts are validated against it:
    missing artifacts are dropped and real ``mime_type``/``media_type`` are filled in.
    """
    media: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()

    def _add(part: Any) -> None:
        if not isinstance(part, dict):
            return
        artifact_id = str(part.get("artifact_id") or "")
        if not artifact_id or artifact_id in seen_ids:
            return
        seen_ids.add(artifact_id)
        media.append(dict(part))

    # Source 1: top-level accumulated media (ADR-0113 loop accumulator), checking the
    # common nesting shapes (top level, under data, under result).
    if isinstance(payload, dict):
        for container in (
            payload,
            payload.get("data") if isinstance(payload.get("data"), dict) else None,
            payload.get("result") if isinstance(payload.get("result"), dict) else None,
        ):
            if isinstance(container, dict) and isinstance(container.get("media"), list):
                for part in container["media"]:
                    _add(part)

    # Source 2: structured tool_results media.
    for entry in _iter_tool_result_dicts(payload):
        result = entry.get("result")
        if not isinstance(result, dict):
            continue
        parts = result.get("media")
        if isinstance(parts, list):
            for part in parts:
                _add(part)

    # Source 3: artifact image links inlined in the response text.
    if isinstance(text, str) and "artifact:" in text:
        for match in _ARTIFACT_IMAGE_MD_RE.finditer(text):
            alt = (match.group(1) or "").strip()
            artifact_id = (match.group(2) or "").strip()
            if not artifact_id or artifact_id in seen_ids:
                continue
            _add(
                {
                    "type": "media",
                    "media_type": "image",
                    "artifact_id": artifact_id,
                    "alt": alt or "generated image",
                }
            )

    return _validate_and_enrich_media(media, artifact_store)


def extract_response_text(payload: Any) -> str:
    """Extract the best available assistant text from heterogeneous strategy outputs."""
    if not isinstance(payload, dict):
        return str(payload or "")
    for key in ("content", "final_response", "final_answer"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    nested = payload.get("data")
    if isinstance(nested, dict):
        for key in ("content", "final_response", "final_answer"):
            value = nested.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def extract_turn_usage(payload: Any) -> Optional[Dict[str, int]]:
    """Pull aggregated token usage from the agent loop or a no-tools reply.

    ``agentic_loop`` already accumulates usage across model calls (ADR-0064 R9).
    Surfaces that value on the terminal stream event so OpenAI-compat agent mode
    (and other chat consumers) can report a turn total (ADR-0125).
    """
    if not isinstance(payload, dict):
        return None

    candidates: List[Any] = [payload.get("usage")]
    nested = payload.get("data")
    if isinstance(nested, dict):
        candidates.append(nested.get("usage"))
        candidates.append(nested)

    usage_keys = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "reasoning_tokens",
    )
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        source = candidate.get("usage") if isinstance(candidate.get("usage"), dict) else candidate
        if not isinstance(source, dict):
            continue
        if not any(source.get(key) is not None for key in usage_keys):
            continue
        normalized: Dict[str, int] = {
            key: int(source.get(key) or 0)
            for key in usage_keys
            if source.get(key) is not None
        }
        if "total_tokens" not in normalized:
            normalized["total_tokens"] = (
                normalized.get("prompt_tokens", 0) + normalized.get("completion_tokens", 0)
            )
        return normalized

    if any(payload.get(key) is not None for key in ("prompt_tokens", "completion_tokens", "total_tokens")):
        normalized = {
            key: int(payload.get(key) or 0)
            for key in usage_keys
            if payload.get(key) is not None
        }
        if "total_tokens" not in normalized:
            normalized["total_tokens"] = (
                normalized.get("prompt_tokens", 0) + normalized.get("completion_tokens", 0)
            )
        return normalized
    return None


def extract_turn_cost(payload: Any) -> Optional[float]:
    """Pull the turn cost from the agent loop or a nested result envelope.

    The agentic loop sums each priced model call into a top-level ``cost_usd``
    (see ``react/loop_results.accumulate_usage``). Returns None when no model
    call reported a cost, which callers must keep distinct from a zero-cost
    turn.
    """
    if not isinstance(payload, dict):
        return None

    for candidate in (payload, payload.get("data")):
        if not isinstance(candidate, dict):
            continue
        cost = candidate.get("cost_usd")
        if isinstance(cost, bool):
            continue
        if isinstance(cost, (int, float)):
            return float(cost)
    return None


def resolve_turn_model(
    payload: Any,
    *,
    provider: Optional[str] = None,
    model_name: Optional[str] = None,
) -> Optional[str]:
    """Best-effort model identifier for a completed turn.

    Prefers what the reasoning result reported, falling back to the agent's
    configured provider/model so exports still name a model on results that omit
    it.
    """
    if isinstance(payload, dict):
        for candidate in (payload, payload.get("data")):
            if not isinstance(candidate, dict):
                continue
            for key in ("model", "model_name", "model_id"):
                value = candidate.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

    if not model_name:
        return None
    return f"{provider}/{model_name}" if provider else model_name


def complete_agent_turn(
    motet: Any,
    turn_result: Any,
    final_response: str,
    qualified_id: str,
    parent_command_id: Optional[str],
    prepared_context_info: Dict[str, Any],
    analysis_metadata: Dict[str, Any],
) -> Dict[str, Any]:
    """Emit terminal stream events and build the agent_turn success return dict.

    Covers generated-media collection through the ``end`` /
    ``agent_turn_complete`` stream event and the response payload. Callers that
    already finalized (or skipped finalize on suspend) pass the post-reasoning
    ``final_response`` and ``turn_result`` as-is.
    """
    # ADR-0113: surface artifact-backed media (e.g. generated images) produced during the
    # turn, so the surface/UI can render it alongside the text response. Merges structured
    # tool_results media with artifact image links inlined in the final response text.
    generated_media = _collect_generated_media(
        turn_result,
        final_response,
        artifact_store=getattr(motet, "artifact_store", None),
    )
    turn_usage = extract_turn_usage(turn_result)

    # Terminal stream contract: only top-level turn emits `end`.
    # Nested agent turns (e.g. workflow fan-out/fan-in) emit a non-terminal completion
    # event so sibling/child steps can continue streaming.
    terminal_fields: Dict[str, Any] = {
        "media": generated_media,
        "artifact_rag_citations": prepared_context_info.get("artifact_rag_citations", []),
    }
    if turn_usage is not None:
        terminal_fields["usage"] = turn_usage
    turn_stop_reason = None
    if isinstance(turn_result, dict):
        turn_stop_reason = turn_result.get("stop_reason")
    if turn_stop_reason:
        terminal_fields["stop_reason"] = turn_stop_reason
    # Never leave the wire empty on budget stops (Cursor "continue" thrash).
    if not str(final_response or "").strip() and is_budget_stop(
        str(turn_stop_reason) if turn_stop_reason else None
    ):
        final_response = BUDGET_STOP_FALLBACK_MESSAGE

    if parent_command_id:
        motet.stream_event(
            "agent_turn_complete",
            agent_id=qualified_id,
            final_response=final_response,
            **terminal_fields,
        )
    else:
        motet.stream_event(
            "end",
            content=final_response,
            **terminal_fields,
        )
    response: Dict[str, Any] = {
        "agent_id": qualified_id,
        "final_response": final_response,
        "media": generated_media,
        "result": turn_result,
        "analysis_metadata": analysis_metadata,
        "context_info": prepared_context_info,
        "artifact_rag_citations": prepared_context_info.get("artifact_rag_citations", []),
        "usage": turn_usage,
    }
    # Callers outside orchestration (OpenAI facade) branch on stop_reason, so it
    # must survive the loop result -> turn response hop, not just the stream event.
    if turn_stop_reason:
        response["stop_reason"] = turn_stop_reason
    return response


__all__ = [
    "extract_response_text",
    "extract_turn_cost",
    "extract_turn_usage",
    "resolve_turn_model",
    "complete_agent_turn",
    "_collect_generated_media",
    "_validate_and_enrich_media",
    "_iter_tool_result_dicts",
    "_media_type_for_content_type",
]
