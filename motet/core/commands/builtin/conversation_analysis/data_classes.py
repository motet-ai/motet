"""
Motet - Conversation Analysis Data Classes

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Pydantic data models for conversation analysis commands.
    Defines request/response structures for each analysis dimension.
    The *Result models double as JSON Schema sources for the
    output contracts the dimension commands attach to their inference
    calls, so every classification field is a Literal rather than a str.

Dependencies:
    - pydantic: Data validation and model definitions
    - typing: Type hints and annotations
    - motet.core.reasoning.constants: Canonical Complexity vocabulary
      literals, so result schemas emit enums the model must match

Usage:
    from motet.core.commands.builtin.conversation_analysis.data_classes import (
        ConversationAnalysisData,
        IntentAnalysisData
    )

Notes:
    - All data classes inherit from Pydantic BaseModel
    - Supports validation and serialization out of the box
    - Compatible with decorator-based command pattern
    - The Literals near the top are the analysis vocabulary and must stay in
      step with the option lists in each dimension's prompt; they become the
      enums in the output contract the model is constrained to
    - analysis_model / analysis_provider default to None. The turn hook
      pins the turn's model unless MOTET_ANALYSIS_MODEL is set; a direct
      call with both unset lets model_inference resolve the stack default.
"""

from typing import List, Literal, Optional, Any, Dict
from pydantic import BaseModel, Field
from motet.core.types import Message
from motet.core.commands.base_command_data import BaseCommandData
from motet.core.reasoning.constants import Complexity

# Analysis vocabulary. These mirror the option lists in each dimension's prompt
# and reach the model as JSON Schema enums, so a field left as a bare ``str``
# lets the model echo the prompt's "a|b|c" template back as a literal value.
Intent = Literal[
    "greeting",
    "question",
    "research",
    "brainstorm",
    "collaborate",
    "analyze",
    "plan",
    "compare",
    "task_request",
    "context_question",
]
Emotion = Literal[
    "frustrated", "excited", "confused", "confident", "neutral", "anxious", "satisfied"
]
Level = Literal["low", "medium", "high"]
CommunicationStyle = Literal["direct", "collaborative", "exploratory", "structured"]
Scope = Literal["narrow", "focused", "broad", "multi_domain"]
ToolRequirements = Literal["none", "basic", "advanced", "specialized"]
Expertise = Literal["beginner", "intermediate", "expert"]


# Full-analysis default: no LLM dimensions (ADR-0138).
#
# The turn gate that survives — trivial allowlist, pending-action override, RAG
# intent heuristic — is entirely local, so the default turn pays nothing here.
# Every LLM dimension lost its consumer: `intent.primary` / `confidence` were
# already dead (#258), `strategy_hint` no longer routes anything, `complexity`
# fed a pass-through that is gone, and `context` existed only to feed the
# clarification route, which the agent loop now covers from its system brief.
#
# All five remain available as opt-in dimensions for telemetry or a product
# feature, via `analysis_dimensions` or a `context_inject` command.
DEFAULT_ANALYSIS_DIMENSIONS: list[str] = []


class ConversationAnalysisData(BaseCommandData):
    """Main conversation analysis request data"""
    messages: List[Message] = Field(description="Messages to analyze")
    conversation_context: Optional[List[Message]] = Field(
        default=None,
        description="Conversation history for context"
    )
    pending_action: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            " routing hint from agent_turn's pending-action read: "
            '{"status": "fresh"|"stale"|"none", "reply": "confirm"|"decline"|"other"}. '
            "The marker is the single source of truth for pendingness; when the hint "
            "is omitted or status is \"none\", nothing is pending."
        )
    )
    analysis_dimensions: Optional[List[str]] = Field(
        default_factory=lambda: list(DEFAULT_ANALYSIS_DIMENSIONS),
        description=(
            "Dimensions to analyze. Default is none — the turn gate is local "
            ". intent, context, complexity, tone, and user_profile "
            "are all opt-in."
        ),
    )
    analysis_model: Optional[str] = Field(
        default=None,
        description=(
            "Model for analysis. The turn hook pins the turn's model unless "
            "MOTET_ANALYSIS_MODEL is set. Unset on a direct call lets "
            "model_inference resolve the stack default."
        )
    )
    analysis_provider: Optional[str] = Field(
        default=None,
        description=(
            "Provider for analysis. The turn hook pins the turn's provider "
            "unless MOTET_ANALYSIS_PROVIDER is set. Unset on a direct call "
            "lets model_inference resolve the stack default."
        )
    )


class IntentAnalysisData(BaseCommandData):
    """Intent analysis request data"""
    user_text: str = Field(description="User message text to analyze")
    conversation_context: Optional[List[Message]] = Field(
        default=None,
        description="Conversation history for context"
    )
    analysis_model: Optional[str] = Field(
        default=None,
        description=(
            "Model for analysis. The turn hook pins the turn's model unless "
            "MOTET_ANALYSIS_MODEL is set. Unset on a direct call lets "
            "model_inference resolve the stack default."
        )
    )
    analysis_provider: Optional[str] = Field(
        default=None,
        description=(
            "Provider for analysis. The turn hook pins the turn's provider "
            "unless MOTET_ANALYSIS_PROVIDER is set. Unset on a direct call "
            "lets model_inference resolve the stack default."
        )
    )


class ToneAnalysisData(BaseCommandData):
    """Tone analysis request data"""
    user_text: str = Field(description="User message text to analyze")
    conversation_context: Optional[List[Message]] = Field(
        default=None,
        description="Conversation history for context"
    )
    analysis_model: Optional[str] = Field(
        default=None,
        description=(
            "Model for analysis. The turn hook pins the turn's model unless "
            "MOTET_ANALYSIS_MODEL is set. Unset on a direct call lets "
            "model_inference resolve the stack default."
        )
    )
    analysis_provider: Optional[str] = Field(
        default=None,
        description=(
            "Provider for analysis. The turn hook pins the turn's provider "
            "unless MOTET_ANALYSIS_PROVIDER is set. Unset on a direct call "
            "lets model_inference resolve the stack default."
        )
    )


class ComplexityAnalysisData(BaseCommandData):
    """Complexity analysis request data"""
    user_text: str = Field(description="User message text to analyze")
    conversation_context: Optional[List[Message]] = Field(
        default=None,
        description="Conversation history for context"
    )
    analysis_model: Optional[str] = Field(
        default=None,
        description=(
            "Model for analysis. The turn hook pins the turn's model unless "
            "MOTET_ANALYSIS_MODEL is set. Unset on a direct call lets "
            "model_inference resolve the stack default."
        )
    )
    analysis_provider: Optional[str] = Field(
        default=None,
        description=(
            "Provider for analysis. The turn hook pins the turn's provider "
            "unless MOTET_ANALYSIS_PROVIDER is set. Unset on a direct call "
            "lets model_inference resolve the stack default."
        )
    )


class ContextAnalysisData(BaseCommandData):
    """Context analysis request data"""
    user_text: str = Field(description="User message text to analyze")
    conversation_context: Optional[List[Message]] = Field(
        default=None,
        description="Conversation history for context"
    )
    analysis_model: Optional[str] = Field(
        default=None,
        description=(
            "Model for analysis. The turn hook pins the turn's model unless "
            "MOTET_ANALYSIS_MODEL is set. Unset on a direct call lets "
            "model_inference resolve the stack default."
        )
    )
    analysis_provider: Optional[str] = Field(
        default=None,
        description=(
            "Provider for analysis. The turn hook pins the turn's provider "
            "unless MOTET_ANALYSIS_PROVIDER is set. Unset on a direct call "
            "lets model_inference resolve the stack default."
        )
    )


class UserProfileAnalysisData(BaseCommandData):
    """User profile analysis request data"""
    user_text: str = Field(description="User message text to analyze")
    conversation_context: Optional[List[Message]] = Field(
        default=None,
        description="Conversation history for context"
    )
    analysis_model: Optional[str] = Field(
        default=None,
        description=(
            "Model for analysis. The turn hook pins the turn's model unless "
            "MOTET_ANALYSIS_MODEL is set. Unset on a direct call lets "
            "model_inference resolve the stack default."
        )
    )
    analysis_provider: Optional[str] = Field(
        default=None,
        description=(
            "Provider for analysis. The turn hook pins the turn's provider "
            "unless MOTET_ANALYSIS_PROVIDER is set. Unset on a direct call "
            "lets model_inference resolve the stack default."
        )
    )


# Response models for structured output

class IntentAnalysisResult(BaseModel):
    """Intent classification results"""
    primary: Intent = Field(description="Primary intent classification")
    confidence: float = Field(description="Confidence score 0.0-1.0")


class ToneAnalysisResult(BaseModel):
    """Tone and emotional analysis results"""
    emotion: Emotion = Field(description="Dominant emotion in the message")
    urgency: Level = Field(description="Urgency level")
    satisfaction: Level = Field(description="Satisfaction level")
    communication_style: CommunicationStyle = Field(description="Communication style")
    confidence: float = Field(description="Confidence score 0.0-1.0")


class ComplexityAnalysisResult(BaseModel):
    """Task complexity assessment results"""
    level: Complexity = Field(description="Overall task complexity")
    estimated_turns: int = Field(description="Estimated conversation turns needed")
    scope: Scope = Field(description="Breadth of the request")
    tool_requirements: ToolRequirements = Field(description="Tool needs")
    expertise_needed: Expertise = Field(description="Expertise needed to answer")


class ContextAnalysisResult(BaseModel):
    """Context dependency analysis results"""
    needs_clarification: bool = Field(description="Whether clarification is needed")
    references_previous: bool = Field(description="Whether it references previous conversation")
    missing_info: List[str] = Field(description="List of missing information")
    resolved_references: Dict[str, str] = Field(description="Resolved pronoun references")


class UserProfileAnalysisResult(BaseModel):
    """Per-turn user profile classification"""
    current_expertise: Dict[str, Any] = Field(description="Current expertise indicators")
    current_communication: Dict[str, Any] = Field(description="Current communication preferences")
    current_context: Dict[str, Any] = Field(description="Current context indicators")

