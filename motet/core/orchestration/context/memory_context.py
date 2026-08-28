"""
Motet - Memory Recall Context Provider

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-04

Description:
    Implements cross-conversation memory recall for the context preparation
    pipeline. The provider derives a query from the latest user message,
    retrieves relevant memories, records observability metadata, and injects
    memory context before artifact providers run.

Dependencies:
    - time for provider timing metrics
    - motet.core.types.Message for fallback system-message insertion
    - context.types for shared pipeline state

Usage:
    state = MemoryRecallProvider().apply(state, data=data, motet=motet, logger=logger)

Notes:
    - Hybrid retrieval remains preferred when available.
    - Fallback behavior matches the previous prepare_context implementation.
"""

from __future__ import annotations

import time
from typing import Any

from ...types import Message
from .types import ContextPipelineState


class MemoryRecallProvider:
    """Retrieve and inject relevant cross-conversation memory."""

    name = "memory_recall"

    def apply(
        self,
        state: ContextPipelineState,
        *,
        data: Any,
        motet: Any,
        logger: Any,
    ) -> ContextPipelineState:
        if not data.include_memory_recall:
            return state

        try:
            t0 = time.perf_counter()
            last_user_msg = next((msg for msg in reversed(state.messages) if msg.role == "user"), None)
            query = last_user_msg.content if last_user_msg else ""

            if hasattr(motet.memory, "hybrid_retrieve") and query:
                memory_items = motet.memory.hybrid_retrieve(
                    query=query,
                    limit=5,
                    min_relevance=0.4,
                    include_recent=True,
                    include_vector=True,
                    conversation_id=motet.conversation_id,
                )
            elif hasattr(motet.memory, "recall"):
                memory_items = motet.memory.recall(
                    limit=10,
                    conversation_id=motet.conversation_id,
                )
            else:
                memory_items = []

            state.context_info["memory_items"] = [
                (
                    item.model_dump(mode="json")
                    if hasattr(item, "model_dump")
                    else item.__dict__ if hasattr(item, "__dict__") else str(item)
                )
                for item in memory_items
            ]

            if hasattr(motet.memory, "apply_vector_recall") and query and memory_items:
                state.messages = motet.memory.apply_vector_recall(
                    messages=state.messages,
                    query=query,
                    max_context_items=3,
                    min_relevance=0.5,
                    conversation_id=motet.conversation_id,
                    memory_items=memory_items,
                )
            elif memory_items:
                memory_context = "Recent context: " + "; ".join(
                    [item.content if hasattr(item, "content") else str(item) for item in memory_items[:3]]
                )
                if state.messages and state.messages[-1].role == "user":
                    context_msg = Message(role="system", content=memory_context)
                    state.messages.insert(-1, context_msg)

            state.timings["memory_recall_s"] = round(time.perf_counter() - t0, 3)
        except Exception as e:
            state.context_info["memory_error"] = str(e)

        return state
