"""
Motet - Loop State Snapshot Codec

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-29

Description:
    Single conversion surface between AgenticLoopData, in-process continuations, and TurnCheckpoint loop-state fields (issue #147).
    Replaces the hand-copied field lists in agent entry, agentic_loop continue,
    suspend, and resume_turn so new loop fields cannot silently drift.

Dependencies:
    - pydantic: Snapshot model validation/serialization
    - AgenticLoopData: Loop command input shape
    - TurnCheckpoint: Suspension checkpoint model (typed via TYPE_CHECKING)

Usage:
    from motet.core.reasoning.react.loop_state_snapshot import (
        LoopStateSnapshot,
    )

    # Recursion / continue with overrides
    next_data = LoopStateSnapshot.from_loop_data(
        data,
        remaining_iterations=data.remaining_iterations - 1,
        usage_accumulator=accumulated_usage,
    ).to_loop_data(
        conversation_history=data.conversation_history,
        stream_key=data.stream_key,
    )

    # Suspend → checkpoint loop fields
    snap = LoopStateSnapshot.from_loop_data(data, usage_accumulator=dict(usage))
    checkpoint = TurnCheckpoint(..., **snap.to_checkpoint_loop_fields())

    # Resume ← checkpoint
    data = LoopStateSnapshot.from_checkpoint(checkpoint).to_loop_data(
        conversation_history=history,
        stream_key=motet.stream_key,
    )

Notes:
    - conversation_history / stream_key / prefilled_tool_calls are intentionally
      outside the snapshot: history is checkpointed separately (and may be
      caller-overridden on resume), stream_key is invocation-scoped, and
      prefilled_tool_calls applies only to first-action entry.
    - to_checkpoint_loop_fields() serializes nested models for Redis durability.
    - Checkpoint Redis blobs are nested v1 (``schema_version`` + identity /
      loop_state / handback) via ``TurnCheckpoint.to_storage_dict`` (#157);
      this codec still speaks the flat in-process field list.
    - ``with_fresh_budget()`` is the issue #188 Continue policy; handback resume
      must keep ``remaining_iterations`` / ``model_calls_used`` instead.
      Fresh budget clears ``thinking_parts`` so a new Continue turn does not
      persist the prior turn's reasoning as this turn's display thinking.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...types import CanonicalToolSchema, Message, SkillRef
from .agent_data import DEFAULT_MODEL_NAME, DEFAULT_MODEL_PROVIDER
from .agentic_loop_data import AgenticLoopData, PrefilledToolCall

if TYPE_CHECKING:
    from ...checkpoints import TurnCheckpoint

# Fields that must stay in sync across agent entry, recursion, suspend, and resume.
_LOOP_STATE_FIELDS = (
    "input",
    "tools",
    "tool_filter_metadata",
    "executed_signatures",
    "stalled_iterations",
    "observation_cache",
    "used_tool_names",
    "max_iterations",
    "remaining_iterations",
    "max_model_calls",
    "model_calls_used",
    "max_cost_usd",
    "max_prompt_tokens",
    "max_tool_time_ms",
    "max_tools",
    "model_provider",
    "model_name",
    "model_profile_name",
    "temperature",
    "enable_thinking",
    "reasoning_effort",
    "enable_prompt_caching",
    "usage_accumulator",
    "media_accumulator",
    "thinking_parts",
    "tool_summaries",
    "spawn_children",
    "skill_refs",
    "handback_tool_names",
    "handback_tools",
    "agent_id",
    "parent_agent_id",
    "inject_meta_tools",
)


def _serialize_optional_items(
    items: Optional[Sequence[Any]],
) -> Optional[List[Dict[str, Any]]]:
    """Serialize Pydantic models (or pass through dicts) for checkpoint storage."""
    if items is None:
        return None
    return [
        item.model_dump(mode="json") if hasattr(item, "model_dump") else item
        for item in items
    ]


class LoopStateSnapshot(BaseModel):
    """
    Motet-authoritative loop fields shared by AgenticLoopData and TurnCheckpoint.

    Use from_loop_data / from_checkpoint to build, then to_loop_data or
    to_checkpoint_loop_fields to emit the destination shape.
    """

    input: str = ""
    tools: Optional[List[Union[Dict[str, Any], CanonicalToolSchema]]] = None
    tool_filter_metadata: Optional[Dict[str, Any]] = None
    executed_signatures: List[str] = Field(default_factory=list)
    stalled_iterations: int = 0
    observation_cache: Dict[str, Any] = Field(default_factory=dict)
    used_tool_names: List[str] = Field(default_factory=list)
    max_iterations: int = 20
    remaining_iterations: int = 20
    max_model_calls: int = 60
    model_calls_used: int = 0
    max_cost_usd: float = 0.0
    max_prompt_tokens: int = 0
    max_tool_time_ms: int = 0
    max_tools: int = 10
    model_provider: str = DEFAULT_MODEL_PROVIDER
    model_name: str = DEFAULT_MODEL_NAME
    model_profile_name: Optional[str] = None
    temperature: float = 0.7
    enable_thinking: bool = False
    reasoning_effort: Optional[str] = "medium"
    enable_prompt_caching: Optional[bool] = None
    # Token counters are ints, the ADR-0018 cost_usd running total is a float.
    usage_accumulator: Optional[Dict[str, Any]] = None
    media_accumulator: List[Dict[str, Any]] = Field(default_factory=list)
    thinking_parts: List[str] = Field(default_factory=list)
    tool_summaries: List[Dict[str, Any]] = Field(default_factory=list)
    spawn_children: List[Dict[str, Any]] = Field(default_factory=list)
    skill_refs: Optional[List[Union[SkillRef, Dict[str, Any]]]] = None
    handback_tool_names: Optional[List[str]] = None
    handback_tools: Optional[List[Union[Dict[str, Any], CanonicalToolSchema]]] = None
    agent_id: Optional[str] = None
    parent_agent_id: Optional[str] = None
    inject_meta_tools: bool = True

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @field_validator("skill_refs", mode="before")
    @classmethod
    def _coerce_skill_refs(
        cls, v: Any
    ) -> Optional[List[Union[SkillRef, Dict[str, Any]]]]:
        """Accept SkillRef models or checkpoint-serialized dicts."""
        if v is None:
            return None
        items = v if isinstance(v, list) else [v]
        out: List[Union[SkillRef, Dict[str, Any]]] = []
        for item in items:
            if isinstance(item, SkillRef) or isinstance(item, dict):
                out.append(item)
            elif hasattr(item, "model_dump"):
                out.append(item.model_dump(mode="json"))
            else:
                out.append(item)
        return out

    @classmethod
    def from_loop_data(
        cls,
        data: AgenticLoopData,
        **overrides: Any,
    ) -> "LoopStateSnapshot":
        """Capture loop state from AgenticLoopData, applying optional field overrides."""
        payload: Dict[str, Any] = {name: getattr(data, name) for name in _LOOP_STATE_FIELDS}
        payload.update(overrides)
        return cls.model_validate(payload)

    @classmethod
    def from_checkpoint(
        cls,
        checkpoint: "TurnCheckpoint",
        **overrides: Any,
    ) -> "LoopStateSnapshot":
        """Restore loop state from a TurnCheckpoint, applying optional field overrides."""
        payload: Dict[str, Any] = {
            name: getattr(checkpoint, name) for name in _LOOP_STATE_FIELDS
        }
        payload.update(overrides)
        return cls.model_validate(payload)

    def to_loop_data(
        self,
        *,
        conversation_history: List[Message],
        stream_key: str,
        prefilled_tool_calls: Optional[List[PrefilledToolCall]] = None,
        **overrides: Any,
    ) -> AgenticLoopData:
        """
        Build AgenticLoopData for loop entry / recursion / resume.

        conversation_history and stream_key are required (invocation-scoped).
        prefilled_tool_calls is entry-only (ADR-0111) and omitted unless supplied.
        """
        payload = self.model_dump()
        payload.update(overrides)
        payload["conversation_history"] = conversation_history
        payload["stream_key"] = stream_key
        if not payload.get("reasoning_effort"):
            payload["reasoning_effort"] = "medium"
        if prefilled_tool_calls is not None:
            payload["prefilled_tool_calls"] = prefilled_tool_calls
        return AgenticLoopData.model_validate(payload)

    def to_checkpoint_loop_fields(self) -> Dict[str, Any]:
        """
        Kwargs for the loop-state portion of TurnCheckpoint(...).

        Nested tool/skill models are serialized for Redis durability. Identity
        and handback records remain the caller's responsibility.
        """
        return {
            "input": self.input,
            "tools": _serialize_optional_items(self.tools),
            "tool_filter_metadata": self.tool_filter_metadata,
            "executed_signatures": list(self.executed_signatures or []),
            "stalled_iterations": self.stalled_iterations,
            "observation_cache": dict(self.observation_cache or {}),
            "used_tool_names": list(self.used_tool_names or []),
            "max_iterations": self.max_iterations,
            "remaining_iterations": self.remaining_iterations,
            "max_model_calls": self.max_model_calls,
            "model_calls_used": self.model_calls_used,
            "max_cost_usd": self.max_cost_usd,
            "max_prompt_tokens": self.max_prompt_tokens,
            "max_tool_time_ms": self.max_tool_time_ms,
            "max_tools": self.max_tools,
            "model_provider": self.model_provider,
            "model_name": self.model_name,
            "model_profile_name": self.model_profile_name,
            "temperature": self.temperature,
            "enable_thinking": self.enable_thinking,
            "reasoning_effort": self.reasoning_effort,
            "enable_prompt_caching": self.enable_prompt_caching,
            "usage_accumulator": (
                dict(self.usage_accumulator) if self.usage_accumulator else None
            ),
            "media_accumulator": list(self.media_accumulator or []),
            "thinking_parts": list(self.thinking_parts or []),
            "tool_summaries": [dict(row) for row in (self.tool_summaries or [])],
            "spawn_children": [dict(row) for row in (self.spawn_children or [])],
            "skill_refs": _serialize_optional_items(self.skill_refs),
            # Always a list on the checkpoint (matches prior suspend field copy).
            "handback_tool_names": list(self.handback_tool_names or []),
            "handback_tools": _serialize_optional_items(self.handback_tools),
            "agent_id": self.agent_id,
            "parent_agent_id": self.parent_agent_id,
            "inject_meta_tools": self.inject_meta_tools,
        }

    def with_fresh_budget(
        self,
        *,
        max_iterations: Optional[int] = None,
        max_model_calls: Optional[int] = None,
    ) -> "LoopStateSnapshot":
        """
        Issue #188 budget Continue policy: reset counters for a new turn.

        Keeps tool/signature/media continuity from the prior turn's snapshot.
        Clears usage (new-turn cost attribution) and stalled_iterations.
        Handback resume must **not** use this — it keeps remaining budget.
        """
        max_it = int(max_iterations if max_iterations is not None else self.max_iterations)
        if max_it < 1:
            max_it = 1
        if max_model_calls is not None:
            max_mc = int(max_model_calls)
        else:
            max_mc = int(self.max_model_calls or 0) or max(max_it * 3, 30)
        if max_mc < 1:
            max_mc = 1
        return self.model_copy(
            update={
                "max_iterations": max_it,
                "remaining_iterations": max_it,
                "max_model_calls": max_mc,
                "model_calls_used": 0,
                "stalled_iterations": 0,
                "usage_accumulator": None,
                "thinking_parts": [],
                "tool_summaries": [],
                "spawn_children": [],
            }
        )


__all__ = ["LoopStateSnapshot"]
