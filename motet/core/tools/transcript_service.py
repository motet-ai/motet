"""
Motet - Tool Transcript Reconstruction Service

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Reconstructs schema-correct tool transcripts for model context from persisted
    ToolInvocation records. parse_and_dedupe_tool_invocation_memories
    is shared by finalize_turn (transcript storage) and reconstruct_tool_transcripts()
    (replay). This module centralizes parsing, deduplication, batching, provider-safe
    rendering, and artifact retrieval gating for use by prepare_context.

    Oversized tool arguments are hydrated from ``arguments_artifact_id`` before
    render; result ``artifact_id`` token-budget nulling must never touch args.

Dependencies:
    - motet.core.tools.tool_transcripts: ToolInvocation, ToolInvocationStatus
    - motet.core.tools.rendering: get_renderer (provider-aware)
    - motet.core.artifacts: get_artifact_store
    - motet.core.types: Message
    - structlog: structured logging

Usage:
    from motet.core.tools.transcript_service import reconstruct_tool_transcripts

    tuples = reconstruct_tool_transcripts(
        tool_invocation_memories=tool_invocations,
        motet=motet,
        provider_name="openai",
    )

Notes:
    - Fail-closed: skips invalid records and omits transcript segments that cannot be
      rendered schema-correctly.
    - STARTED-only invocations are omitted to avoid schema-invalid OpenAI transcripts.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

import structlog

from ..artifacts import get_artifact_store
from ..types import Message
from .arguments_offload import arguments_unsafe_for_provider_replay
from .rendering import get_renderer
from .tool_transcripts import ToolInvocation, ToolInvocationStatus, dedupe_tool_invocations

logger = structlog.get_logger(__name__)


def parse_and_dedupe_tool_invocation_memories(
    memory_items: Iterable[Any],
    *,
    task_id: Optional[str] = None,
    supported_schema_versions: Optional[set[str]] = None,
    log_started_only: bool = True,
    log_context: Optional[Dict[str, Any]] = None,
) -> List[ToolInvocation]:
    """
    Parse tool_invocation memory items, filter by task_id, dedupe by tool_call_id, return final-only list.

    Used by finalize_turn (with task_id) and by reconstruct_tool_transcripts (no task_id).
    When log_started_only is True, logs a warning for tool_call_ids that had only STARTED records.
    log_context is merged into that warning (e.g. conversation_id, task_id for tracing).
    """
    supported = supported_schema_versions or {"1.0"}
    parsed: List[ToolInvocation] = []
    for memory in memory_items:
        try:
            metadata = getattr(memory, "metadata", {}) or {}
            if task_id is not None and metadata.get("task_id") != task_id:
                continue
            inv = ToolInvocation.model_validate(metadata)
            if inv.schema_version not in supported:
                logger.warning(
                    "tool_invocation_schema_version_unsupported",
                    schema_version=inv.schema_version,
                    supported=sorted(supported),
                    tool_name=getattr(inv, "tool_name", None),
                    tool_call_id=getattr(inv, "tool_call_id", None),
                )
                continue
            parsed.append(inv)
        except Exception as e:
            logger.warning(
                "tool_invocation_parse_failed",
                memory_id=getattr(memory, "id", None),
                error=str(e),
            )
            continue
    final_invs, started_only_ids = dedupe_tool_invocations(parsed)
    if log_started_only and started_only_ids:
        logger.warning(
            "canonical_transcript_started_only_tool_invocations_skipped",
            skipped_count=len(started_only_ids),
            skipped_tool_call_ids=started_only_ids[:10],
            note="ToolInvocation STARTED records were present without a final status; skipped for replay.",
            **(log_context or {}),
        )
    return final_invs


def reconstruct_tool_transcripts(
    *,
    tool_invocation_memories: Iterable[Tuple[float, Any]],
    motet: Any,
    provider_name: str,
    supported_schema_versions: Optional[set[str]] = None,
    artifact_fetch_limit: int = 5,
) -> List[Tuple[float, Message]]:
    """
    Reconstruct tool transcript messages (timestamped) from stored tool_invocation memories.

    Args:
        tool_invocation_memories: iterable of (timestamp_float, MemoryItem-like) tuples
        motet: MotetContext (for fallback tenant/principal/motet and logging context)
        provider_name: model provider identifier (e.g., "openai", "anthropic")
        supported_schema_versions: optional allowlist of supported ToolInvocation schema versions

    Returns:
        List of (timestamp_float, Message) tuples to merge into conversation history.
    """
    memory_items = [m for _, m in tool_invocation_memories]
    invs = parse_and_dedupe_tool_invocation_memories(
        memory_items,
        supported_schema_versions=supported_schema_versions,
        log_started_only=False,
    )
    if not invs:
        return []

    # Group by group_id, else singleton
    grouped: Dict[str, List[ToolInvocation]] = {}
    singles: List[ToolInvocation] = []
    for inv in invs:
        if inv.tool_call_group_id:
            grouped.setdefault(inv.tool_call_group_id, []).append(inv)
        else:
            singles.append(inv)

    renderer = get_renderer(provider_name=provider_name)
    artifact_store = get_artifact_store()

    # Token budgeting guidance (ADR-0061): only fetch full artifacts for a small number
    # of most-recent invocations; older invocations fall back to preview_observation.
    try:
        def _ts(inv: ToolInvocation) -> float:
            t = inv.completed_at or inv.started_at
            return t.timestamp() if t else 0.0

        sorted_invs = sorted(invs, key=_ts, reverse=True)
        allow_artifact_ids = {i.tool_call_id for i in sorted_invs[: max(0, int(artifact_fetch_limit))]}
        for i in invs:
            if i.tool_call_id not in allow_artifact_ids:
                i.artifact_id = None
    except Exception:
        # Best-effort: if any issues, do not mutate artifact ids.
        pass

    def make_artifact_getter(invocations: List[ToolInvocation]):
        first = invocations[0] if invocations else None
        tenant = (first.tenant_id if first else None) or getattr(motet, "tenant_id", None)
        principal = (first.principal_id if first else None) or getattr(motet, "principal_id", None)
        motet_id = (first.motet_id if first else None) or getattr(motet, "motet_id", None)

        def artifact_getter(artifact_id: str) -> Any:
            payload = artifact_store.get(
                artifact_id,
                tenant_id=tenant,
                principal_id=principal,
                motet_id=motet_id,
            )
            if payload is None:
                logger.info(
                    "tool_artifact_missing_or_denied",
                    artifact_id=artifact_id,
                    tenant_id=tenant,
                    principal_id=principal,
                    motet_id=motet_id,
                    conversation_id=getattr(motet, "conversation_id", None),
                )
            return payload

        return artifact_getter

    def finalize(invocations: List[ToolInvocation]) -> List[ToolInvocation]:
        # Fail-closed: avoid STARTED-only transcripts (schema-invalid for OpenAI tool messages).
        return [i for i in invocations if i.status != ToolInvocationStatus.STARTED]

    def hydrate_arguments(invocations: List[ToolInvocation]) -> List[ToolInvocation]:
        """
        Restore full unmodified arguments_json from arguments_artifact_id.

        Distinct from result artifact_id budgeting — args must always hydrate when
        present, or the invocation is omitted (never replay truncated/invalid JSON).
        """
        out_invs: List[ToolInvocation] = []
        for inv in invocations:
            artifact_id = getattr(inv, "arguments_artifact_id", None)
            if artifact_id:
                getter = make_artifact_getter([inv])
                payload = getter(str(artifact_id))
                if isinstance(payload, (bytes, bytearray)):
                    try:
                        full = bytes(payload).decode("utf-8")
                    except UnicodeDecodeError:
                        full = None
                elif isinstance(payload, str):
                    full = payload
                else:
                    full = None
                if not full or not str(full).strip():
                    logger.warning(
                        "tool_arguments_artifact_missing_omitting_invocation",
                        tool_call_id=inv.tool_call_id,
                        tool_name=inv.tool_name,
                        arguments_artifact_id=artifact_id,
                    )
                    continue
                out_invs.append(inv.model_copy(update={"arguments_json": full}))
                continue
            if arguments_unsafe_for_provider_replay(
                inv.arguments_json,
                arguments_artifact_id=None,
            ):
                logger.warning(
                    "tool_arguments_unsafe_omitting_invocation",
                    tool_call_id=inv.tool_call_id,
                    tool_name=inv.tool_name,
                )
                continue
            out_invs.append(inv)
        return out_invs

    out: List[Tuple[float, Message]] = []

    # Render groups (batched tool calls)
    for _group_id, invs in grouped.items():
        invs.sort(key=lambda x: x.tool_call_index or 0)
        invs_final = hydrate_arguments(finalize(invs))
        if not invs_final:
            continue

        artifact_getter = make_artifact_getter(invs_final)
        assistant_msgs = renderer.render_assistant_call(invs_final)
        tool_msgs = renderer.render_tool_results(invs_final, artifact_getter)

        ts = invs_final[0].started_at.timestamp() if invs_final[0].started_at else 0.0
        for m in assistant_msgs + tool_msgs:
            out.append((ts, m))

    # Render singles
    for inv in singles:
        invs_final = hydrate_arguments(finalize([inv]))
        if not invs_final:
            continue

        artifact_getter = make_artifact_getter(invs_final)
        assistant_msgs = renderer.render_assistant_call(invs_final)
        tool_msgs = renderer.render_tool_results(invs_final, artifact_getter)

        inv0 = invs_final[0]
        ts = inv0.started_at.timestamp() if inv0.started_at else 0.0
        for m in assistant_msgs + tool_msgs:
            out.append((ts, m))

    return out


__all__ = ["parse_and_dedupe_tool_invocation_memories", "reconstruct_tool_transcripts"]


