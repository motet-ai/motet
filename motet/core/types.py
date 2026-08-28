"""
Motet - Type Definitions

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-28

Description:
    Core type definitions for the Motet distributed framework.
    Provides message, response, tool, and capability type definitions.

    additions:
    - `Message.content_parts`: Provider-agnostic multimodal inputs (text + artifact references like images).
    - `RequestContext`: Request-scoped identity/isolation/budgets used by providers/renderers (kept separate
    from `model_settings`, which is reserved for model tuning like temperature/max_tokens).

    additions:
    - `ReasoningEffort` / `REASONING_EFFORT_LADDER` / `normalize_reasoning_effort`: the canonical
    reasoning-effort vocabulary (`low < medium < high < xhigh < max`) shared by orchestration and
    every provider adapter, plus the clamping used to map it onto provider-supported subsets.

Dependencies:
    - datetime: Time and date handling
    - enum: Enumeration types
    - typing: Type hints and protocols
    - pydantic: Data validation and serialization

Usage:
    from motet.core.types import Message, Response, Tool, AgentCapability, RequestContext
    
    # Create message
    message = Message(role="user", content="Hello")

    # Provide request-scoped context for secure artifact access (multimodal, tools, etc.)
    ctx = RequestContext(tenant_id="default", principal_id="user-123", enable_multimodal=True)
    
    # Create tool
    tool = Tool(name="calculator", description="Performs calculations")

    rows = serialize_memory_items([MemoryItem(id="m1", type="note", content="hello")])

    # Clamp a canonical reasoning effort onto what a provider actually accepts
    effort = normalize_reasoning_effort("max", supported=("low", "medium", "high", "xhigh"))  # -> "xhigh"

Notes:
    - Provides core type definitions
    - Includes pydantic models for validation
    - Supports protocol definitions
    - Integrates across the entire framework
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Protocol, Callable, Tuple, TypeVar, Union, Literal, Iterator, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Role(str, Enum):
    system = "system"
    user = "user"
    assistant = "assistant"
    tool = "tool"


class Message(BaseModel):
    # Pydantic core honors this key; it is not in the ConfigDict TypedDict stub (use cast for the checker).
    model_config = cast(ConfigDict, {"ser_json_exclude_none": True})  # Exclude None values when serializing
    
    role: str = Field(description="Message author role: system|user|assistant|tool")
    content: str
    agent_id: Optional[str] = Field(
        default=None,
        description=(
            "Qualified registry id of the agent that produced this message "
            "(assistant/tool turns). Set on persisted canonical transcript messages when known."
        ),
    )
    parent_agent_id: Optional[str] = Field(
        default=None,
        description=(
            "Qualified registry id of the agent that started this loop when the author is nested "
            "(for example a core.spawn_agents child). Omitted on the conversation-primary agent."
        ),
    )
    # ADR-0062: Provider-agnostic multimodal content parts
    #
    # Precedence rule (required):
    # - If content_parts is present and non-empty, renderers MUST use content_parts as model input
    #   and SHOULD treat `content` as a text-only fallback/summary for legacy consumers/logging.
    # - Avoid double-injection: do not duplicate injected attachment text in both content and parts.
    content_parts: Optional[List["ContentPart"]] = Field(
        default=None,
        description="Optional provider-agnostic multimodal parts. Takes precedence over `content` when present.",
    )
    name: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    tool_calls_canonical: Optional[List["ToolCallRequest"]] = Field(
        default=None,
        description=(
            'Assistant tool-call proposals as ToolCallRequest. Leftover ChatCompletions ``tool_calls`` keys are discarded, not lifted (issue #225).'
        ),
    )
    tool_call_id: Optional[str] = None  # For tool messages (response to a tool call)

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_tool_calls(cls, data: Any) -> Any:
        """Issue #225: leftover ``tool_calls`` keys are discarded, not lifted."""
        if isinstance(data, dict) and "tool_calls" in data:
            data = dict(data)
            data.pop("tool_calls", None)
        return data
    attachments: Optional[List[Dict[str, Any]]] = Field(default=None, description="List of UploadAttachment objects")
    # ADR-0064 R10: Canonical reasoning storage for multi-turn replay
    reasoning_content: Optional[str] = Field(
        default=None,
        description="Reasoning text for this assistant turn; required for replay by providers that expect it (e.g. Moonshot).",
    )
    reasoning_blocks: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Opaque reasoning blocks for exact replay (e.g. Anthropic thinking blocks with signature).",
    )


class TextPart(BaseModel):
    """Provider-agnostic text content part."""

    type: Literal["text"] = "text"
    text: str = Field(description="Text content for the model")


class MediaPart(BaseModel):
    """
    Provider-agnostic media content part.

    This generalizes multimodal parts beyond images (audio/video/files) while remaining compatible with
    the artifact-based injection model.

    Notes:
    - Prefer `artifact_id` for internal artifact storage/retrieval (tenant/principal scoped).
    - `url`/`base64_data` are supported for interoperability and local providers, but may be policy-restricted.
    - Provider-specific knobs (e.g., image detail) are optional and only interpreted when applicable.
"""

    type: Literal["media"] = "media"
    media_type: Literal["image", "audio", "video", "file"] = Field(
        description="High-level media category for rendering (image|audio|video|file)."
    )
    mime_type: str = Field(description="MIME type for the media (e.g. image/png, audio/wav).")
    artifact_id: Optional[str] = Field(default=None, description="Artifact ID containing the media bytes.")
    url: Optional[str] = Field(default=None, description="Remote URL for the media (provider-dependent).")
    base64_data: Optional[str] = Field(default=None, description="Inline base64-encoded media (provider-dependent).")
    # Rendering hint (primarily relevant for images today)
    detail: Optional[Literal["low", "high", "auto"]] = Field(
        default=None,
        description="Optional fidelity/cost hint. Intended for images; providers may ignore for other media.",
    )


# Discriminated union-like type alias (Pydantic v2 can validate unions by matching fields).
ContentPart = Union[TextPart, MediaPart]


# ================================================================================================
# Request Context (Identity + Isolation + Budgets)
# ================================================================================================

class RequestContext(BaseModel):
    """
    Request-scoped context for model calls.

    This is intentionally separate from model settings:
    - model_settings: temperature, max_tokens, json mode, etc.
    - request_context: identity/isolation/budgets required to safely fetch artifacts and render multimodal inputs.

    Tracing fields are optional and used for observability/debugging.
    """

    # Identity / tenancy
    tenant_id: Optional[str] = Field(default=None, description="Tenant identifier for multi-tenancy isolation")
    principal_id: Optional[str] = Field(default=None, description="Principal/user identifier for access control")
    motet_id: Optional[str] = Field(default=None, description="Motet/environment identifier (deployment/env)")
    conversation_id: Optional[str] = Field(
        default=None,
        description=(
            "Conversation/session identifier. Used for observability and for provider cache routing "
            "(e.g. xAI prompt_cache_key)."
        ),
    )

    # Tracing / observability (optional, for debugging and log correlation)
    task_id: Optional[str] = Field(default=None, description="Current task identifier for tracing")
    command_id: Optional[str] = Field(default=None, description="Current command identifier for tracing")
    parent_command_id: Optional[str] = Field(default=None, description="Parent command ID for chained commands")
    trace_id: Optional[str] = Field(default=None, description="Distributed trace ID for cross-service correlation")

    # Routing / policy (ADR-0064)
    model_profile_name: Optional[str] = Field(
        default=None,
        description=(
            'Optional tenant-scoped ModelProfile name used for adapter routing and policy overrides. When set, model_inference/model_stream will prefer this over the global Config.model_profile_name.'
        ),
    )

    # Isolation / security (mirrors DistributedCommandContext semantics, but model-layer friendly)
    tenant_isolation_required: bool = Field(default=True, description="Require tenant isolation for artifact/tool access")
    worker_security_level: str = Field(default="standard", description="Worker security level (standard|high|isolated)")

    # Multimodal rendering policy/budgets (ADR-0062)
    enable_multimodal: bool = Field(default=False, description="Enable multimodal rendering when content_parts are present")
    max_images: int = Field(default=8, ge=0, description="Max images to include in a single model request")
    max_image_bytes: int = Field(default=20 * 1024 * 1024, ge=0, description="Max bytes per image to inline for vision models")


class Principal(BaseModel):
    id: str
    roles: List[str] = Field(default_factory=list)
    tenant_id: Optional[str] = None
    motet_id: Optional[str] = None  # Motet/environment identifier for multi-environment deployments
    display_name: Optional[str] = None
    email: Optional[str] = None
    claims: Dict[str, Any] = Field(default_factory=dict)


class Tenant(BaseModel):
    id: str
    name: Optional[str] = None


class Response(BaseModel):
    content: str
    model: Optional[str] = None
    stop_reason: Optional[str] = None
    usage_tokens_input: Optional[int] = None
    usage_tokens_output: Optional[int] = None
    raw: Optional[Dict[str, Any]] = None


# ================================================================================================
# Canonical LLM Protocol Types (ADR-0064)
# ================================================================================================

class StopReason(str, Enum):
    """Canonical stop reasons."""

    NATURAL_STOP = "natural_stop"
    LENGTH_LIMIT = "length_limit"
    TOOL_CALLS = "tool_calls"
    STOP_SEQUENCE = "stop_sequence"
    SAFETY_FILTER = "safety_filter"
    ERROR = "error"


class FallbackPolicy(str, Enum):
    """Structured output fallback policy."""

    RETRY_WITH_STRONGER_INSTRUCTIONS = "retry_with_stronger_instructions"
    REPAIR_AND_VALIDATE = "repair_and_validate"
    FAIL = "fail"


# Canonical reasoning-effort vocabulary (ADR-0064), ordered cheapest to deepest.
#
# Providers accept different subsets of this ladder, so adapters own the mapping
# onto provider values rather than passing the canonical value through blindly.
# Verified against live APIs / provider docs (2026-07-27):
#   anthropic  low|medium|high|xhigh|max   (Opus 5 caps at high when thinking is disabled)
#   openai     low|medium|high|xhigh       (+ max on gpt-5.6 Responses only; else clamp to xhigh)
#   xai        low|medium|high|xhigh       (no max)
#   meta       minimal|low|medium|high|xhigh  (no none/max; thinking-off → minimal)
#   deepseek   high|max                    (low/medium collapse up to high)
#   moonshot   max only on K3
ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]

REASONING_EFFORT_LADDER: Tuple[ReasoningEffort, ...] = ("low", "medium", "high", "xhigh", "max")


def normalize_reasoning_effort(
    value: Any,
    *,
    default: ReasoningEffort = "medium",
    supported: Optional[Iterable[str]] = None,
) -> ReasoningEffort:
    """
    Coerce an arbitrary value to a canonical effort, optionally clamped to a subset.

    Unrecognized values fall back to ``default`` rather than raising, because effort
    reaches adapters from unvalidated request-context overrides. When ``supported``
    is given, the result is the highest supported rung at or below the request, so a
    provider that lacks the top of the ladder degrades instead of erroring.
    """
    effort = value.strip().lower() if isinstance(value, str) else ""
    if effort not in REASONING_EFFORT_LADDER:
        effort = default
    if supported is None:
        return cast(ReasoningEffort, effort)

    allowed = [rung for rung in REASONING_EFFORT_LADDER if rung in set(supported)]
    if not allowed:
        return cast(ReasoningEffort, effort)
    if effort in allowed:
        return cast(ReasoningEffort, effort)
    requested_index = REASONING_EFFORT_LADDER.index(cast(ReasoningEffort, effort))
    at_or_below = [r for r in allowed if REASONING_EFFORT_LADDER.index(r) <= requested_index]
    return cast(ReasoningEffort, at_or_below[-1] if at_or_below else allowed[0])


class OutputContract(BaseModel):
    """Canonical structured output contract."""

    format: Literal["text", "json"] = Field(default="text", description="Requested output format.")
    json_schema: Optional[Dict[str, Any]] = Field(default=None, description="JSON Schema when format=json.")
    strict: bool = Field(default=False, description="Request strict schema enforcement when supported.")
    fallback_policy: FallbackPolicy = Field(
        default=FallbackPolicy.REPAIR_AND_VALIDATE,
        description="Fallback behavior on schema validation failure.",
    )


class ToolCallRequest(BaseModel):
    """Model requests tool execution (canonical)."""

    call_id: str = Field(..., description="Stable correlation ID for the tool call.")
    tool_name: str = Field(..., description="Tool name (registry name or namespaced provider built-in).")
    arguments_json: str = Field(..., description="Raw JSON string of tool arguments.")
    arguments: Optional[Dict[str, Any]] = Field(default=None, description="Parsed arguments (best-effort).")
    arguments_artifact_id: Optional[str] = Field(
        default=None,
        description=(
            'When set, arguments_json in storage is an offload preview; hydrate the full unmodified JSON from ArtifactStore before provider replay.'
        ),
    )
    tool_call_group_id: Optional[str] = Field(
        default=None,
        description="Optional grouping ID for batched/parallel tool calls (best-effort; provider-dependent).",
    )
    tool_call_index: Optional[int] = Field(
        default=None,
        description="Optional index within a group for stable ordering (best-effort; provider-dependent).",
    )
    kind: Optional[str] = Field(
        default=None,
        description="Tool call kind: None for normal, 'provider' for provider-executed builtins.",
    )
    thought_signature: Optional[str] = Field(
        default=None,
        description=(
            "Provider thought signature bound to this call (e.g. Gemini thought_signature, "
            "base64url-encoded); must be replayed with the function call on later turns."
        ),
    )


Message.model_rebuild()


class ToolCallResult(BaseModel):
    """System provides tool execution result back to the model (canonical)."""

    call_id: str = Field(..., description="Must match the request call_id.")
    tool_name: str = Field(..., description="Tool name (must match the tool call request).")
    output: Any = Field(..., description="JSON-serializable tool output (or string).")
    is_error: bool = Field(default=False, description="Whether the tool execution failed.")
    error_type: Optional[str] = Field(default=None, description="Short error type string when is_error=true.")
    error_message: Optional[str] = Field(default=None, description="Human-readable error message when is_error=true.")
    tool_call_group_id: Optional[str] = Field(
        default=None,
        description="Optional grouping ID for batched/parallel tool calls (best-effort; provider-dependent).",
    )
    tool_call_index: Optional[int] = Field(
        default=None,
        description="Optional index within a group for stable ordering (best-effort; provider-dependent).",
    )


class CanonicalToolSchema(BaseModel):
    """Canonical tool schema (JSON Schema) that exporters map to provider formats."""

    name: str = Field(..., description="Tool name.")
    description: str = Field(default="", description="Tool description.")
    json_schema: Dict[str, Any] = Field(..., description="JSON Schema describing tool arguments.")
    strict: bool = Field(default=False, description="Hint: strict schema adherence preferred when supported.")


def tool_schema_name(schema: Any) -> str:
    """Read the tool name off a CanonicalToolSchema or canonical-like dict.

    Accepts:
    - ``CanonicalToolSchema`` (ADR-0064) — reads ``.name``
    - canonical dict — reads ``["name"]``

    Provider ``function.name`` dicts are not a schema shape (ADR-0137).

    Returns ``""`` when no name can be read, so callers can filter with a plain
    truthiness check. Callers wanting a display placeholder should apply their own
    (e.g. ``tool_schema_name(s) or "unnamed"``).

    The ``isinstance`` guards are deliberate: a bare ``getattr`` + ``str()`` turns a
    ``None`` name into the literal ``"None"`` and a ``Mock`` into its repr, both of
    which read as valid tool names downstream.
    """
    name = getattr(schema, "name", None)
    if isinstance(name, str):
        return name.strip()
    if isinstance(schema, dict) and isinstance(schema.get("name"), str):
        return schema["name"].strip()
    return ""


class LLMUsage(BaseModel):
    """
    Canonical usage envelope for LLM inference.

    This defines the minimal usage metrics we track across all providers.
    All fields are optional since not all providers report all metrics.

    Minimal Required Fields (adapters SHOULD populate when available):
    - prompt_tokens: Input/prompt token count
    - output_tokens: Output/completion token count

    Standard Optional Fields (populate when provider supports):
    - total_tokens: Sum of prompt + output (convenience)
    - cache_read_tokens: Tokens read from cache (Anthropic, future OpenAI)
    - cache_creation_tokens: Tokens written to cache
    - reasoning_tokens: Internal reasoning tokens (OpenAI o1/o3, Anthropic thinking)
    - tool_time_ms: Time spent on tool execution (orchestration-level)

    Provider-Specific Fields:
    - provider_metadata: Dict for any provider-specific metrics not in canonical fields
"""

    # Core token counts (minimal required)
    prompt_tokens: Optional[int] = Field(default=None, description="Prompt/input tokens (if available).")
    output_tokens: Optional[int] = Field(default=None, description="Output tokens (if available).")
    total_tokens: Optional[int] = Field(default=None, description="Total tokens (if available).")

    # Cache metrics (Anthropic prompt caching, future OpenAI)
    cache_read_tokens: Optional[int] = Field(default=None, description="Tokens read from prompt cache.")
    cache_creation_tokens: Optional[int] = Field(default=None, description="Tokens written to prompt cache.")

    # Reasoning/thinking metrics (OpenAI o1/o3, Anthropic extended thinking)
    reasoning_tokens: Optional[int] = Field(default=None, description="Internal reasoning/thinking tokens.")

    # Tool execution timing (orchestration-level, not provider-reported)
    tool_time_ms: Optional[int] = Field(
        default=None,
        description="Time spent on tool execution in milliseconds (orchestration-tracked, not provider-reported).",
    )

    # Provider-specific spillover
    provider_metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Provider-specific usage metadata not covered by canonical fields.",
    )


class CitationSpan(BaseModel):
    """Character span in output_text that a citation supports (best-effort, provider-dependent)."""

    start: int = Field(..., description="Start character offset in the final rendered output_text.")
    end: int = Field(..., description="End character offset in the final rendered output_text.")


class Citation(BaseModel):
    """Provider-agnostic citation/annotation envelope (optional; provider-dependent)."""

    source_type: Literal["web", "document", "tool", "other"] = Field(
        ..., description="High-level citation source category."
    )
    title: Optional[str] = Field(default=None, description="Human-friendly source title (if available).")
    url: Optional[str] = Field(default=None, description="Source URL (if available).")
    source_id: Optional[str] = Field(default=None, description="Provider/source identifier (file id, doc id, etc.).")
    snippets: Optional[List[str]] = Field(
        default=None, description="Short supporting excerpts/snippets (if available)."
    )
    spans: Optional[List[CitationSpan]] = Field(
        default=None, description="Optional character spans in output_text that this citation supports."
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default=None, description="Provider-specific citation fields (kept out of prompts)."
    )


class TextDeltaEvent(BaseModel):
    type: Literal["text_delta"] = "text_delta"
    text: str = Field(..., description="Incremental text delta.")


class ToolCallDeltaEvent(BaseModel):
    type: Literal["tool_call_delta"] = "tool_call_delta"
    call_id: str = Field(..., description="Correlation ID for the tool call.")
    tool_name: Optional[str] = Field(default=None, description="Tool name (may be omitted in delta events).")
    arguments_delta: Optional[str] = Field(default=None, description="Incremental arguments JSON delta (provider-dependent).")


class ToolCallCompleteEvent(BaseModel):
    type: Literal["tool_call_complete"] = "tool_call_complete"
    call_id: str = Field(..., description="Correlation ID for the tool call.")
    tool_name: str = Field(..., description="Tool name.")
    arguments_json: str = Field(..., description="Completed raw JSON arguments string.")
    kind: Optional[str] = Field(
        default=None,
        description="Tool call kind: None for normal, 'provider' for provider-executed builtins.",
    )
    thought_signature: Optional[str] = Field(
        default=None,
        description=(
            "Provider thought signature bound to this call (e.g. Gemini thought_signature, "
            "base64url-encoded); must be replayed with the function call on later turns."
        ),
    )


class ToolUseEvent(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    kind: Literal["provider", "motet", "mcp", "workflow"] = Field(
        ..., description="Origin/type of tool invocation."
    )
    tool_name: str = Field(..., description="Canonical tool name.")
    tool_call_id: Optional[str] = Field(default=None, description="Tool call correlation ID (if available).")
    status: Optional[str] = Field(default=None, description="Tool status if available (started/success/error).")
    metadata: Optional[Dict[str, Any]] = Field(
        default=None, description="Raw event details for observability."
    )


class StopEvent(BaseModel):
    type: Literal["stop"] = "stop"
    reason: StopReason = Field(..., description="Canonical stop reason.")


class UsageEvent(BaseModel):
    type: Literal["usage"] = "usage"
    usage: LLMUsage


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    error_type: str = Field(..., description="Short error type identifier.")
    message: str = Field(..., description="Error message.")


class CitationsEvent(BaseModel):
    type: Literal["citations"] = "citations"
    citations: List[Citation] = Field(..., description="Citations/annotations for the response (best-effort).")


class ThinkingEvent(BaseModel):
    """
    Canonical event for model reasoning/thinking traces.

    Some providers expose internal reasoning before the final response:
    - Anthropic: 'thinking' content blocks (beta)
    - OpenAI: reasoning tokens in o1/o3 models
    - Gemini: thinking models (2.5+)

    This event allows unified streaming and logging of reasoning traces
    without provider-specific handling in orchestration.
"""

    type: Literal["thinking"] = "thinking"
    text: str = Field(..., description="Incremental or complete reasoning text.")
    is_complete: bool = Field(
        default=False,
        description="True if this is the final/complete thinking block (vs streaming delta).",
    )
    # ADR-0064 R10: Opaque provider reasoning payload for stateless multi-turn replay
    # (e.g. OpenAI Responses encrypted reasoning items). Attached on the final event
    # (is_complete=True) so orchestration can persist it on Message.reasoning_blocks.
    blocks: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Opaque provider reasoning blocks for multi-turn replay (adapter-specific; not user-visible).",
    )


LLMStreamEvent = Union[
    TextDeltaEvent,
    ToolCallDeltaEvent,
    ToolCallCompleteEvent,
    ToolUseEvent,
    CitationsEvent,
    ThinkingEvent,
    StopEvent,
    UsageEvent,
    ErrorEvent,
]


OutputItem = Union[TextPart, MediaPart, ToolCallRequest]
TranscriptItem = Union[Message, ToolCallRequest, ToolCallResult]


class SkillRef(BaseModel):
    """
    Canonical record for one skill applied on a turn.

    Populated by orchestration for observability; adapters treat as opaque until Path B.
"""

    model_config = cast(ConfigDict, {"ser_json_exclude_none": True})

    skill_id: str = Field(..., description="Canonical id, typically {bundle_id}.{skill_slug}.")
    name: Optional[str] = Field(default=None, description="SKILL.md frontmatter name (slug).")
    bundle_id: Optional[str] = Field(default=None, description="Source bundle manifest name when from a bundle.")
    bundle_version: Optional[str] = Field(
        default=None,
        description="Deploy version / content fingerprint for the bundle install on this worker.",
    )
    content_fingerprint: Optional[str] = Field(
        default=None,
        description="Hash of injected instruction material; not sent to the LLM as a field.",
    )
    source: Optional[str] = Field(default=None, description="Discovery source, e.g. bundle | global | core.")
    path_b: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Reserved for provider-native skill metadata under Path B.",
    )


class LLMRequest(BaseModel):
    """Provider-agnostic request envelope for adapters."""

    messages: List[Message] = Field(..., description="Canonical conversation messages.")
    tools: Optional[List[CanonicalToolSchema]] = Field(default=None, description="Canonical tool schemas.")
    output_contract: Optional[OutputContract] = Field(default=None, description="Optional structured output contract.")
    model_settings: Optional[Dict[str, Any]] = Field(default=None, description="Provider/model tuning params.")
    request_context: Optional[RequestContext] = Field(default=None, description="Request-scoped identity/isolation/budgets.")
    skill_refs: Optional[List[SkillRef]] = Field(
        default=None,
        description="Skills orchestration applied this call (observability only; adapters do not reinterpret).",
    )


class LLMResponse(BaseModel):
    """Provider-agnostic response envelope for adapters."""

    output_text: Optional[str] = Field(default=None, description="Convenience flattened text output (best-effort).")
    output_items: List[OutputItem] = Field(default_factory=list, description="Canonical output items.")
    citations: Optional[List[Citation]] = Field(default=None, description="Optional citations/annotations (best-effort).")
    stop_reason: StopReason = Field(..., description="Canonical stop reason.")
    usage: Optional[LLMUsage] = Field(default=None, description="Best-effort usage envelope.")
    raw_provider_metadata: Optional[Dict[str, Any]] = Field(default=None, description="Provider-specific metadata for debugging.")
    # ADR-0064 R10: Reasoning for persistence and multi-turn replay
    reasoning_content: Optional[str] = Field(default=None, description="Reasoning text from this turn (e.g. Moonshot).")
    reasoning_blocks: Optional[List[Dict[str, Any]]] = Field(default=None, description="Opaque reasoning blocks (e.g. Anthropic).")


# ================================================================================================
# Image Generation (ADR-0113)
# ================================================================================================

class ImageGenerationRequest(BaseModel):
    """
    Provider-agnostic image-generation request.

    This is a deliberately separate call shape from LLMRequest: image generation is a
    text(+image)->image operation, not a chat completion. Adapters translate this to the
    provider's images endpoint (e.g. OpenAI /v1/images, xAI /v1/images, Gemini/Imagen).
"""

    prompt: str = Field(..., description="Text prompt describing the image to generate.")
    n: int = Field(default=1, ge=1, le=10, description="Number of images to generate.")
    size: Optional[str] = Field(
        default=None,
        description="Requested size as WIDTHxHEIGHT or 'auto' (provider-dependent, e.g. '1024x1024').",
    )
    quality: Optional[str] = Field(default=None, description="Provider-specific quality hint (e.g. 'high').")
    background: Optional[str] = Field(default=None, description="Provider-specific background hint (e.g. 'transparent').")
    input_images: Optional[List[MediaPart]] = Field(
        default=None,
        description="Optional source images for edit/variation operations.",
    )
    model_settings: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Provider/model tuning params (provider, model_name, response_format, etc.).",
    )
    request_context: Optional[RequestContext] = Field(
        default=None,
        description="Request-scoped identity/isolation used to fetch input-image artifacts.",
    )


class GeneratedImage(BaseModel):
    """A single generated image returned by an adapter."""

    mime_type: str = Field(default="image/png", description="MIME type of the generated image bytes.")
    base64_data: Optional[str] = Field(default=None, description="Base64-encoded image bytes (no data-URI prefix).")
    url: Optional[str] = Field(default=None, description="Temporary URL to the image (provider-dependent).")
    revised_prompt: Optional[str] = Field(default=None, description="Provider-revised prompt, when supplied.")


class ImageGenerationResponse(BaseModel):
    """Provider-agnostic image-generation response envelope."""

    images: List[GeneratedImage] = Field(default_factory=list, description="Generated images (bytes or URLs).")
    model: Optional[str] = Field(default=None, description="Resolved provider model id used for generation.")
    usage: Optional[LLMUsage] = Field(default=None, description="Best-effort usage envelope (provider-dependent).")
    raw_provider_metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Provider-specific metadata for debugging.",
    )


class Tool(BaseModel):
    name: str
    description: str = ""
    parameters: Dict[str, Any] = Field(default_factory=dict)


class MemoryItem(BaseModel):
    """
    Memory item with multi-motet and multi-tenant isolation.

    Supports hierarchical scoping for proper data isolation across:
    - Multiple motet deployments (production, staging, customer-specific)
    - Multiple tenants within a motet
    - Multiple principals (users) within a tenant
    - Multiple conversations/tasks within a session
"""
    id: str
    type: str
    content: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    tags: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    
    # Multi-motet/multi-tenant isolation (ADR-0027)
    motet_id: Optional[str] = Field(
        default=None, 
        description="Motet deployment ID (production, staging, customer_xyz)"
    )
    tenant_id: Optional[str] = Field(
        default=None, 
        description="Tenant/organization ID for multi-tenancy isolation"
    )
    principal_id: Optional[str] = Field(
        default=None, 
        description="User/principal ID for user-scoped memories"
    )
    conversation_id: Optional[str] = Field(
        default=None, 
        description="Conversation/session ID for conversation-scoped memories"
    )
    
    # Memory scoping (ADR-0027)
    scope_type: Optional[str] = Field(
        default="working", 
        description="Memory scope: working, conversation, task, principal, collective"
    )
    scope_id: Optional[str] = Field(
        default=None, 
        description="ID for scoped memory (conversation_id, task_id, principal_id, etc.)"
    )


def serialize_memory_items(items: Any) -> List[Dict[str, Any]]:
    """JSON-safe dicts from MemoryItem models or already-serialized rows."""
    out: List[Dict[str, Any]] = []
    for item in items or []:
        if hasattr(item, "model_dump"):
            try:
                dumped = item.model_dump(mode="json")
            except TypeError:
                dumped = item.model_dump()
            if isinstance(dumped, dict):
                out.append(dumped)
        elif isinstance(item, dict):
            out.append(item)
    return out


class AgentCapability(str, Enum):
    reasoning = "reasoning"
    tools = "tools"
    memory = "memory"
    planning = "planning"


class ModelProvider(str, Enum):
    mock = "mock"
    openai = "openai"
    anthropic = "anthropic"


class PoolType(str, Enum):
    """
    Worker pool types for distributed command execution.

    Used as hints for routing commands to optimal worker pools.
    Commands work on ALL pool types - this is just a performance preference.
"""
    HIGH_CONCURRENCY = "high_concurrency"  # eventlet/gevent/threads (50-100+ concurrent I/O)
    PROCESS = "process"  # fork pool (process isolation, CPU-heavy)


class MemoryScopeType(str, Enum):
    """
    Memory scope types for access patterns and retention policies.

    Scope types define HOW memories are accessed and retained, not isolation boundaries.
    Motet/tenant/principal isolation is handled automatically via MemoryItem fields:
    - motet_id: Isolates memories across deployment environments (prod/staging/dev)
    - tenant_id: Isolates memories across organizations/customers
    - principal_id: Isolates memories across users within a tenant

    Scope Type Definitions:
    - GLOBAL: Tenant-wide shared knowledge (within current motet)
    Accessible to all users/conversations in this tenant+motet
    Example: "Company policy: vacation requests require 2 weeks notice"

    - PRINCIPAL: User-specific memories that follow the user across conversations
    Example: "User prefers concise responses", "User's timezone: PST"

    - CONVERSATION: Conversation-specific ephemeral context
    Cleaned up after conversation ends
    Example: "User is asking about project Alpha", "Current topic: pricing"

    - TASK: Task-specific ephemeral memories
    Cleaned up after task completion
    Example: "Processing batch job #123", "Current step: validation"

    - COLLECTIVE: Cross-worker validated insights
    Promoted through consensus mechanism
    Example: "Best practices for error handling", "Common user patterns"

    - BACKGROUND: Autonomous thinking from background processes
    Generated by scheduled reflection tasks
    Example: "Pattern detected: users struggle with onboarding step 3"

    Access Hierarchy (broadest to narrowest):
    GLOBAL (tenant-wide, motet-specific)
    ↓
    PRINCIPAL (user-specific within tenant+motet)
    ↓
    CONVERSATION (conversation-specific)
    ↓
    TASK (task-specific ephemeral)

    Note: COLLECTIVE and BACKGROUND can exist at any level depending on their source.

    Each scope type has different retention policies and access patterns.
"""
    GLOBAL = "global"                    # Tenant-wide shared knowledge (motet-specific)
    PRINCIPAL = "principal"              # User-specific memories
    CONVERSATION = "conversation"        # Conversation-specific ephemeral
    TASK = "task"                       # Task-specific ephemeral
    COLLECTIVE = "collective"            # Cross-worker validated insights
    BACKGROUND = "background"            # Autonomous thinking memories


__all__ = [
    "Role",
    "Message",
    "ContentPart",
    "TextPart",
    "MediaPart",
    "Response",
    "Tool",
    # ADR-0064 canonical protocol types
    "StopReason",
    "FallbackPolicy",
    "OutputContract",
    "ToolCallRequest",
    "ToolCallResult",
    "CanonicalToolSchema",
    "tool_schema_name",
    "LLMUsage",
    "CitationSpan",
    "Citation",
    "CitationsEvent",
    "ThinkingEvent",
    "ToolUseEvent",
    "LLMStreamEvent",
    "OutputItem",
    "TranscriptItem",
    "SkillRef",
    "LLMRequest",
    "LLMResponse",
    # ADR-0113 image generation types
    "ImageGenerationRequest",
    "GeneratedImage",
    "ImageGenerationResponse",
    "MemoryItem",
    "serialize_memory_items",
    "AgentCapability",
    "ModelProvider",
    "PoolType",
    "MemoryScopeType",
]


# Generic protocol for registries that index by two-part string keys.
# Invariant: T appears in both parameter and return positions (register/build/get).
T = TypeVar("T")


class BaseRegistry(Protocol[T]):
    def register(self, key1: str, key2: str, factory: Callable[..., T], **metadata: Any) -> None: ...

    def build(self, key1: str, key2: str, **kwargs: Any) -> T: ...

    def get(self, key1: str, key2: str) -> Optional[Callable[..., T]]: ...

    def list(self, key1_filter: Optional[str] = None) -> List[Tuple[str, str]]: ...

    def supports(self, key1: str, key2: str) -> bool: ...


__all__.append("BaseRegistry")

# Export identity types
__all__.extend(["Principal", "Tenant"])


