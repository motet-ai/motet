"""
Motet - Conversation History Context Provider

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-29

Description:
    Implements the conversation-history stage of the prepare_context pipeline.
    It replays canonical conversation transcripts for the active conversation,
    merges them with the current request messages, and removes malformed
    assistant tool-call spans before later providers add memory and artifact
    context.

Dependencies:
    - time for provider timing metrics
    - motet.core.conversations for canonical transcript replay
    - motet.core.types.Message for typed history messages
    - context.tool_calls for provider-safe tool-call sanitization

Usage:
    state = ConversationHistoryProvider().apply(state, data=data, motet=motet, logger=logger)

Notes:
    - This provider intentionally degrades to the original input messages when
      transcript replay fails, matching the previous prepare_context behavior.
"""

from __future__ import annotations

import time
from typing import Any, List

from ...types import Message
from .tool_calls import sanitize_orphan_tool_call_messages
from .types import ContextPipelineState


class ConversationHistoryProvider:
    """Load and merge canonical conversation history for the active turn."""

    name = "conversation_history"

    def apply(
        self,
        state: ContextPipelineState,
        *,
        data: Any,
        motet: Any,
        logger: Any,
    ) -> ContextPipelineState:
        logger.info(
            "prepare_context_conversation_check",
            conversation_id=motet.conversation_id,
            has_memory=hasattr(motet, "memory") and motet.memory is not None,
            has_recall_conversation=(
                hasattr(motet.memory, "recall_conversation") if hasattr(motet, "memory") and motet.memory else False
            ),
        )

        if motet.conversation_id and motet.memory and hasattr(motet.memory, "recall_conversation"):
            try:
                logger.info(
                    "prepare_context_searching_memories",
                    conversation_id=motet.conversation_id,
                )
                conversation_history: List[Message] = []
                try:
                    from ...conversations import load_history
                    from ...conversations.transcript_replay import merge_conversation_history

                    t0 = time.perf_counter()
                    tuples = load_history(motet, motet.conversation_id, limit=100)
                    conversation_history = [msg for _, msg in tuples]
                    for hist_msg in conversation_history:
                        meta = getattr(hist_msg, "metadata", None)
                        if isinstance(meta, dict) and (
                            "thinking_text" in meta
                            or "tool_summaries" in meta
                            or "cost_usd" in meta
                            or "spawn_children" in meta
                        ):
                            cleaned = dict(meta)
                            cleaned.pop("thinking_text", None)
                            cleaned.pop("tool_summaries", None)
                            cleaned.pop("cost_usd", None)
                            cleaned.pop("spawn_children", None)
                            hist_msg.metadata = cleaned
                    state.messages = merge_conversation_history(state.messages, conversation_history)
                    state.messages, sanitize_stats = sanitize_orphan_tool_call_messages(state.messages)
                    if sanitize_stats["removed_assistant_calls"] > 0 or sanitize_stats["removed_tool_messages"] > 0:
                        logger.warning(
                            "prepare_context_orphan_tool_calls_pruned",
                            conversation_id=motet.conversation_id,
                            removed_assistant_calls=sanitize_stats["removed_assistant_calls"],
                            removed_tool_messages=sanitize_stats["removed_tool_messages"],
                        )
                    state.timings["history_load_s"] = round(time.perf_counter() - t0, 3)
                except Exception as e:
                    logger.warning(
                        "canonical_transcript_replay_failed",
                        error=str(e),
                        conversation_id=motet.conversation_id,
                        exc_info=True,
                    )
                    conversation_history = []

                logger.info(
                    "prepare_context_memories_found",
                    conversation_id=motet.conversation_id,
                    memory_count=len(conversation_history),
                )

                state.context_info["conversation_history_retrieved"] = len(conversation_history)
                logger.info(
                    "conversation_history_retrieved",
                    conversation_id=motet.conversation_id,
                    history_count=len(conversation_history),
                    total_messages=len(state.messages),
                    parsed_messages=[{"role": m.role, "content": m.content[:50]} for m in conversation_history[:3]],
                )
            except Exception as e:
                logger.warning(
                    "conversation_history_retrieval_failed",
                    conversation_id=motet.conversation_id,
                    error=str(e),
                    error_type=type(e).__name__,
                    exc_info=True,
                )
                state.context_info["conversation_history_error"] = str(e)
        elif not motet.conversation_id:
            logger.debug(
                "prepare_context_no_conversation_id",
                task_id=motet.task_id,
            )
        elif not motet.memory:
            logger.warning(
                "prepare_context_no_memory_manager",
                conversation_id=motet.conversation_id,
            )
        elif not hasattr(motet.memory, "recall_conversation"):
            logger.warning(
                "prepare_context_no_recall_conversation_method",
                conversation_id=motet.conversation_id,
                memory_type=type(motet.memory).__name__ if hasattr(motet, "memory") else "None",
            )

        return state
