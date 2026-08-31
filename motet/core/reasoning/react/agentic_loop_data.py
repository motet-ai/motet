"""
Motet - Agentic Loop Data

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-29

Description:
    In-process loop state for the `agentic_loop` executor. A single Pydantic
    data class (`AgenticLoopData`) captures the state needed for tool chaining and unified reasoning execution. Not a Celery command
    payload (the loop runs inside ``agent_loop``); still registered so
    checkpoint / debug codecs can resolve the type name.
    Includes optional ``enable_prompt_caching`` override for agentic defaults and
    ``handback_tool_names`` for turn suspension on externally-owned tools.
    Carries ``agent_id``, ``model_calls_used``, and ``max_model_calls`` across
    suspend/resume. Client handbacks are same-iteration: they do not
    decrement ``remaining_iterations``; ``max_model_calls`` is the safety rail.

Dependencies:
    - pydantic: Data validation and serialization
    - BaseCommandData: Base class for distributed command data
    - Message: Conversation message type used for history
    - Reasoning Budget Gates: Escalation gating metadata fields

Usage:
    from motet.core.reasoning.react.agentic_loop_data import AgenticLoopData

Notes:
    - All data classes inherit from BaseCommandData for serialization
    - AgenticLoopData holds recursive tool-chaining state
    - Automatically registers with command_data_registry on import
"""


from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field, field_validator, ConfigDict

from motet.core.commands.base_command_data import BaseCommandData, MessageFieldMixin
from ...types import CanonicalToolSchema, Message, ReasoningEffort, SkillRef
from .agent_data import DEFAULT_MODEL_NAME, DEFAULT_MODEL_PROVIDER

class PrefilledToolCall(BaseModel):
    """Caller-supplied first tool call that bypasses the iteration-0 model call.

    When set on the first iteration, the agentic loop synthesizes a canonical
    assistant tool call from this payload, executes it, and continues the loop
    without a planning LLM call. The caller owns both the tool selection and the
    arguments; the arguments are forwarded structurally (never via the model).
    """

    tool_name: str = Field(
        ...,
        description=(
            "Canonical tool or workflow name to invoke as the turn's first action "
            "(e.g. 'workflow_<workflow_id>' or 'mcp.server.tool')."
        ),
    )
    arguments: Dict[str, Any] = Field(
        default_factory=dict,
        description="Tool-call arguments, supplied structurally by the caller.",
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)


class AgenticLoopData(MessageFieldMixin, BaseCommandData):
    """
    Data for one in-process agentic_loop iteration.

    Represents the state for one iteration of the tool chaining pattern. Each
    agentic_loop call receives this data, performs LLM inference, executes tools,
    and may return a continuation with updated state.

    Meta disclosure: agentic_loop is self-contained. If tools is not
    provided, the first iteration builds a short frozen shortlist (always-sticky
    meta tools + keyword pins + required_tools). Catalog tools are reached via
    core.tools_search → core.tool_call, not via a per-turn embedding shortlist.
    
    Fields:
        input: User input for keyword pins and context (used on first iteration if tools not provided)
        conversation_history: Full message history with automatic Message conversion (from MessageFieldMixin)
        tools: Tool schemas (optional - if None, built as the meta-disclosure shortlist on first iteration)
        executed_signatures: Signatures of already-executed tools for duplicate detection
        max_iterations: Maximum iterations for this loop (used to compute iteration counters)
        remaining_iterations: Remaining iterations before stopping (prevents infinite loops)
        current_iteration: Derived 1-based Motet-tool round (max - remaining + 1)
        stream_key: Unified task-level stream key (task:{task_id}:response)
        max_tools: Max schemas in the frozen tools prefix
        model_provider: Provider for model calls (e.g., "openai", "anthropic")
        model_name: Model name for model calls (provider-specific)
        temperature: Sampling temperature for model calls
        model_profile_name: Optional model profile name for routing/policy overrides
    
    Example:
        # Simple usage - let agentic_loop discover tools
        loop_data = AgenticLoopData(
            input="What's the weather?",
            conversation_history=[Message(role="user", content="What's the weather?")],
            remaining_iterations=10,
            stream_key="task:abc123:response"
        )
        
        # Advanced usage - provide pre-selected tool schemas
        loop_data = AgenticLoopData(
            input="What's the weather?",
            conversation_history=[Message(role="user", content="What's the weather?")],
            tools=[{"type": "function", "function": {"name": "get_weather", ...}}],
            remaining_iterations=10,
            stream_key="task:abc123:response"
        )
    """
    # Override BaseCommandData's Optional[List] with a non-optional List[Message].
    # agentic_loop always requires a list (it appends and iterates unconditionally),
    # so None is not a valid state here. Callers that omit this field get an empty list.
    conversation_history: List[Message] = Field(  # type: ignore[override]
        default_factory=list,
        description=(
            "Full conversation message history. Items may be Message objects or dicts "
            "with {role, content}; the parent validator converts dicts automatically."
        ),
    )

    @field_validator('conversation_history', mode='before')
    @classmethod
    def ensure_conversation_history_list(cls, v: Any) -> List[Any]:
        """Normalize None → [] and convert message dicts to Message objects."""
        if not v:
            return []
        result = []
        for msg in v:
            if isinstance(msg, dict):
                result.append(Message.model_validate(msg))
            else:
                result.append(msg)
        return result

    # User input for semantic search (ADR-0059: discovery inside agentic_loop)
    input: str = Field(
        default="",
        description="User input for semantic search tool discovery on first iteration"
    )
    
    # Tool schemas (includes workflows exported as tools with workflow_ prefix)
    # LLM-compatible format with tokenized context params (ADR-0046)
    # If None or empty, agentic_loop will discover tools via semantic search on first iteration
    tools: Optional[List[Union[Dict[str, Any], CanonicalToolSchema]]] = Field(
        default=None,
        description="Tool schemas (optional). Supports legacy provider formats (dict) and canonical schemas (CanonicalToolSchema). If None, discovered via semantic search using input."
    )
    
    # Progress tracking (repeat calls are executed; stalling is what gets stopped)
    executed_signatures: List[str] = Field(
        default_factory=list,
        description=(
            "Signatures of tool calls already made this turn (tool_name:params_hash). "
            "Used to tell a novel call from a repeat, which is the progress signal for "
            "stalled_iterations — repeats are executed, not blocked. Valid only for the "
            "conversation_history they accompany: each asserts that the call's result is "
            "present above, so on resume with a caller-supplied history they are re-derived "
            "from it, since a caller that summarizes drops results."
        )
    )
    stalled_iterations: int = Field(
        default=0,
        description=(
            "Consecutive iterations whose every tool call repeated one already made this "
            "turn. Reset by any novel call. At MAX_STALLED_ITERATIONS the turn stops: a "
            "model asking only for what it already has is not making progress, and unlike "
            "a per-call veto this cannot be escaped by nudging a parameter."
        ),
        ge=0,
    )
    observation_cache: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Per-signature tool freshness (cache-control). A fresh hit replays a "
            "short notice instead of re-executing. Entries store the directive, "
            "not the observation body. Default no-store when a tool omits it."
        ),
    )
    
    # Iteration control
    max_iterations: int = Field(
        default=20,
        description="Maximum iterations for this loop (used for iteration counters and budgeting)",
        ge=1,
    )
    remaining_iterations: int = Field(
        default=20,
        description=(
            "Remaining Motet-tool recursion iterations before stopping. "
            "Decremented only when the loop executes Motet-owned tools and recurses; "
            "client handback suspend/resume does not consume this budget."
        ),
        ge=0  # Must be >= 0
    )
    max_model_calls: int = Field(
        default=60,
        description=(
            "Hard cap on model inference calls per turn. Safety rail against infinite "
            "handback↔model loops when remaining_iterations is not decremented on suspend. "
            "Default matches max(max_iterations * 3, 30)."
        ),
        ge=1,
    )
    model_calls_used: int = Field(
        default=0,
        description="Model inference calls already consumed in this turn (checkpointed on suspend).",
        ge=0,
    )
    max_cost_usd: float = Field(
        default=0.0,
        description=(
            "Stop when accumulated cost_usd reaches this value. 0 disables. "
            "Resolved at agent entry from AgentData / MOTET_AGENT_MAX_COST_USD."
        ),
        ge=0.0,
    )
    max_prompt_tokens: int = Field(
        default=0,
        description=(
            "Stop when accumulated prompt_tokens reaches this count. 0 disables. "
            "Resolved at agent entry from AgentData / MOTET_AGENT_MAX_PROMPT_TOKENS."
        ),
        ge=0,
    )
    max_tool_time_ms: int = Field(
        default=0,
        description=(
            "Stop when accumulated tool_time_ms (join wall clock) reaches "
            "this. 0 disables. Spawn children set 60000; parent turns stay 0."
        ),
        ge=0,
    )
    
    # Streaming
    stream_key: str = Field(
        default="",
        description="Unified task-level stream key (task:{task_id}:response)"
    )
    
    # Shortlist size for meta-tool progressive disclosure (ADR-0128).
    # Must clear always-sticky meta tools (4) plus the largest keyword pin group (4);
    # truncation happens after pins are admitted. Catalog tools are reached via
    # tools_search → tool_call, not by growing this list each iteration.
    max_tools: int = Field(
        default=10,
        description=(
            "Max schemas in the frozen tools prefix. Size above "
            "always-sticky (4) + largest keyword pin group (4); catalog reachability "
            "is tools_search → tool_call, not per-iteration shortlist growth."       ),
        ge=1
    )

    # Model selection for discovery + inference
    model_provider: str = Field(
        default=DEFAULT_MODEL_PROVIDER,
        description="Model provider for discovery and loop inference (e.g., 'openai', 'anthropic')",
    )
    model_name: str = Field(
        default=DEFAULT_MODEL_NAME,
        description="Model name for discovery and loop inference (provider-specific)",
    )
    temperature: float = Field(
        default=0.7,
        description="Sampling temperature for loop inference",
        ge=0.0,
        le=2.0,
    )
    model_profile_name: Optional[str] = Field(
        default=None,
        description="Optional model profile name for routing/policy overrides.",
    )
    # ADR-0064: Extended thinking (reasoning summaries) for o-series/gpt-5
    enable_thinking: bool = Field(
        default=False,
        description="Enable extended thinking/reasoning (provider summaries) for capable models.",
    )
    reasoning_effort: Optional[ReasoningEffort] = Field(
        default="medium",
        description="Reasoning effort when enable_thinking is True: low, medium, high, xhigh, or max.",
    )
    # ADR-0124: None = default on for models with CAP_PROMPT_CACHING; explicit True/False overrides.
    enable_prompt_caching: Optional[bool] = Field(
        default=None,
        description=(
            "Enable provider prompt caching. None defaults to True in the agentic "
            "loop when the resolved model has CAP_PROMPT_CACHING; explicit False disables Motet "
            "optimize-hits wiring (not a universal zero-cache-hits guarantee on automatic providers)."
        ),
    )
    usage_accumulator: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional usage accumulator carried across recursive agentic_loop calls "
            ". Token counters are ints; the cost_usd running "
            "total is a float, so values are not narrowed to int."
        ),
    )
    # ADR-0113: Artifact-backed media (e.g. generated images) produced by tools, carried
    # across recursive iterations so the final (terminal) loop result can surface it. The
    # terminal iteration usually returns an empty tool_results list (it is a text-only
    # response), so without this accumulator media generated in earlier iterations would
    # be lost before the turn collects it for the chat surface/UI.
    media_accumulator: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Artifact-backed media parts (serialized MediaPart dicts with artifact_id) "
            "accumulated across agentic_loop iterations. De-duplicated by "
            "artifact_id; surfaced on the terminal loop result as top-level 'media'."
        ),
    )
    thinking_parts: List[str] = Field(
        default_factory=list,
        description=(
            "Provider reasoning summaries accumulated this loop for display persist. "
            "Carried across iterations on the loop snapshot. Joined onto the "
            "terminal result as thinking_text. Not assistant content and not "
            "replayed into the next user turn."
        ),
    )
    tool_summaries: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Short tool name/status/preview rows accumulated this loop for "
            "conversation reload. Carried across iterations on the loop snapshot. "
            "Optional step is the loop iteration so chat reload can rebuild "
            "Step N. Not tool-call / tool-result replay items."
        ),
    )
    spawn_children: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Card pointers to isolated spawn conversations created this loop. "
            "Accumulated across fan-outs on the loop snapshot and written on "
            "the parent transcript row. Omitted from next-turn model replay."
        ),
    )

    # Checkpoint / observability (ADR-0127). No longer drives schema re-query merge
    # under ADR-0128 (tools[] is the frozen meta bag; catalog schemas live in the tail).
    used_tool_names: List[str] = Field(
        default_factory=list,
        description=(
            "Names of tools called earlier in this turn. Checkpointed on suspend "
            "; retained for observability. Does not reshape tools[] under "
            " meta disclosure."
        ),
    )
    # ADR-0093 / ADR-0128: Filter metadata for discovery mode; applied when tools is None
    tool_filter_metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Exclude/required/prefix/category filters applied when building the "
            "meta-disclosure shortlist and enforced by tools_search / tool_call."
        ),
    )
    skill_refs: Optional[List[SkillRef]] = Field(
        default=None,
        description="Forwarded to each model_stream invocation for observability.",
    )
    # ADR-0111: Prefilled first tool calls. When set, the first iteration skips the
    # planning model call, synthesizes these tool calls, executes them in parallel,
    # and continues. Multiple entries mirror parallel tool calls in one model turn.
    prefilled_tool_calls: Optional[List[PrefilledToolCall]] = Field(
        default=None,
        description=(
            "Caller-supplied first tool call(s). On the first iteration the loop "
            "synthesizes and executes these calls instead of asking the model to choose, "
            "then continues normally. Applies to the first action only; multiple entries "
            "execute together in parallel (not a sequential script)."
        ),
    )

    # ADR-0127: Externally-owned tools. When the model requests any of these, the
    # loop does not execute the turn's calls: it checkpoints its state and returns
    # a `suspended` result carrying the handed-back tool calls, to be continued
    # via the resume_turn command once the external owner supplies observations.
    handback_tool_names: Optional[List[str]] = Field(
        default=None,
        description=(
            "Tool names owned by an external party (e.g. a facade client's "
            "declared tools). A model turn requesting any of them suspends the loop "
            "instead of executing: state is checkpointed and all of the turn's calls "
            "are handed back so the wire transcript stays coherent."
        ),
    )
    # ADR-0125 §5c.1: Externally-owned tool SCHEMAS. Unlike handback_tool_names
    # (which only marks names), these are injected into the model's tool list
    # every iteration — surviving embedding discovery and re-query — so the
    # model can actually call tools that exist only on the client. Their names
    # implicitly extend handback_tool_names; on a name collision with a Motet
    # registry tool, the external schema wins (client-declared ⇒ handback).
    handback_tools: Optional[List[Union[Dict[str, Any], CanonicalToolSchema]]] = Field(
        default=None,
        description=(
            "Canonical schemas for externally-owned tools "
            "(e.g. the OpenAI facade client's declared tools). Injected into the model "
            "tool list every iteration alongside discovery results; calling one "
            "suspends the turn. Names here are implicitly handback_tool_names."
        ),
    )
    # ADR-0127: owning agent id for finalize-on-resume and re-suspend checkpoints.
    # Prefer this over command metadata: resume_turn → agentic_loop often has no
    # agent_id in metadata, which previously produced agent-less checkpoints.
    agent_id: Optional[str] = Field(
        default=None,
        description=(
            "Qualified agent id owning this loop. Checkpointed on suspend "
            "so resume_turn can finalize under the correct agent, and re-suspends "
            "preserve ownership even when command metadata lacks agent_id. "
            "None when the loop is not agent-owned (OpenAI hosted_tools turns "
            "that are not a registry agent)."
        ),
    )
    parent_agent_id: Optional[str] = Field(
        default=None,
        description=(
            "Set when this loop is a nested sub-agent. Same field as "
            "AgentData.parent_agent_id and LoopContext.parent_agent_id. A budget "
            "stop on a nested loop skips the Continue checkpoint — Continue is "
            "for the user turn."
        ),
    )
    inject_meta_tools: bool = Field(
        default=True,
        description=(
            "When True (default), inject Motet's fallback "
            "system prompt. OpenAI hosted_tools sets False: allowlist only, client "
            "messages unchanged, no owning agent. Cursor agent mode is a real "
            "agent_id (cursor.backend), not this flag."
        ),
    )
    @field_validator("prefilled_tool_calls", mode="before")
    @classmethod
    def _coerce_prefilled_tool_calls(cls, v: Any) -> Optional[List[PrefilledToolCall]]:
        if v is None:
            return None
        # Accept a single object/dict for the common single-action case.
        items = v if isinstance(v, list) else [v]
        coerced: List[PrefilledToolCall] = []
        for item in items:
            if isinstance(item, PrefilledToolCall):
                coerced.append(item)
            elif isinstance(item, dict):
                coerced.append(PrefilledToolCall(**item))
            else:
                raise ValueError(
                    "prefilled_tool_calls entries must be PrefilledToolCall or dict"
                )
        return coerced or None

    @field_validator("skill_refs", mode="before")
    @classmethod
    def _coerce_skill_refs_loop(cls, v: Any) -> Optional[List[SkillRef]]:
        if v is None:
            return None
        if not isinstance(v, list):
            raise ValueError("skill_refs must be a list or None")
        out: List[SkillRef] = []
        for item in v:
            if isinstance(item, SkillRef):
                out.append(item)
            elif isinstance(item, dict):
                out.append(SkillRef(**item))
            else:
                raise ValueError(f"skill_refs items must be SkillRef or dict, got {type(item).__name__}")
        return out
    
    @field_validator('executed_signatures', mode='before')
    @classmethod
    def convert_set_to_list(cls, v):
        """Convert set to list for JSON serialization."""
        if isinstance(v, set):
            return list(v)
        return v if v is not None else []
    
    def model_dump(self, **kwargs):
        """Custom model dump to ensure proper serialization."""
        data = super().model_dump(**kwargs)
        # Ensure executed_signatures is a list for JSON serialization
        if 'executed_signatures' in data and isinstance(data['executed_signatures'], set):
            data['executed_signatures'] = list(data['executed_signatures'])
        return data

    @property
    def current_iteration(self) -> int:
        """1-based Motet-tool iteration index (unchanged across client handback resume)."""
        return int(self.max_iterations or 0) - int(self.remaining_iterations or 0) + 1

    model_config = ConfigDict(arbitrary_types_allowed=True)


# Register data classes with command_data_registry
def _register_agentic_loop_data_classes():
    """Register AgenticLoopData under the historical command type name."""
    from motet.core.commands.command_data_registry import command_data_registry
    
    command_data_registry.register("core.agentic_loop", AgenticLoopData)


_register_agentic_loop_data_classes()

