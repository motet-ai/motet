"""
Motet - Trivial Message Classifier

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Closed allowlist for greetings, thanks, closers, and acknowledgements.
    The turn gate uses this to skip tools on a trivial last user message.
    Conversation analysis uses the same helpers so skip-analysis cannot
    drift from the gate.

    Matching is context-free: the same string can be a greeting or a
    confirmation. ``pending_action_blocks_direct`` supplies that context
    and lives with the confirmation marker, not here.

Dependencies:
    - motet.core.conversations.pending_action: ``AFFIRMATIVE_ACKS``,
      ``NEGATIVE_ACKS``, and ``normalize_ack_text`` so acknowledgements
      cannot drift from confirm/decline classification

Usage:
    from motet.core.conversations.trivial_message import (
        is_trivial_message, last_user_message,
    )

    last = last_user_message(history)
    if last is not None and is_trivial_message(last):
        ...

Notes:
    - Messages containing "?" never match. Multimodal messages never match.
    - The allowlist is composed from the confirmation ack tables plus
      greetings, thanks, and closers. Do not duplicate those ack strings here.
"""

from __future__ import annotations

from typing import Any, List, Optional

from motet.core.conversations.pending_action import (
    AFFIRMATIVE_ACKS,
    NEGATIVE_ACKS,
    normalize_ack_text,
)

# Closed allowlist. Anything not on this list is not a greeting/ack.
# Matching alone is not enough to skip the loop: a pending proposal still
# wins so a "yes" confirming an action re-enters the loop.
_TRIVIAL_MESSAGE_ALLOWLIST = frozenset({
    "hi", "hello", "hey", "hiya", "howdy", "yo",
    "good morning", "good afternoon", "good evening", "morning", "evening",
    "thanks", "thank you", "thanks so much", "thank you so much",
    "thanks a lot", "many thanks", "thx", "ty", "tysm", "cheers",
    "appreciated", "much appreciated", "appreciate it",
    "bye", "goodbye", "good night", "goodnight", "see you", "see ya",
    "later", "cya", "take care",
    "cool", "great", "nice", "perfect", "awesome", "amazing",
    "got it", "understood", "makes sense", "done", "no problem", "np",
    "love it", "fair enough",
    "ok thanks", "okay thanks", "ok thank you", "great thanks",
    "perfect thanks", "got it thanks", "sounds good thanks",
    "no thanks", "thanks bye",
}) | AFFIRMATIVE_ACKS | NEGATIVE_ACKS


def last_user_message(messages: Optional[List[Any]]) -> Optional[Any]:
    """Last ``role=user`` message, or None when the history has none."""
    if not messages:
        return None
    for msg in reversed(messages):
        if getattr(msg, "role", None) == "user":
            return msg
    return None


def is_trivial_message(message: Any) -> bool:
    """Return True when a user message matches the closed trivial allowlist.

    Multimodal messages never qualify. Questions (``ok?``) never qualify.
    Context-free by design: the same string can be a greeting or a
    confirmation. ``pending_action_blocks_direct`` is that context.
    """
    if getattr(message, "content_parts", None):
        return False
    content = getattr(message, "content", "") or ""
    if not isinstance(content, str):
        return False
    if "?" in content:
        return False
    return normalize_ack_text(content) in _TRIVIAL_MESSAGE_ALLOWLIST


__all__ = [
    "is_trivial_message",
    "last_user_message",
]
