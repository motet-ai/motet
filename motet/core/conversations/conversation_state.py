"""
Motet - Conversation State Facade

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-28

Description:
    Thin facade for conversation state: load history (replay transcripts) and
    append a turn (store transcript). Single place for "conversation state"
    contract used by prepare_context, finalize_turn, conversation_get, and
    future consumers (export, audit, etc.).

Dependencies:
    - transcript_replay: get_conversation_history_from_transcripts
    - transcript_storage: store_turn_transcript

Usage:
    from motet.core.conversations.conversation_state import load_history, append_turn

    history = load_history(motet, conversation_id)
    append_turn(motet, messages, assistant_response)
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

from .transcript_replay import get_conversation_history_from_transcripts
from .transcript_storage import store_turn_transcript


def load_history(
    motet: Any,
    conversation_id: str,
    *,
    limit: int = 250,
) -> List[Tuple[Any, Any]]:
    """
    Load conversation history for a conversation (canonical transcript replay).

    Returns chronological list of (created_at, Message). Use for context building
    (prepare_context) or API response (conversation_get).
    """
    return get_conversation_history_from_transcripts(
        motet,
        conversation_id,
        limit=limit,
    )


def append_turn(
    motet: Any,
    messages: List[Any],
    assistant_response: str,
    *,
    agent_id: Optional[str] = None,
    thinking_text: Optional[str] = None,
) -> dict:
    """
    Append one turn to conversation state (store canonical transcript).

    Persists this turn as one conversation_transcript memory. Returns the
    result dict from store_turn_transcript (canonical_transcript_stored, etc.).
    """
    return store_turn_transcript(
        motet,
        messages,
        assistant_response,
        agent_id=agent_id,
        thinking_text=thinking_text,
    )


__all__ = ["append_turn", "load_history"]
