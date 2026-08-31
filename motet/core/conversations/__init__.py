"""
Motet - Conversations (core)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-29

Description:
    Conversation-related helpers: canonical transcript codec, rendering, replay,
    storage, conversation-state facade for prepare_context, finalize_turn,
    the conversation registry, conversation ownership
    (issue #139), conversation lineage (opaque isolated ids +
    parent/root pointers and a parent→children index), and the
    child-conversation lifecycle for fan-outs (mint, register, brief,
    reply, card pointer).

Usage:
    from motet.core.conversations import get_conversation_history_from_transcripts, load_history, append_turn
    from motet.core.conversations.registry import list_conversations, register_or_touch_conversation
    from motet.core.conversations.ownership import authorize_conversation_access_sync
    from motet.core.conversations.transcript_storage import store_turn_transcript
    from motet.core.conversations.lineage import mint_isolated_conversation, root_conversation_id_of
    from motet.core.conversations.children import create_child_conversation, complete_child_conversation
"""

from .conversation_state import append_turn, load_history
from .transcript_replay import get_conversation_history_from_transcripts

__all__ = [
    "append_turn",
    "get_conversation_history_from_transcripts",
    "load_history",
]
