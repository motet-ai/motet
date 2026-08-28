"""
Motet - Conversation Analysis Orchestrator

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Opt-in conversation analysis command for tone / profile / complexity on
    ``context_inject``. The trivial-message allowlist lives in
    ``trivial_message``; this module reuses those helpers so skip-analysis
    and the turn gate cannot drift. Coordinates remaining analysis
    dimensions with motet.join().

Dependencies:
    - structlog: Structured logging
    - typing: Type hints
    - Decorator command system
    - All dimension analysis commands

Usage:
    from motet.core.commands.builtin.conversation_analysis import conversation_analysis
    
    command = conversation_analysis(
        task_id="task-123",
        conversation_id="conv-456",
        data=ConversationAnalysisData(
            messages=[...],
            analysis_dimensions=["intent", "context"]
        )
    )
    result = motet.do(command)

    Notes:
    - Executes all dimensions in parallel using motet.join()
    - 3-5x faster than sequential execution (10-20s vs 30-60s)
    - Graceful degradation: one dimension failure doesn't block others
    - Better observability: track each dimension separately
    - Modular and testable: each dimension is independent
    - Lightweight intent hints must not reroute a named tool/workflow dispatch
      into a strategy that cannot dispatch (see _lightweight_intent_detection)
Notes:
    - Executes all dimensions in parallel using motet.join()
    - 3-5x faster than sequential execution (10-20s vs 30-60s)
    - Graceful degradation: one dimension failure doesn't block others
    - Better observability: track each dimension separately
    - Modular and testable: each dimension is independent
    - Lightweight intent hints must not reroute a named tool/workflow dispatch
      into a strategy that cannot dispatch (see _lightweight_intent_detection)
"""

import re

import structlog
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, List, Optional, Tuple
from motet import motet
from motet.core.commands.decorator import get_motet_context
from motet.core.workers.observers import EventPriority
from motet.core.conversations.pending_action import pending_action_block_reason
from motet.core.conversations.trivial_message import (
    is_trivial_message,
    last_user_message,
)
from motet.core.commands.builtin.conversation_analysis.data_classes import (
    DEFAULT_ANALYSIS_DIMENSIONS,
    ConversationAnalysisData,
)
from motet.core.commands.builtin.conversation_analysis.intent_analysis import intent_analysis, IntentAnalysisData
from motet.core.commands.builtin.conversation_analysis.tone_analysis import tone_analysis, ToneAnalysisData
from motet.core.commands.builtin.conversation_analysis.complexity_analysis import complexity_analysis, ComplexityAnalysisData
from motet.core.commands.builtin.conversation_analysis.context_analysis import context_analysis, ContextAnalysisData
from motet.core.commands.builtin.conversation_analysis.user_profile_analysis import user_profile_analysis, UserProfileAnalysisData

logger = structlog.get_logger(__name__)

_TEXT_TOKEN_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]*")

# Baseline tool-intent markers used by _lightweight_intent_detection for the
# intent hint only (never as a skip gate). This static tuple is the fallback
# floor; _tool_intent_pattern() unions it with the keywords of all currently
# registered tools so the hint tracks what is actually installed (e.g. an MCP
# server registering tools with "gmail"/"calendar" keywords extends the
# vocabulary automatically). Word-boundary matching is required: substring
# checks made "get" match "together" and "run" match "brunch".
_TOOL_INTENT_TERMS = (
    "weather", "search", "calculate", "get", "find", "lookup", "fetch",
    "send", "email", "schedule", "remind", "run", "execute", "create",
)

# Cache for the registry-derived pattern: (registered tool-name set, compiled
# regex). Rebuilt only when the set of registered tools changes (MCP tools
# register dynamically as services come online). Updated via atomic tuple
# swap; a concurrent race just recompiles the same pattern, so no lock needed.
_tool_intent_pattern_cache: Tuple[Optional[FrozenSet[str]], Optional["re.Pattern[str]"]] = (
    None,
    None,
)


def _tool_intent_pattern() -> "re.Pattern[str]":
    """
    Compile the tool-intent regex from registered tool keywords.

    Every RegisteredTool carries a ``keywords`` list (builtin tools declare
    them; MCP tools get them inferred at registration). The union of those
    keywords plus the static ``_TOOL_INTENT_TERMS`` floor forms the intent
    vocabulary. False positives are low-stakes: the pattern only sets the
    ``tool_usage`` intent hint in lightweight analysis and never gates the
    skip/direct-answer path.
    """
    global _tool_intent_pattern_cache

    try:
        from motet.core.tools.registry import registry as tool_registry
        items = tool_registry.list_items()
    except Exception as exc:
        # Registry unavailable (early startup, stripped-down test contexts):
        # fall back to the static floor rather than failing analysis.
        logger.warning(
            "tool_intent_registry_unavailable",
            operation="_tool_intent_pattern",
            error=str(exc),
            error_type=type(exc).__name__,
            note="falling back to static _TOOL_INTENT_TERMS",
        )
        items = {}

    names = frozenset(items.keys())
    cached_names, cached_pattern = _tool_intent_pattern_cache
    if cached_pattern is not None and names == cached_names:
        return cached_pattern

    terms = {t.lower() for t in _TOOL_INTENT_TERMS}
    for tool in items.values():
        for keyword in getattr(tool, "keywords", None) or []:
            normalized = str(keyword).strip().lower()
            # Single characters would match almost anything even with \b.
            if len(normalized) >= 2:
                terms.add(normalized)

    # Sorted for a deterministic pattern; escaped because keywords are free
    # text (may contain spaces, hyphens, or regex metacharacters).
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(t) for t in sorted(terms)) + r")\b"
    )
    _tool_intent_pattern_cache = (names, pattern)
    return pattern

_ARTIFACT_REFERENCE_TERMS = {
    "artifact",
    "artifacts",
    "attachment",
    "attachments",
    "doc",
    "docs",
    "document",
    "documents",
    "file",
    "files",
    "pdf",
    "report",
    "spreadsheet",
    "upload",
    "uploaded",
}
_ARTIFACT_ACTION_TERMS = {
    "analyze": "analyze",
    "answer": "question",
    "cite": "question",
    "compare": "compare",
    "extract": "extract",
    "find": "question",
    "summarize": "summary",
    "summary": "summary",
}
_PRINCIPAL_SCOPE_PATTERNS = (
    "my files",
    "my uploads",
    "my documents",
    "all my files",
    "all my uploads",
    "across my documents",
    "across my files",
)
_MOTET_SCOPE_PATTERNS = (
    "workspace documents",
    "workspace files",
    "team documents",
    "team files",
    "all motet artifacts",
    "all workspace artifacts",
)


def _infer_artifact_rag_intent(text: str) -> Dict[str, Any]:
    """Infer conservative artifact RAG intent signals for context policy."""

    text_lower = (text or "").lower()
    terms = set(_TEXT_TOKEN_RE.findall(text_lower))
    has_artifact_reference = bool(terms.intersection(_ARTIFACT_REFERENCE_TERMS))
    action = "question" if "?" in text_lower else "none"
    for marker, marker_action in _ARTIFACT_ACTION_TERMS.items():
        if marker in terms:
            action = marker_action
            break

    suggested_scope = "conversation"
    if any(pattern in text_lower for pattern in _MOTET_SCOPE_PATTERNS):
        suggested_scope = "motet"
    elif any(pattern in text_lower for pattern in _PRINCIPAL_SCOPE_PATTERNS):
        suggested_scope = "principal"

    needs_rag = has_artifact_reference or action in {"summary", "compare", "extract"}
    confidence = 0.8 if has_artifact_reference else 0.45 if needs_rag else 0.2
    return {
        "needs_rag": needs_rag,
        "artifact_intent": has_artifact_reference,
        "artifact_action": action,
        "suggested_scope": suggested_scope,
        "confidence": confidence,
        "source": "heuristic",
    }


def _should_analyze_conversation(
    messages: List[Any],
    pending_action: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Determine analysis mode for a turn.

    Modes:
    - skip: obvious/simple queries
    - lightweight: moderate single-turn queries
    - full: complex or multi-turn queries

    ``pending_action`` is the ADR-0121 routing hint from agent_turn's marker
    read ({"status", "reply"}). The marker is the single source of truth for
    pendingness — this function does no transcript reads or text heuristics
    of its own. A fresh or stale marker disables the trivial skip
    unconditionally, with dedicated reasons for the confirm/decline
    partition.
    """
    if not messages:
        return {"full_analysis": False, "lightweight": False, "reason": "no_messages"}

    last = last_user_message(messages)
    if last is None:
        return {"full_analysis": False, "lightweight": False, "reason": "no_user_message"}

    query = getattr(last, "content", "") or ""
    query_lower = query.lower().strip()
    word_count = len(query.split())
    message_count = len(messages)

    # Skip is allowlist-only. A pending marker disables it — an allowlisted
    # ack ("ok") answering a pending proposal is a confirmation, so it
    # re-enters lightweight analysis. The gate uses the same helpers.
    if is_trivial_message(last):
        block_reason = pending_action_block_reason(pending_action)
        if block_reason:
            return {"full_analysis": False, "lightweight": True, "reason": block_reason}
        return {"full_analysis": False, "lightweight": False, "reason": "simple_query"}

    if word_count <= 3:
        # Short but not a known trivial expression (tool request, continuation
        # instruction, unusual ack, multimodal...). Lightweight analysis is
        # LLM-free, so routing these onward costs nothing.
        return {"full_analysis": False, "lightweight": True, "reason": "short_query"}

    if message_count <= 2 and word_count <= 30:
        return {"full_analysis": False, "lightweight": True, "reason": "moderate_query"}

    if message_count > 6 or word_count > 50:
        return {"full_analysis": True, "lightweight": False, "reason": "complex_multi_turn"}

    complex_patterns = [
        "analyze", "evaluate", "compare", "explain", "step by step",
        "based on", "considering", "given that", "in the context of",
    ]
    if any(pattern in query_lower for pattern in complex_patterns):
        return {"full_analysis": True, "lightweight": False, "reason": "complex_pattern"}

    return {"full_analysis": False, "lightweight": True, "reason": "moderate_query"}


# Routing decisions are their own observable layer (skip/lightweight/full is a
# router in the semantic-routing sense): daily counters make precision and
# fallback-rate reviewable, and are the labeled-data substrate for a learned
# router later. 30 days retention is enough for trend review without a cleanup
# job.
_ROUTING_DECISION_KEY_PREFIX = "routing:analysis_decisions"
_ROUTING_DECISION_TTL_SECONDS = 30 * 24 * 3600


def _record_routing_decision(
    decision: Dict[str, Any], word_count: int, message_count: int
) -> None:
    """
    Record one analysis-routing decision: a structured log plus daily counters.

    Counters live in a Redis hash per UTC day
    (``routing:analysis_decisions:{YYYY-MM-DD}``) with one field per
    ``{mode}:{reason}`` pair and a ``total`` field, surfaced by the debug API
    (``GET /api/v1/debug/routing/stats``). Recording is strictly best-effort:
    routing itself must never fail because metrics could not be written.
    """
    mode = (
        "full"
        if decision.get("full_analysis")
        else "lightweight" if decision.get("lightweight") else "skip"
    )
    reason = str(decision.get("reason", "unknown"))
    logger.info(
        "conversation_routing_decision",
        mode=mode,
        reason=reason,
        word_count=word_count,
        message_count=message_count,
    )
    try:
        from motet.core.distributed.redis_manager import get_sync_redis_client

        redis = get_sync_redis_client("routing_metrics")
        date_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"{_ROUTING_DECISION_KEY_PREFIX}:{date_key}"
        pipe = redis.pipeline()
        pipe.hincrby(key, f"{mode}:{reason}", 1)
        pipe.hincrby(key, "total", 1)
        pipe.expire(key, _ROUTING_DECISION_TTL_SECONDS)
        pipe.execute()
    except Exception as exc:
        logger.warning(
            "routing_decision_counter_failed",
            operation="_record_routing_decision",
            mode=mode,
            reason=reason,
            error=str(exc),
            error_type=type(exc).__name__,
        )


# A canonical tool name (core.tool_call, expert-panel.recall_discussion,
# mcp.google_workspace.list_docs) or a workflow function name. The first
# segment needs two characters and the second three so ordinary prose
# abbreviations ("e.g.", "u.s.") do not read as a dispatch.
_EXPLICIT_DISPATCH_RE = re.compile(r"\b(?:[a-z][a-z0-9_-]+\.[a-z][a-z0-9_]{2,}|workflow_[a-z0-9][a-z0-9_.-]*)\b")

# Anchored at a word start so inflections still match (analysis, assessment,
# comparison) without the mid-word hits the tool vocabulary already guards
# against; "ideas?" rather than "idea\w*" keeps "ideal" out of exploration.
_ANALYTICAL_RE = re.compile(r"\b(?:analy[sz]\w*|evaluat\w*|assess\w*|compar\w*)\b")
_EXPLORATORY_RE = re.compile(r"\b(?:brainstorm\w*|ideas?|alternatives?|options?|pros and cons)\b")


def _pending_action_status(pending_action: Optional[Dict[str, Any]]) -> Optional[str]:
    """ADR-0121 status from the routing hint, or None when nothing is pending."""
    if not isinstance(pending_action, dict):
        return None
    status = pending_action.get("status")
    if status in ("fresh", "stale", "none"):
        return status
    return None


def _lightweight_intent_detection(messages: List[Any]) -> Dict[str, Any]:
    """Local intent labelling and RAG signals, without LLM calls.

    ADR-0138 removed the ``strategy_hint`` this used to compute. Choosing a
    reasoning strategy by matching "analyze" or "brainstorm" against the first
    user sentence is the same bet as the LLM hint it fed, just free: it commits
    before any tool result exists. The agent loop decides for itself.

    The ``intent`` label is kept for observability — it is what
    ``conversation_routing_decision`` reports — and the RAG signals are kept
    because ``RagContextProvider`` consumes them.
    """
    if not messages:
        return {}

    last = last_user_message(messages)
    if last is None:
        return {}

    query = getattr(last, "content", "") or ""
    query_lower = query.lower()

    intent = "general"

    if _tool_intent_pattern().search(query_lower):
        intent = "tool_usage"

    if not _EXPLICIT_DISPATCH_RE.search(query_lower):
        if _ANALYTICAL_RE.search(query_lower):
            intent = "analysis"

        if _EXPLORATORY_RE.search(query_lower):
            intent = "exploration"

    word_count = len(query.split())
    complexity_level = "moderate" if word_count > 30 else "simple"

    # Keep response shape compatible with full analysis output.
    return {
        "intent": {
            "primary": intent,
            "confidence": 0.7,
        },
        "tone": {
            "emotion": "neutral",
            "urgency": "medium",
            "satisfaction": "medium",
            "communication_style": "direct",
            "confidence": 0.5,
            "fallback_reason": "lightweight_analysis",
        },
        "complexity": {
            "level": complexity_level,
            "estimated_turns": 2 if complexity_level == "simple" else 3,
            "scope": "focused",
            "tool_requirements": "basic",
            "expertise_needed": "intermediate",
            "fallback_reason": "lightweight_analysis",
        },
        "context": {
            "needs_clarification": False,
            "references_previous": False,
            "missing_info": [],
            "resolved_references": {},
            "fallback_reason": "lightweight_analysis",
        },
        "rag": _infer_artifact_rag_intent(query),
        "reasoning": "Lightweight analysis completed",
        "metadata": {
            "analyzer_type": "lightweight",
            "analysis_mode": "lightweight",
        },
    }


@motet.command(
    description="Run multi-dimensional conversation analysis in parallel. Default dimensions are intent and context; tone, complexity, and user-profile are opt-in.",
    timeout_seconds=60, priority=EventPriority.HIGH)
def conversation_analysis(
    data: ConversationAnalysisData) -> Dict[str, Any]:
    """
    Orchestrate multi-dimensional conversation analysis with parallel execution.
    
    Benefits of parallel approach:
    - 3-5x faster than sequential (30-60s → 10-20s)
    - Individual dimension failures don't block others
    - Better observability (track each dimension separately)
    - More modular and testable
    - Can selectively enable/disable dimensions
    
    Args:
        data: ConversationAnalysisData with messages and configuration
        
    Returns:
        Aggregated analysis dict (decorator wraps ADR-0029).
    """
    motet = get_motet_context()
    
    logger.info(
        "conversation_analysis_started",
        num_messages=len(data.messages),
        dimensions=data.analysis_dimensions or DEFAULT_ANALYSIS_DIMENSIONS
    )
    
    # Validate messages
    if not data.messages:
        raise ValueError("No messages provided for analysis")

    latest_message = data.messages[-1]
    if latest_message.role != "user":
        raise ValueError("Latest message must be from user for analysis")
    
    user_text = latest_message.content

    # ADR-0059 Phase 2: choose full/lightweight/skip inside the command so all callers share behavior.
    needs_analysis = _should_analyze_conversation(
        data.messages, pending_action=data.pending_action
    )
    # Uniform routing observability: one log + daily counters per decision
    # (replaces the per-branch conversation_analysis_lightweight/_skipped logs).
    _record_routing_decision(
        needs_analysis,
        word_count=len(user_text.split()),
        message_count=len(data.messages),
    )
    if needs_analysis["lightweight"]:
        lightweight = _lightweight_intent_detection(data.messages)
        lightweight.setdefault("metadata", {})
        lightweight["metadata"].update(
            {
                "reason": needs_analysis.get("reason", "moderate_query"),
                "model": data.analysis_model,
                "message_length": len(user_text),
                "conversation_length": len(data.conversation_context or []),
                "analysis_dimensions": data.analysis_dimensions
                or DEFAULT_ANALYSIS_DIMENSIONS,
                "pending_action_status": _pending_action_status(data.pending_action),
            }
        )
        return lightweight

    if not needs_analysis["full_analysis"]:
        skipped = {
            "intent": {"primary": "general", "confidence": 0.6},
            "tone": {
                "emotion": "neutral",
                "urgency": "low",
                "satisfaction": "medium",
                "communication_style": "direct",
                "confidence": 0.5,
                "fallback_reason": "analysis_skipped",
            },
            "complexity": {
                "level": "simple",
                "estimated_turns": 1,
                "scope": "narrow",
                "tool_requirements": "none",
                "expertise_needed": "beginner",
                "fallback_reason": "analysis_skipped",
            },
            "context": {
                "needs_clarification": False,
                "references_previous": False,
                "missing_info": [],
                "resolved_references": {},
                "fallback_reason": "analysis_skipped",
            },
            "rag": _infer_artifact_rag_intent(user_text),
            "reasoning": "Analysis skipped for simple query",
            "metadata": {
                "analyzer_type": "none",
                "analysis_mode": "skipped",
                "reason": needs_analysis.get("reason", "simple_query"),
                "model": data.analysis_model,
                "message_length": len(user_text),
                "conversation_length": len(data.conversation_context or []),
                "analysis_dimensions": data.analysis_dimensions
                or DEFAULT_ANALYSIS_DIMENSIONS,
                "pending_action_status": _pending_action_status(data.pending_action),
            },
        }
        return skipped

    dimensions = data.analysis_dimensions or DEFAULT_ANALYSIS_DIMENSIONS
    
    # Build analysis commands based on requested dimensions
    # Use tuple format (function, data) for motet.join() - ADR-0030 pattern
    analysis_commands = []
    
    def _create_analysis_data(DataClass, user_text: str, conversation_context=None):
        """Helper to create analysis data with proper default handling."""
        kwargs = {
            "user_text": user_text,
            "conversation_context": conversation_context or data.conversation_context,
        }
        # Only pass analysis_model/provider if they're explicitly set (not None)
        # This allows Pydantic defaults to apply when fields are missing
        if data.analysis_model is not None:
            kwargs["analysis_model"] = data.analysis_model
        if data.analysis_provider is not None:
            kwargs["analysis_provider"] = data.analysis_provider
        return DataClass(**kwargs)
    
    if "intent" in dimensions:
        analysis_commands.append((
            "intent",
            (
                intent_analysis,
                _create_analysis_data(IntentAnalysisData, user_text)
            )
        ))
    
    if "tone" in dimensions:
        analysis_commands.append((
            "tone",
            (
                tone_analysis,
                _create_analysis_data(ToneAnalysisData, user_text)
            )
        ))
    
    if "complexity" in dimensions:
        analysis_commands.append((
            "complexity",
            (
                complexity_analysis,
                _create_analysis_data(ComplexityAnalysisData, user_text)
            )
        ))
    
    if "context" in dimensions:
        analysis_commands.append((
            "context",
            (
                context_analysis,
                _create_analysis_data(ContextAnalysisData, user_text)
            )
        ))
    
    if "user_profile" in dimensions:
        analysis_commands.append((
            "user_profile",
            (
                user_profile_analysis,
                _create_analysis_data(UserProfileAnalysisData, user_text)
            )
        ))
    
    logger.info(
        "conversation_analysis_parallel_execution",
        num_commands=len(analysis_commands),
        dimensions=[dim for dim, _ in analysis_commands]
    )
    
    # Execute all analysis commands in parallel
    command_list = [cmd for _, cmd in analysis_commands]
    results = motet.join(
        command_list,
        fail_fast=False,
    )
    
    aggregated_result = _aggregate_analysis_results(
        results,
        [dim for dim, _ in analysis_commands],
        user_text,
        data
    )
    
    logger.info(
        "conversation_analysis_complete",
        dimensions_completed=[k for k in aggregated_result.keys() if k not in ["reasoning", "metadata"]]
    )
    
    return aggregated_result


def _aggregate_analysis_results(
    results: List[Any],
    dimension_names: List[str],
    user_text: str,
    data: ConversationAnalysisData
) -> Dict[str, Any]:
    """Aggregate unwrapped results from ``motet.join`` (submission order)."""
    
    result = {}
    for index, dimension_name in enumerate(dimension_names):
        if index >= len(results):
            logger.warning(
                "dimension_result_not_found",
                dimension=dimension_name,
            )
            result[dimension_name] = _create_fallback_for_dimension(
                dimension_name,
                "Result not found",
            )
            continue
        dimension_result = results[index]
        if isinstance(dimension_result, dict) and dimension_result.get("_error"):
            error_msg = str(dimension_result.get("message") or "Unknown error")
            logger.warning(
                "dimension_analysis_failed",
                dimension=dimension_name,
                error=error_msg,
            )
            result[dimension_name] = _create_fallback_for_dimension(
                dimension_name,
                error_msg,
            )
        else:
            result[dimension_name] = dimension_result

    # Add metadata and reasoning
    result["rag"] = _infer_artifact_rag_intent(user_text)
    result["reasoning"] = "Parallel analysis completed"
    result["metadata"] = {
        "analyzer_type": "parallel_llm",
        "analysis_mode": "full",
        "model": data.analysis_model,
        "message_length": len(user_text),
        "conversation_length": len(data.conversation_context or []),
        "analysis_dimensions": dimension_names,
        "pending_action_status": _pending_action_status(data.pending_action),
    }
    
    return result


def _create_fallback_for_dimension(dimension_name: str, error_reason: str) -> Dict[str, Any]:
    """Create fallback data for a failed dimension"""
    
    fallbacks = {
        "intent": {
            "primary": "task_request",
            "confidence": 0.3,
            "fallback_reason": error_reason
        },
        "tone": {
            "emotion": "neutral",
            "urgency": "medium",
            "satisfaction": "medium",
            "communication_style": "direct",
            "confidence": 0.3,
            "fallback_reason": error_reason
        },
        "complexity": {
            "level": "medium",
            "estimated_turns": 3,
            "scope": "focused",
            "tool_requirements": "basic",
            "expertise_needed": "intermediate",
            "fallback_reason": error_reason
        },
        "context": {
            "needs_clarification": False,
            "references_previous": False,
            "missing_info": [],
            "resolved_references": {},
            "fallback_reason": error_reason
        },
        "user_profile": {
            "current_expertise": {
                "level": "intermediate",
                "domain": "general",
                "confidence": 0.3,
                "evidence": ["fallback"]
            },
            "current_communication": {
                "detail_preference": "moderate",
                "style": "direct",
                "urgency": "medium"
            },
            "current_context": {
                "role_mode": "individual",
                "decision_scope": "personal",
                "time_pressure": "moderate"
            },
            "fallback_reason": error_reason
        }
    }
    
    return fallbacks.get(dimension_name, {"error": error_reason})

