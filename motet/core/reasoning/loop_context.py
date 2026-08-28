"""
Motet - Reasoning Loop Context

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Defines the LoopContext model and helpers used to isolate per-loop state for
    parallel reasoning execution (e.g., nested agentic loops). Provides a stable
    container for loop-scoped conversation history, stream keys, and metadata.

Dependencies:
    - typing: Type hints and annotations
    - pydantic: Field definitions for data validation
    - BaseCommandData/MessageFieldMixin: Shared command data base and message coercion
    - Message: Conversation message type for history

Usage:
    from motet.core.reasoning.loop_context import (
        LoopContext, build_loop_context, resolve_conversation_history
    )

    loop_context = build_loop_context(
        loop_id="core.default.spawn-1",
        base_stream_key="task:abc123:response",
        conversation_history=history,
        parent_agent_id="core.default",
        metadata={"task_index": 1}
    )

Notes:
    - Use loop_context for per-loop isolation when running parallel reasoning loops.
    - Conversation history is copied to avoid cross-loop mutation.
    - Stream keys are scoped per loop to prevent event interleaving.
"""

from typing import Optional, List, Dict, Any

from pydantic import Field

from motet.core.commands.base_command_data import BaseCommandData, MessageFieldMixin
from ..types import Message


class LoopContext(MessageFieldMixin, BaseCommandData):
    """
    Per-loop context for isolating reasoning state.
    
    Encapsulates conversation history and stream key for a single loop instance.
    """

    loop_id: str = Field(
        ...,
        description="Unique identifier for the loop instance"
    )
    stream_key: Optional[str] = Field(
        default=None,
        description="Loop-scoped stream key for event emission"
    )
    conversation_history: Optional[List[Message]] = Field(
        default=None,
        description="Loop-isolated conversation history for safe parallel execution"
    )
    parent_agent_id: Optional[str] = Field(
        default=None,
        description="Parent agent identity when this loop is a nested sub-agent",
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional loop metadata (trace index, node id, etc.)"
    )


def build_loop_context(
    loop_id: str,
    *,
    base_stream_key: Optional[str] = None,
    conversation_history: Optional[List[Message]] = None,
    parent_agent_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> LoopContext:
    """
    Build a loop context with per-loop isolation.
    
    Copies conversation history to avoid cross-loop mutation and scopes stream keys
    to prevent interleaved events when multiple loops run in parallel.
    """

    history_copy = list(conversation_history) if conversation_history else None
    stream_key = None
    if base_stream_key:
        stream_key = f"{base_stream_key}:{loop_id}"          

    return LoopContext(
        loop_id=loop_id,
        stream_key=stream_key,
        conversation_history=history_copy,
        parent_agent_id=parent_agent_id,
        metadata=metadata,
    )


def resolve_conversation_history(
    loop_context: Optional[LoopContext],
    fallback_history: Optional[List[Message]],
) -> Optional[List[Message]]:
    """
    Resolve the conversation history to use for a loop.
    
    Prefers loop_context history and returns a copy to preserve isolation.
    """

    if loop_context and loop_context.conversation_history is not None:
        return list(loop_context.conversation_history)
    if fallback_history:
        return list(fallback_history)
    return None
