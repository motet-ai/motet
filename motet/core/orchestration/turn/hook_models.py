"""
Motet - Turn Hook Contracts

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Declared data and result models for turn hooks. Single-command slots
    (conversation_analysis, memory_reset, context_prepare, finalize) and
    list slots (context_inject, after_finalize) resolve through the command
    registry. These models are the payloads the registry validates at
    resolve: a mismatch is a configuration bug, not a silent empty dict.

    ConversationAnalysisResult is observation-only. It does not modify
    messages, does not patch effective_context, and does not pick a turn
    mode. Extra fields are allowed so a later handoff can ride this seam.
    The same model is forwarded read-only to context_inject hooks and
    typed onto PrepareContextData.analysis_metadata.

Dependencies:
    - pydantic: validation and extra-field policy
    - motet.core.commands.base_command_data: BaseCommandData, MessageFieldMixin

Usage:
    from motet.core.orchestration.turn.hook_models import (
        ConversationAnalysisResult,
        TurnContextHookData,
        TurnContextHookResult,
        TurnAfterFinalizeData,
        TurnAfterFinalizeResult,
        parse_analysis_result,
    )

    analysis = parse_analysis_result(command_return)
    data = TurnContextHookData(messages=history, context=ctx, analysis=analysis)

Notes:
    - context_inject remains additive fan-in (system_messages + optional
      context_patch). It cannot replace history or redact output.
    - after_finalize is fail-soft at runtime; void or ack is enough.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from motet.core.commands.base_command_data import BaseCommandData, MessageFieldMixin


class ConversationAnalysisResult(BaseModel):
    """Declared observation-only result of the conversation_analysis slot.

    Extra fields are kept so a custom analysis command can attach later
    consumers (for example a suggested handoff) without a second hook.
    """

    model_config = ConfigDict(extra="allow")

    intent: Optional[str] = Field(
        default=None,
        description="Primary intent label when the analysis command classified one.",
    )
    intent_confidence: Optional[float] = Field(
        default=None,
        description="Confidence for intent, 0.0–1.0, when the command supplied one.",
    )
    tone: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Tone / emotion classification when that dimension ran.",
    )
    complexity: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Complexity classification when that dimension ran.",
    )
    context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Context-dependency classification when that dimension ran.",
    )
    rag: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Local artifact-RAG intent signal when the command inferred one.",
    )
    analysis_mode: Optional[str] = Field(
        default=None,
        description="How analysis ran (for example full, lightweight, or skipped).",
    )
    tool_requirements: Optional[str] = Field(
        default=None,
        description="Tool-need label from complexity when that dimension ran.",
    )
    pending_action_status: Optional[str] = Field(
        default=None,
        description="Pending-action status the analysis command recorded, if any.",
    )


class TurnContextHookData(MessageFieldMixin, BaseCommandData):
    """Declared input for each context_inject command."""

    messages: List[Any] = Field(
        default_factory=list,
        description="Current turn messages after analysis and pending-action inject.",
    )
    context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Effective turn context. Hooks may merge a patch; they cannot replace history.",
    )
    analysis: Optional[ConversationAnalysisResult] = Field(
        default=None,
        description=(
            "Read-only conversation_analysis result for this turn. "
            "None when the analysis slot is off or failed."
        ),
    )


class TurnContextHookResult(BaseModel):
    """Declared additive result of a context_inject command."""

    model_config = ConfigDict(extra="allow")

    system_messages: Optional[List[Any]] = Field(
        default=None,
        description="System strings or {content: ...} dicts to insert after leading system messages.",
    )
    system_prompt: Optional[str] = Field(
        default=None,
        description="Single system string used when system_messages is empty.",
    )
    context_patch: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Keys merged into effective_context with setdefault (does not overwrite).",
    )
    context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Accepted alias for context_patch.",
    )


class TurnAfterFinalizeData(MessageFieldMixin, BaseCommandData):
    """Declared input for each after_finalize command."""

    messages: List[Any] = Field(
        default_factory=list,
        description="Turn messages (role/content) after the assistant reply.",
    )
    assistant_response: str = Field(
        default="",
        description="Final assistant text for this turn.",
    )
    agent_id: str = Field(
        default="",
        description="Qualified agent id that produced the turn.",
    )
    usage: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Token usage for the turn when a priced call ran.",
    )
    cost_usd: Optional[float] = Field(
        default=None,
        description="Turn cost in USD when metering produced one.",
    )
    model: Optional[str] = Field(
        default=None,
        description="Provider/model id for the turn when known.",
    )
    context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Effective turn context at finalize time.",
    )


class TurnAfterFinalizeResult(BaseModel):
    """Optional ack from an after_finalize command. Void is also success."""

    model_config = ConfigDict(extra="allow")

    ok: bool = Field(
        default=True,
        description="Whether the export/side effect completed.",
    )


def parse_analysis_result(analysis_data: Any) -> Optional[ConversationAnalysisResult]:
    """Parse a conversation_analysis command return into the declared result.

    Accepts the declared model, a dict that already matches it, or the
    nested shape the default command still returns (intent.primary, metadata,
    and so on). Extra fields are kept. Returns None when the payload cannot
    be read as observation.
    """
    if analysis_data is None:
        return None
    if isinstance(analysis_data, ConversationAnalysisResult):
        return analysis_data
    if not isinstance(analysis_data, dict):
        return None

    intent_raw = analysis_data.get("intent")
    if isinstance(intent_raw, dict) or "metadata" in analysis_data or "complexity" in analysis_data:
        return _from_nested_analysis(analysis_data)

    try:
        return ConversationAnalysisResult.model_validate(analysis_data)
    except Exception:
        return None


def _from_nested_analysis(analysis_data: Dict[str, Any]) -> ConversationAnalysisResult:
    """Map the default conversation_analysis nested dict onto the declared model."""
    intent_data = analysis_data.get("intent")
    complexity_data = analysis_data.get("complexity")
    context_data = analysis_data.get("context")
    tone_data = analysis_data.get("tone")
    rag_data = analysis_data.get("rag")
    metadata = analysis_data.get("metadata")

    if not isinstance(intent_data, dict):
        intent_data = {}
    if not isinstance(complexity_data, dict):
        complexity_data = {}
    if not isinstance(context_data, dict):
        context_data = {}
    if not isinstance(tone_data, dict):
        tone_data = {}
    if not isinstance(rag_data, dict):
        rag_data = {}
    if not isinstance(metadata, dict):
        metadata = {}

    extras = {
        key: value
        for key, value in analysis_data.items()
        if key
        not in {
            "intent",
            "complexity",
            "context",
            "tone",
            "rag",
            "metadata",
            "reasoning",
        }
    }
    payload: Dict[str, Any] = {
        "intent": intent_data.get("primary") if intent_data else analysis_data.get("intent"),
        "intent_confidence": intent_data.get("confidence"),
        "tone": tone_data or None,
        "complexity": complexity_data or None,
        "context": context_data or None,
        "rag": rag_data or None,
        "analysis_mode": metadata.get("analysis_mode") or analysis_data.get("analysis_mode"),
        "tool_requirements": complexity_data.get("tool_requirements")
        or analysis_data.get("tool_requirements"),
        "pending_action_status": metadata.get("pending_action_status")
        or analysis_data.get("pending_action_status"),
        **extras,
    }
    return ConversationAnalysisResult.model_validate(payload)


def analysis_as_dict(analysis: Optional[ConversationAnalysisResult]) -> Dict[str, Any]:
    """Plain dict for callers that still index analysis fields by name."""
    if analysis is None:
        return {}
    return analysis.model_dump(exclude_none=False)


__all__ = [
    "ConversationAnalysisResult",
    "TurnAfterFinalizeData",
    "TurnAfterFinalizeResult",
    "TurnContextHookData",
    "TurnContextHookResult",
    "analysis_as_dict",
    "parse_analysis_result",
]
