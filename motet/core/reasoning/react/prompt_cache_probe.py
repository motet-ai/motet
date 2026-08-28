"""
Motet - Prompt Cache Prefix Probe

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Diagnostic probe that explains provider prompt-cache misses in the agentic
    loop. Provider prefix caches match a byte-exact prefix of
    tools -> system -> messages and are all-or-nothing from the first changed
    byte onward: an *append* to the tail keeps the cache, while any *rewrite*
    upstream of the tail re-ingests everything after it at full input price.

    Aggregate usage counters cannot distinguish those two cases. This module
    fingerprints each prompt segment (one per tool schema, one per message),
    chains those digests into a rolling prefix hash, and compares the chain to
    the previous model call on the same conversation. The emitted
    ``prompt_cache_probe`` log line names the exact segment where the prefix
    diverged and how many characters were downstream of it — i.e. the concrete
    cause and cost of the miss.

    Because state lives in Redis rather than process memory, the comparison
    also spans turn suspension/resume, so a prefix rewritten while
    reconstructing a resumed turn is caught the same way as one rewritten
    in-process by context trimming.

Dependencies:
    - motet.core.distributed.redis_manager: Centralized Redis operations
      (store/retrieve_structured_data_sync per AGENTS.md requirements)
    - structlog: Structured logging for the emitted diagnostic events

Usage:
    Enable per deployment (off by default; hashing the full prompt on every
    model call is diagnostic-only overhead)::

        MOTET_PROMPT_CACHE_PROBE=true

    Then, from the agentic loop, immediately before the model call::

        from .prompt_cache_probe import record_prompt_fingerprint

        record_prompt_fingerprint(
            tenant_id=motet.tenant_id,
            motet_id=motet.motet_id,
            conversation_id=getattr(motet, "conversation_id", None),
            tools=sorted_tools,          # the exact list handed to the model
            messages=data.conversation_history,
            iteration=current_iteration,
            model_calls_used=data.model_calls_used,
        )

    Read the results back out of worker logs::

        docker logs motet_dev-worker-1-1 | rg prompt_cache_probe

Notes:
    - Pass the *post-sort* tool list (``_sort_tool_schemas_for_caching``) so the
      fingerprint reflects what the provider actually received.
    - ``verdict=append_only`` means the prefix was preserved and a cache miss on
      that call is not attributable to prompt shape (suspect cache TTL or
      provider-side eviction). ``verdict=prefix_rewritten`` means Motet changed
      the prompt upstream of the tail and the miss was self-inflicted.
    - ``lost_chars`` is the size of the invalidated suffix: the practical upper
      bound on tokens that had to be re-ingested on that call.
    - Best-effort throughout: every failure path degrades to a warning and never
      fails the turn.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

import structlog

from ...types import tool_schema_name

logger = structlog.get_logger(__name__)

_SERVICE = "prompt_cache_probe"
PROBE_TTL_SECONDS = 6 * 3600

# Digest prefix length retained per segment. Full SHA-256 hex is unnecessary for
# change detection and would bloat the stored chain on long conversations.
_DIGEST_CHARS = 16

# Segment labels are logged verbatim; cap them so one pathological tool name or
# role string cannot dominate a log line.
_MAX_LABEL_CHARS = 80


def probe_enabled() -> bool:
    """Whether the prompt-cache probe is enabled (``MOTET_PROMPT_CACHE_PROBE``)."""
    return os.getenv("MOTET_PROMPT_CACHE_PROBE", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _probe_key(
    tenant_id: Optional[str],
    motet_id: Optional[str],
    conversation_id: str,
) -> str:
    return f"prompt_cache_probe:{tenant_id or 'global'}:{motet_id or 'default'}:{conversation_id}"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:_DIGEST_CHARS]


def _label(raw: str) -> str:
    label = (raw or "").strip() or "unknown"
    return label[:_MAX_LABEL_CHARS]


def _canonical_json(value: Any) -> str:
    """Serialize deterministically so equal payloads always yield equal digests."""
    try:
        return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    except Exception:
        return repr(value)


def _tool_payload(schema: Any) -> Any:
    """Best-effort canonical payload for a canonical or legacy tool schema."""
    if hasattr(schema, "model_dump"):
        try:
            return schema.model_dump()
        except Exception:
            return repr(schema)
    return schema


def _message_payload(message: Any) -> Any:
    """Canonical payload covering every field that reaches the provider wire."""
    if isinstance(message, dict):
        return message
    payload: Dict[str, Any] = {
        "role": getattr(message, "role", None),
        "content": getattr(message, "content", None),
        "name": getattr(message, "name", None),
        "tool_call_id": getattr(message, "tool_call_id", None),
    }
    for field in ("tool_calls", "content_parts", "reasoning_content"):
        value = getattr(message, field, None)
        if value:
            payload[field] = _tool_payload(value) if hasattr(value, "model_dump") else value
    return payload


def _segments(
    tools: Optional[Sequence[Any]],
    messages: Optional[Sequence[Any]],
) -> List[Tuple[str, int, str]]:
    """
    Fingerprint the prompt in provider prefix order.

    Returns ``[(label, char_len, digest), ...]`` with one entry per tool schema
    followed by one per message. Tools are segmented individually (rather than as
    a single block) so a divergence report can name the specific schema that
    changed; the caller must pass them in the same order the provider received.
    """
    segments: List[Tuple[str, int, str]] = []
    for schema in tools or []:
        serialized = _canonical_json(_tool_payload(schema))
        label = f"tool:{tool_schema_name(schema) or 'unnamed'}"
        segments.append((_label(label), len(serialized), _digest(serialized)))
    for index, message in enumerate(messages or []):
        serialized = _canonical_json(_message_payload(message))
        role = getattr(message, "role", None) or (
            message.get("role") if isinstance(message, dict) else None
        )
        segments.append(
            (_label(f"msg{index}:{role or 'unknown'}"), len(serialized), _digest(serialized))
        )
    return segments


def _chain(segments: Sequence[Tuple[str, int, str]]) -> List[str]:
    """Rolling prefix hashes: ``chain[i]`` covers segments ``0..i`` inclusive."""
    chain: List[str] = []
    rolling = ""
    for _, _, digest in segments:
        rolling = _digest(f"{rolling}|{digest}")
        chain.append(rolling)
    return chain


def _first_divergence(previous: Sequence[str], current: Sequence[str]) -> Optional[int]:
    """Index of the first differing prefix hash, or None when one is a prefix of the other."""
    for index in range(min(len(previous), len(current))):
        if previous[index] != current[index]:
            return index
    return None


def _describe(
    segments: Sequence[Tuple[str, int, str]],
    index: int,
) -> str:
    """Human-readable label for the segment at ``index`` ("<absent>" past the end)."""
    if 0 <= index < len(segments):
        label, length, _ = segments[index]
        return f"{label}({length}c)"
    return "<absent>"


def record_prompt_fingerprint(
    *,
    tenant_id: Optional[str],
    motet_id: Optional[str],
    conversation_id: Optional[str],
    tools: Optional[Sequence[Any]],
    messages: Optional[Sequence[Any]],
    iteration: Optional[int] = None,
    model_calls_used: Optional[int] = None,
) -> None:
    """
    Fingerprint this call's prompt prefix and log how it diverged from the last one.

    No-op unless ``MOTET_PROMPT_CACHE_PROBE`` is enabled and a conversation id is
    present (cross-call comparison needs stable identity). Never raises: probe
    failures degrade to a warning so diagnostics can never break a turn.

    Emits ``prompt_cache_probe`` with:
        - ``verdict``: ``first_call`` | ``append_only`` | ``prefix_rewritten`` | ``prefix_truncated``
        - ``divergence_index`` / ``divergence_segment``: where the prefix broke,
          plus ``previous_segment`` for the value it replaced
        - ``lost_chars``: size of the invalidated suffix (upper bound on tokens re-ingested)
        - ``cacheable_chars``: size of the surviving cacheable prefix
    """
    if not probe_enabled() or not conversation_id:
        return

    try:
        from ...distributed.redis_manager import (
            get_sync_redis_client,
            retrieve_structured_data_sync,
            store_structured_data_sync,
        )

        segments = _segments(tools, messages)
        if not segments:
            return
        chain = _chain(segments)
        total_chars = sum(length for _, length, _ in segments)

        key = _probe_key(tenant_id, motet_id, conversation_id)
        stored = retrieve_structured_data_sync(_SERVICE, key, format_type="json_string") or {}
        stored_chain = stored.get("chain")
        previous_chain: List[str] = (
            [str(entry) for entry in stored_chain] if isinstance(stored_chain, list) else []
        )
        stored_segments = stored.get("segments")
        previous_segments: List[Tuple[str, int, str]] = [
            (str(entry[0]), int(entry[1]), str(entry[2]))
            for entry in (stored_segments if isinstance(stored_segments, list) else [])
            if isinstance(entry, (list, tuple)) and len(entry) == 3
        ]

        store_structured_data_sync(
            _SERVICE,
            key,
            {
                "chain": chain,
                "segments": [[label, length, digest] for label, length, digest in segments],
            },
            format_type="json_string",
        )
        get_sync_redis_client(_SERVICE).expire(key, PROBE_TTL_SECONDS)

        if not previous_chain:
            logger.info(
                "prompt_cache_probe",
                verdict="first_call",
                conversation_id=conversation_id,
                iteration=iteration,
                model_calls_used=model_calls_used,
                segment_count=len(segments),
                total_chars=total_chars,
            )
            return

        divergence = _first_divergence(previous_chain, chain)
        if divergence is None:
            # One chain is a prefix of the other: appended (cache preserved) or
            # truncated (the tail vanished, which still breaks a longer prefix).
            verdict = "append_only" if len(chain) >= len(previous_chain) else "prefix_truncated"
            lost_chars = 0
            cacheable_chars = sum(
                length for _, length, _ in segments[: min(len(previous_chain), len(chain))]
            )
        else:
            verdict = "prefix_rewritten"
            lost_chars = sum(length for _, length, _ in segments[divergence:])
            cacheable_chars = total_chars - lost_chars

        logger.info(
            "prompt_cache_probe",
            verdict=verdict,
            conversation_id=conversation_id,
            iteration=iteration,
            model_calls_used=model_calls_used,
            segment_count=len(segments),
            previous_segment_count=len(previous_chain),
            total_chars=total_chars,
            cacheable_chars=cacheable_chars,
            lost_chars=lost_chars,
            divergence_index=divergence,
            divergence_segment=(
                _describe(segments, divergence) if divergence is not None else None
            ),
            previous_segment=(
                _describe(previous_segments, divergence)
                if divergence is not None and previous_segments
                else None
            ),
        )

    except Exception as e:
        logger.warning(
            "prompt_cache_probe_failed",
            conversation_id=conversation_id,
            iteration=iteration,
            error=str(e),
            error_type=type(e).__name__,
        )
