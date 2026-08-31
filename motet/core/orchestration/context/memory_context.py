"""
Motet - Memory Recall Context Provider

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-31

Description:
    Implements cross-conversation memory recall for the context preparation
    pipeline. The provider derives a query from the latest user message,
    retrieves relevant memories, records observability metadata, and injects
    memory context before artifact providers run. Turns with no user text
    skip recall (same empty-query rule as artifact RAG).

Dependencies:
    - time for provider timing metrics
    - motet.core.types.Message for fallback system-message insertion
    - context.types for shared pipeline state

Usage:
    state = MemoryRecallProvider().apply(state, data=data, motet=motet, logger=logger)

Notes:
    - Hybrid retrieval runs when the latest user message has non-empty text.
    - Missing or blank user text sets context_info memory_recall_skipped=empty_query
      and does not call recall or hybrid_retrieve.
"""

from __future__ import annotations

import time
from typing import Any

from ...types import Message
from .types import ContextPipelineState


def _user_recall_query(messages: list[Any]) -> str:
    """Latest user text, stripped. Empty when there is no user message."""
    last_user = next(
        (msg for msg in reversed(messages) if getattr(msg, "role", None) == "user"),
        None,
    )
    if last_user is None:
        return ""
    content = getattr(last_user, "content", None)
    if not isinstance(content, str):
        return ""
    return content.strip()


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

        query = _user_recall_query(state.messages)
        if not query:
            state.context_info["memory_recall_skipped"] = "empty_query"
            state.context_info.setdefault("memory_items", [])
            return state

        try:
            t0 = time.perf_counter()

            if hasattr(motet.memory, "hybrid_retrieve"):
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

            if hasattr(motet.memory, "apply_vector_recall") and memory_items:
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
