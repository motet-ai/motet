"""
Motet - RAG Context Provider Hook

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Provides artifact RAG retrieval for the context preparation
    provider pipeline. When enabled, it retrieves scoped, citation-ready chunks
    for the latest user query and prepends them as text content before model
    inference. Inline attachment fallbacks are removed only when retrieved chunks
    actually supersede them (e.g. transcript_segment for video transcripts).

Dependencies:
    - motet.core.types for canonical TextPart construction
    - context.types for shared pipeline state

Usage:
    state = RagContextProvider().apply(state, data=data, motet=motet, logger=logger)

Notes:
    - The provider remains no-op unless `artifact_rag_enabled` is set.
    - Retrieval command failures are captured in context metadata so ordinary
      full-document artifact injection remains available as fallback.
"""

from __future__ import annotations

import re
from typing import Any

from ...types import TextPart
from .types import ContextPipelineState

_TEXT_TOKEN_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]*")
_ARTIFACT_REFERENCE_TERMS = (
    "artifact",
    "attachment",
    "doc",
    "document",
    "file",
    "pdf",
    "report",
    "spreadsheet",
    "upload",
    "uploaded",
)
_DOCUMENT_ACTION_TERMS = (
    "analyze",
    "analysis",
    "cite",
    "compare",
    "extract",
    "find",
    "findings",
    "read",
    "risk",
    "risks",
    "say",
    "says",
    "summarize",
    "summary",
)
_FOLLOWUP_REFERENCE_TERMS = (
    "it",
    "that",
    "this",
    "these",
    "those",
)
_LOW_VALUE_QUERIES = {
    "hi",
    "hello",
    "hey",
    "thanks",
    "thank you",
    "ok",
    "okay",
    "yes",
    "no",
}
_SIGNAL_FREE_ATTACHMENT_QUERIES = {
    "read this",
    "transcribe this",
    "what does it say",
    "what does this say",
    "what is in this",
    "what's in this",
    "what is this",
    "what's this",
}
_MIN_SIMILARITY_WHEN_ARTIFACT_SCOPED = 0.15


def _analysis_rag_signal(data: Any) -> dict[str, Any]:
    analysis_metadata = getattr(data, "analysis_metadata", None)
    if analysis_metadata is None:
        return {}
    rag_signal = getattr(analysis_metadata, "rag", None)
    if rag_signal is None and isinstance(analysis_metadata, dict):
        rag_signal = analysis_metadata.get("rag")
    return rag_signal if isinstance(rag_signal, dict) else {}


def _execution_context(data: Any) -> dict[str, Any]:
    context = getattr(data, "context", None) or {}
    return context if isinstance(context, dict) else {}


def _list_context_values(context: dict[str, Any], *keys: str) -> list[str]:
    """Extract de-duplicated string-list controls from request context."""

    values: list[str] = []
    for key in keys:
        raw = context.get(key)
        if raw is None:
            continue
        if isinstance(raw, str):
            values.extend(part.strip() for part in raw.split(","))
        elif isinstance(raw, list):
            values.extend(str(item).strip() for item in raw)
    return list(dict.fromkeys(value for value in values if value))


def _snippet(text: Any, max_chars: int = 280) -> str:
    """Return a compact single-line citation snippet."""

    normalized = " ".join(str(text or "").split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1].rstrip() + "..."


def _message_has_artifact_context(message: Any) -> bool:
    """Return True when a message carries artifact attachments or inline markers."""

    if bool(getattr(message, "attachments", None)):
        return True
    return any(
        getattr(part, "type", None) == "text"
        and isinstance(getattr(part, "text", None), str)
        and "<attachment " in getattr(part, "text", "")
        for part in list(getattr(message, "content_parts", None) or [])
    )


def _attachment_source_artifact_id(attachment_text: str) -> str | None:
    """Extract source_artifact_id from an inline attachment TextPart."""

    match = re.search(r"source_artifact_id='([^']+)'", attachment_text)
    return match.group(1) if match else None


def _approx_text_tokens(text: str) -> int:
    """Approximate token count using the same word-split heuristic as TokenBudgetProvider."""

    return len(" ".join((text or "").split()).split())


def _attachment_content_text(attachment_text: str) -> str:
    """Extract the derived body from an inline <attachment> TextPart."""

    match = re.search(
        r"\[Use source_artifact_id[^\]]*\]\n(.*)(?:\n</attachment>|</attachment>)",
        attachment_text,
        re.DOTALL,
    )
    if match:
        return match.group(1).strip()
    fallback = re.search(r"<attachment[^>]*>\n?(.*)</attachment>", attachment_text, re.DOTALL)
    if fallback:
        return fallback.group(1).strip()
    return ""


def _attachment_part_token_count(part: Any) -> int:
    """Return approximate tokens for an inline attachment part."""

    text = getattr(part, "text", None)
    if not isinstance(text, str) or "<attachment " not in text:
        return 0
    if "content_status='pending" in text:
        return 0
    return _approx_text_tokens(_attachment_content_text(text))


def _this_turn_attachment_ids(message: Any) -> list[str]:
    """Return source artifact IDs attached to the current user message."""

    ids: list[str] = []
    for att in getattr(message, "attachments", None) or []:
        if not isinstance(att, dict):
            continue
        artifact_id = str(att.get("artifact_id") or "").strip()
        if artifact_id:
            ids.append(artifact_id)
    return list(dict.fromkeys(ids))


def _inline_attachment_budget_facts(message: Any) -> tuple[int, list[str]]:
    """Return max inline attachment token count and referenced source artifact IDs."""

    max_tokens = 0
    source_ids: list[str] = []
    for part in getattr(message, "content_parts", None) or []:
        text = getattr(part, "text", None)
        if not isinstance(text, str) or "<attachment " not in text:
            continue
        max_tokens = max(max_tokens, _attachment_part_token_count(part))
        source_id = _attachment_source_artifact_id(text)
        if source_id:
            source_ids.append(source_id)
    return max_tokens, list(dict.fromkeys(source_ids))


def _is_signal_free_attachment_query(query: str, *, attachment_count: int) -> bool:
    """Return True for generic deictic queries over a single this-turn attachment."""

    if attachment_count != 1:
        return False
    normalized = " ".join((query or "").strip().lower().rstrip("?").split())
    if normalized in _SIGNAL_FREE_ATTACHMENT_QUERIES:
        return True
    terms = set(_TEXT_TOKEN_RE.findall(normalized))
    return bool(terms.intersection(_FOLLOWUP_REFERENCE_TERMS) and terms.intersection(_DOCUMENT_ACTION_TERMS))


def _chunk_kinds_for_source(chunks: list[Any], source_artifact_id: str) -> set[str]:
    """Return chunk_kind values indexed for one source artifact."""

    kinds: set[str] = set()
    for raw_chunk in chunks:
        if not isinstance(raw_chunk, dict):
            continue
        if str(raw_chunk.get("source_artifact_id") or "") != source_artifact_id:
            continue
        kind = str(raw_chunk.get("chunk_kind") or "").strip()
        if kind:
            kinds.add(kind)
    return kinds


def _rag_supersedes_attachment_part(part: Any, *, chunks: list[Any], token_budget: int) -> bool:
    """Return True when retrieved chunks make an inline attachment redundant."""

    if getattr(part, "type", None) != "text":
        return False
    text = getattr(part, "text", None)
    if not isinstance(text, str) or "<attachment " not in text:
        return False
    if "content_status='pending" in text:
        return False
    if _attachment_part_token_count(part) <= max(1, int(token_budget or 1)):
        return False

    source_artifact_id = _attachment_source_artifact_id(text)
    if not source_artifact_id:
        return True

    kinds = _chunk_kinds_for_source(chunks, source_artifact_id)
    if not kinds:
        return False

    if "artifact_id is the video transcript" in text or "source_content_type='video/" in text:
        return "transcript_segment" in kinds

    return bool(kinds - {"video_scene"})


def _build_artifact_citations(chunks: list[Any]) -> list[dict[str, Any]]:
    """Build stable, UI-facing citation metadata from retrieved chunks."""

    citations: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for rank, raw_chunk in enumerate(chunks, start=1):
        chunk = raw_chunk if isinstance(raw_chunk, dict) else {}
        source_artifact_id = str(chunk.get("source_artifact_id") or "").strip()
        strategy_id = str(chunk.get("prep_strategy_id") or "text_default")
        chunk_index_raw = chunk.get("chunk_index", 0)
        try:
            chunk_index = int(chunk_index_raw)
        except (TypeError, ValueError):
            chunk_index = 0
        key = (source_artifact_id, strategy_id, chunk_index)
        if not source_artifact_id or key in seen:
            continue
        seen.add(key)

        filename = str(chunk.get("filename") or source_artifact_id)
        raw_coordinates = chunk.get("coordinates")
        coordinates: dict[str, Any] = raw_coordinates if isinstance(raw_coordinates, dict) else {}
        coord_kind = str(coordinates.get("kind") or "")
        page_number = coordinates.get("page_number") or coordinates.get("page") or chunk.get("page_number")
        json_pointer = coordinates.get("pointer") if coord_kind == "json" else None
        line_start = coordinates.get("line_start") if coord_kind == "code" else None
        line_end = coordinates.get("line_end") if coord_kind == "code" else None
        heading_path = coordinates.get("heading_path") if coord_kind == "text" else None
        heading_label = " > ".join(str(part) for part in heading_path or [] if str(part).strip())
        table_range = coordinates.get("range") if coord_kind == "table" else None
        timestamp_start = coordinates.get("timestamp_start") if coord_kind == "media" else None
        timestamp_end = coordinates.get("timestamp_end") if coord_kind == "media" else None
        source_label = filename
        if page_number:
            source_label = f"{source_label}, page {page_number}"
        if heading_label:
            source_label = f"{source_label}, section {heading_label}"
        if json_pointer:
            source_label = f"{source_label}, {json_pointer}"
        if line_start:
            line_label = f"lines {line_start}-{line_end}" if line_end and line_end != line_start else f"line {line_start}"
            source_label = f"{source_label}, {line_label}"
        if table_range:
            source_label = f"{source_label}, table {table_range}"
        if timestamp_start is not None:
            if timestamp_end is not None:
                source_label = f"{source_label}, {timestamp_start:.1f}s-{timestamp_end:.1f}s"
            else:
                source_label = f"{source_label}, {timestamp_start:.1f}s"
        citation_id = f"A{len(citations) + 1}"
        score = chunk.get("hybrid_score") or chunk.get("similarity")
        citations.append(
            {
                "citation_id": citation_id,
                "rank": rank,
                "source_label": source_label,
                "artifact_id": source_artifact_id,
                "source_artifact_id": source_artifact_id,
                "derived_artifact_id": str(chunk.get("derived_artifact_id") or ""),
                "chunk_index": chunk_index,
                "chunk_kind": chunk.get("chunk_kind"),
                "prep_strategy_id": strategy_id,
                "prep_strategy_version": chunk.get("prep_strategy_version"),
                "coordinates": coordinates,
                "page_number": page_number,
                "heading_path": list(heading_path or []),
                "json_pointer": json_pointer,
                "line_start": line_start,
                "line_end": line_end,
                "table_range": table_range,
                "score": score,
                "api_path": f"/api/v1/artifacts/{source_artifact_id}",
                "text_snippet": _snippet(chunk.get("content_text")),
            }
        )
    return citations


class RagContextProvider:
    """Retrieve and inject semantic artifact chunks when ADR-0063 is enabled."""

    name = "rag_context"

    def apply(
        self,
        state: ContextPipelineState,
        *,
        data: Any,
        motet: Any,
        logger: Any,
    ) -> ContextPipelineState:
        cfg = getattr(getattr(motet, "stack", None), "config", None)
        if cfg is None or not bool(getattr(cfg, "artifact_rag_enabled", False)):
            state.context_info.setdefault("rag_context_enabled", False)
            return state

        state.context_info["rag_context_enabled"] = True
        try:
            last_user_msg = next((msg for msg in reversed(state.messages) if msg.role == "user"), None)
            query_text = getattr(last_user_msg, "content", "") if last_user_msg else ""
            if not query_text:
                state.context_info["rag_context_skipped"] = "empty_query"
                return state

            plan = self._build_retrieval_plan(
                query_text=query_text,
                message=last_user_msg,
                has_recent_artifact_context=any(_message_has_artifact_context(msg) for msg in state.messages),
                data=data,
                cfg=cfg,
            )
            state.context_info["artifact_rag_policy"] = plan
            if not plan["should_retrieve"]:
                state.context_info["rag_context_skipped"] = plan["reason"]
                return state

            from motet.core.commands.command_data_classes import RagRetrieveContextData
            from motet.core.commands.builtin.rag import rag_retrieve_context

            similarity_threshold = float(plan.get("similarity_threshold", getattr(cfg, "artifact_rag_similarity_threshold", 0.0)))
            result = motet.do(
                rag_retrieve_context,
                data=RagRetrieveContextData(
                    query_text=query_text,
                    scope=plan["scope"],
                    conversation_id=motet.conversation_id,
                    role="user",
                    artifact_ids=plan["artifact_ids"],
                    artifact_tags=plan["artifact_tags"],
                    top_k=plan["top_k"],
                    similarity_threshold=similarity_threshold,
                    token_budget=plan["token_budget"],
                    hybrid_enabled=bool(getattr(cfg, "artifact_rag_hybrid_enabled", True)),
                    vector_weight=float(getattr(cfg, "artifact_rag_vector_weight", 0.7)),
                    lexical_weight=float(getattr(cfg, "artifact_rag_lexical_weight", 0.3)),
                    candidate_multiplier=plan["candidate_multiplier"],
                    position_ordered=bool(plan.get("position_ordered")),
                ),
            )
            chunks = result.get("chunks", []) if isinstance(result, dict) else []
            context_text = result.get("context_text", "") if isinstance(result, dict) else ""
            state.context_info["vector_results"] = chunks
            state.context_info["artifact_rag_chunks"] = chunks
            state.context_info["artifact_rag_chunk_count"] = len(chunks)
            state.context_info["artifact_rag_citations"] = _build_artifact_citations(chunks)
            if not context_text or not last_user_msg:
                return state

            for msg in state.messages:
                existing_parts = list(getattr(msg, "content_parts", None) or [])
                if not existing_parts:
                    continue
                msg.content_parts = [  # type: ignore[attr-defined]
                    part
                    for part in existing_parts
                    if not _rag_supersedes_attachment_part(
                        part,
                        chunks=chunks,
                        token_budget=int(plan.get("token_budget") or 1),
                    )
                ]

            content_parts = list(getattr(last_user_msg, "content_parts", None) or [])
            if not content_parts:
                content_parts = [TextPart(text=getattr(last_user_msg, "content", ""))]
            content_parts.insert(0, TextPart(text=f"<artifact_rag_context>\n{context_text}\n</artifact_rag_context>"))
            last_user_msg.content_parts = content_parts  # type: ignore[attr-defined]
        except Exception as e:
            logger.warning(
                "artifact_rag_context_failed",
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            state.context_info["rag_context_error"] = str(e)
        return state

    @classmethod
    def _build_retrieval_plan(
        cls,
        *,
        query_text: str,
        message: Any,
        data: Any,
        cfg: Any,
        has_recent_artifact_context: bool = False,
    ) -> dict[str, Any]:
        """Build a deterministic RAG retrieval plan from analysis and request facts."""

        query = (query_text or "").strip()
        query_lower = query.lower()
        context = _execution_context(data)
        rag_signal = _analysis_rag_signal(data)
        artifact_ids = _list_context_values(context, "artifact_ids", "selected_artifact_ids", "artifact_rag_artifact_ids")
        artifact_tags = _list_context_values(context, "artifact_tags", "selected_artifact_tags", "artifact_rag_tags")
        turn_attachment_ids = _this_turn_attachment_ids(message)
        if turn_attachment_ids:
            artifact_ids = list(dict.fromkeys([*artifact_ids, *turn_attachment_ids]))

        has_attachment_marker = _message_has_artifact_context(message)
        has_message_attachment = bool(getattr(message, "attachments", None))
        query_terms = set(_TEXT_TOKEN_RE.findall(query_lower))
        has_artifact_reference = bool(query_terms.intersection(_ARTIFACT_REFERENCE_TERMS))
        has_document_action = bool(query_terms.intersection(_DOCUMENT_ACTION_TERMS))
        has_followup_reference = bool(query_terms.intersection(_FOLLOWUP_REFERENCE_TERMS))
        explicit_rag = bool(context.get("artifact_rag_enabled") or context.get("force_artifact_rag"))
        analysis_needs_rag = bool(rag_signal.get("needs_rag")) and float(rag_signal.get("confidence") or 0.0) >= 0.45

        if query_lower in _LOW_VALUE_QUERIES:
            return cls._skip_plan("low_value_query", cfg)

        should_retrieve = (
            has_attachment_marker
            or has_message_attachment
            or explicit_rag
            or analysis_needs_rag
            or (has_artifact_reference and ("?" in query or has_document_action))
            or (has_document_action and has_attachment_marker)
            or (has_recent_artifact_context and ("?" in query or has_document_action or has_followup_reference))
        )
        if not should_retrieve:
            return cls._skip_plan("no_artifact_rag_intent", cfg)

        scope = cls._determine_scope(context=context, rag_signal=rag_signal)
        action = str(rag_signal.get("artifact_action") or "").lower()
        if not action or action == "none":
            if query_terms.intersection({"summarize", "summary"}):
                action = "summary"
            elif "compare" in query_terms:
                action = "compare"
            elif "extract" in query_terms:
                action = "extract"
            else:
                action = "question"

        default_top_k = int(getattr(cfg, "artifact_rag_top_k", 5))
        default_budget = int(getattr(cfg, "artifact_rag_token_budget", 4000))
        default_multiplier = int(getattr(cfg, "artifact_rag_candidate_multiplier", 4))
        if action in {"summary", "compare", "extract"}:
            top_k = max(default_top_k, 8 if action == "compare" else 6)
            token_budget = max(default_budget, 6000 if action == "compare" else 5000)
            candidate_multiplier = max(default_multiplier, 4)
        else:
            top_k = min(default_top_k, 3)
            token_budget = min(default_budget, 3000)
            candidate_multiplier = default_multiplier

        token_budget = max(1, token_budget)
        inline_attachment_tokens, inline_source_ids = _inline_attachment_budget_facts(message)
        if inline_source_ids:
            artifact_ids = list(dict.fromkeys([*artifact_ids, *inline_source_ids]))

        if turn_attachment_ids and _is_signal_free_attachment_query(query, attachment_count=len(turn_attachment_ids)):
            return cls._skip_plan("signal_free_single_attachment", cfg, artifact_ids=artifact_ids or None)

        if inline_attachment_tokens > 0 and inline_attachment_tokens <= token_budget:
            return cls._skip_plan("full_text_in_budget", cfg, artifact_ids=artifact_ids or None)

        position_ordered = bool(artifact_ids) and inline_attachment_tokens > token_budget
        similarity_threshold = float(getattr(cfg, "artifact_rag_similarity_threshold", 0.0))
        if artifact_ids and not position_ordered:
            similarity_threshold = max(similarity_threshold, _MIN_SIMILARITY_WHEN_ARTIFACT_SCOPED)

        return {
            "should_retrieve": True,
            "reason": "artifact_rag_intent",
            "scope": scope,
            "artifact_action": action,
            "top_k": max(1, top_k),
            "token_budget": token_budget,
            "candidate_multiplier": max(1, candidate_multiplier),
            "artifact_ids": artifact_ids or None,
            "artifact_tags": artifact_tags or None,
            "analysis_signal": rag_signal,
            "position_ordered": position_ordered,
            "similarity_threshold": similarity_threshold,
        }

    @staticmethod
    def _determine_scope(*, context: dict[str, Any], rag_signal: dict[str, Any]) -> str:
        """Choose a retrieval scope conservatively from explicit context and analysis."""

        requested_scope = str(
            context.get("artifact_rag_scope")
            or context.get("retrieval_scope")
            or rag_signal.get("suggested_scope")
            or "conversation"
        ).lower()
        if requested_scope not in {"conversation", "principal", "motet"}:
            return "conversation"
        if requested_scope == "conversation":
            return "conversation"

        # Broader scopes require an explicit deterministic UI/request affordance.
        if context.get("allow_broader_artifact_rag_scope") is True:
            return requested_scope
        if str(context.get("artifact_rag_scope") or "").lower() == requested_scope:
            return requested_scope
        return "conversation"

    @staticmethod
    def _skip_plan(reason: str, cfg: Any, *, artifact_ids: list[str] | None = None) -> dict[str, Any]:
        return {
            "should_retrieve": False,
            "reason": reason,
            "scope": "conversation",
            "top_k": int(getattr(cfg, "artifact_rag_top_k", 5)),
            "token_budget": int(getattr(cfg, "artifact_rag_token_budget", 4000)),
            "candidate_multiplier": int(getattr(cfg, "artifact_rag_candidate_multiplier", 4)),
            "artifact_ids": artifact_ids,
            "artifact_tags": None,
            "analysis_signal": {},
            "position_ordered": False,
            "similarity_threshold": float(getattr(cfg, "artifact_rag_similarity_threshold", 0.0)),
        }
