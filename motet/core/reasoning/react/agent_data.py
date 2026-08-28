"""
Motet - Agent Loop Data

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-27

Description:
    Command data model for ``core.agent_loop`` / ``run_agent``. Provides
    minimal input for "run the loop": ``run_agent`` builds LoopContext and
    AgenticLoopData internally. Uses industry naming: `input`, `tools`
    (mapped to AgenticLoopData.input and tools inside the builder).
    Forwards optional ``enable_prompt_caching`` to the agentic loop.
    ``DEFAULT_MODEL_PROVIDER`` / ``DEFAULT_MODEL_NAME`` are the shared
    fallback when a turn does not name a model.

Dependencies:
    - pydantic: Data validation and serialization
    - BaseCommandData, MessageFieldMixin: Base class and message coercion
    - typing: Type hints

Usage:
    from motet.core.reasoning.react.agent_data import AgentData

    data = AgentData(
        agent_id="expert-panel.skeptic",
        use_task_stream=True,  # route events to task-level stream for chat UI
        input="What's the weather in Paris?",
        conversation_history=[Message(role="user", content="...")],
    )

Notes:
    - Agent builds its own LoopContext from agent_id, base_stream_key, etc.
    - use_task_stream=True writes to the task stream (chat turn and spawn children).
    - tools (optional): when provided, skip registry discovery; mapped to tool_schemas.
    - Automatically registers with command_data_registry on import.
    - DEFAULT_MODEL_PROVIDER / DEFAULT_MODEL_NAME are the fallback when a
      turn does not name a model (no_tools reply and AgentData itself).
"""

from typing import Any, Dict, List, Optional, Union

from pydantic import Field, ConfigDict, field_validator

from motet.core.commands.base_command_data import BaseCommandData, MessageFieldMixin
from ...types import CanonicalToolSchema, ReasoningEffort, SkillRef

DEFAULT_MODEL_PROVIDER = "openai"
DEFAULT_MODEL_NAME = "gpt-4.1-mini"


class AgentData(MessageFieldMixin, BaseCommandData):
    """
    Input data for ``core.agent_loop`` and in-process ``run_agent``.

    Minimal surface: callers pass identity (agent_id), input, and optional overrides.
    ``run_agent`` builds LoopContext and AgenticLoopData internally.
    """

    agent_id: str = Field(
        default="agent",
        description="Agent identity; used as loop_id when building LoopContext (e.g. 'core.default', 'core.default.spawn-1').",
    )
    use_task_stream: bool = Field(
        default=False,
        description=(
            "When True, write events to the base task-level stream (task:{task_id}:response) "
            "instead of a scoped per-agent stream. Set by agent_turn and core.spawn_agents "
            "so the chat UI receives events attributed by agent_id."
        ),
    )
    input: str = Field(
        ...,
        description="The input to the agent: user message, sub-task prompt, or instruction.",
    )
    conversation_history: Optional[List[Any]] = Field(
        default=None,
        description="Conversation history; agent copies it into the loop context for isolation.",
    )
    parent_agent_id: Optional[str] = Field(
        default=None,
        description="Parent agent identity for nested sub-agents.",
    )
    base_stream_key: Optional[str] = Field(
        default=None,
        description="Base stream key; agent scopes it per agent_id. When None, derived from motet.task_id.",
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Opaque metadata passed into LoopContext.metadata.",
    )
    tools: Optional[List[Union[Dict[str, Any], CanonicalToolSchema]]] = Field(
        default=None,
        description=(
            "When provided: passed to AgenticLoopData.tools (skip shortlist build). "
            "When None: the meta-disclosure shortlist is built."
        ),
    )
    inject_meta_tools: bool = Field(
        default=True,
        description=(
            "When False, do not inject Motet's fallback "
            "system prompt, and do not stamp an owning agent_id on the loop "
            "(hosted_tools). agent_id remains the LoopContext loop_id only."
        ),
    )
    tool_filter_metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Filter metadata for discovery mode (exclude_tools, exclude_workflows, required_tools, required_workflows, prefix, category). Applied when tools is None.",
    )
    max_iterations: int = Field(
        default=20,
        description="Max Motet-tool recursion iterations for the agentic loop.",
        ge=1,
    )
    max_model_calls: Optional[int] = Field(
        default=None,
        description=(
            "Hard cap on model inference calls per turn (handback safety rail). "
            "None defaults to max(max_iterations * 3, 30) in run_agent."
        ),
        ge=1,
    )
    max_cost_usd: Optional[float] = Field(
        default=None,
        description=(
            "Stop when accumulated model cost reaches this many USD. "
            "None inherits MOTET_AGENT_MAX_COST_USD. 0 disables."
        ),
        ge=0.0,
    )
    max_prompt_tokens: Optional[int] = Field(
        default=None,
        description=(
            "Stop when accumulated prompt tokens reach this count. "
            "None inherits MOTET_AGENT_MAX_PROMPT_TOKENS. 0 disables."
        ),
        ge=0,
    )
    max_tool_time_ms: Optional[int] = Field(
        default=None,
        description=(
            "Stop when accumulated tool_time_ms (join wall clock) reaches "
            "this. None and 0 disable. Spawn children set 60000; parent "
            "turns stay off so a coding session is not cut at 60s."
        ),
        ge=0,
    )
    max_tools: int = Field(
        default=20,
        description=(
            "Max schemas in the frozen tools prefix. "
            "Size above always-sticky + largest keyword pin group."
        ),
        ge=1,
    )
    model_provider: str = Field(
        default=DEFAULT_MODEL_PROVIDER,
        description="Provider for discovery/inference.",
    )
    model_name: str = Field(
        default=DEFAULT_MODEL_NAME,
        description="Model name for discovery/inference.",
    )
    model_profile_name: Optional[str] = Field(
        default=None,
        description="Optional model profile for routing.",
    )
    temperature: float = Field(
        default=0.2,
        description="Sampling temperature.",
        ge=0.0,
        le=2.0,
    )
    # ADR-0064: Extended thinking (reasoning summaries) for o-series/gpt-5; when True, reasoning_effort applies
    enable_thinking: bool = Field(
        default=False,
        description="Enable extended thinking/reasoning (provider summaries) for capable models.",
    )
    reasoning_effort: Optional[ReasoningEffort] = Field(
        default="medium",
        description="Reasoning effort when enable_thinking is True: low, medium, high, xhigh, or max.",
    )
    # ADR-0124: forwarded to agentic_loop; None = CAP-gated default on.
    enable_prompt_caching: Optional[bool] = Field(
        default=None,
        description=(
            "Enable provider prompt caching. None lets the agentic loop default "
            "to True when the model has CAP_PROMPT_CACHING."
        ),
    )
    skill_refs: Optional[List[SkillRef]] = Field(
        default=None,
        description="Canonical skill refs for this turn; forwarded to model_stream.",
    )
    prefilled_tool_calls: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description=(
            "Caller-supplied first tool call(s) (each {tool_name, arguments}). When "
            "set, the agentic loop synthesizes and executes these calls on the first iteration "
            "instead of a planning model call, then continues normally. Multiple entries execute "
            "in parallel; a single object is also accepted and coerced to a one-element list."
        ),
    )
    handback_tool_names: Optional[List[str]] = Field(
        default=None,
        description=(
            "Tool names owned by an external party. Forwarded to the agentic loop; "
            "a model turn requesting any of them suspends the loop (checkpoint + handback) "
            "instead of executing."
        ),
    )
    handback_tools: Optional[List[Union[Dict[str, Any], CanonicalToolSchema]]] = Field(
        default=None,
        description=(
            "Canonical schemas for externally-owned tools (e.g. an OpenAI "
            "facade client's declared tools). Forwarded to the agentic loop, which injects "
            "them into the model tool list every iteration; calling one suspends the turn. "
            "Names are implicitly handback_tool_names."
        ),
    )

    @field_validator("skill_refs", mode="before")
    @classmethod
    def _coerce_skill_refs_agent(cls, v: Any) -> Optional[List[SkillRef]]:
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

    model_config = ConfigDict(arbitrary_types_allowed=True)


def _register_agent_data():
    """Register AgentData with command_data_registry."""
    from motet.core.commands.command_data_registry import command_data_registry
    command_data_registry.register("core.agent_loop", AgentData)


_register_agent_data()
