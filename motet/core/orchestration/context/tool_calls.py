"""
Motet - Context Tool-Call Sanitization

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Prepare-context entry point for tool-call transcript sanitization. Delegates to the shared provider-boundary sanitizer so
    conversation history merge / token budgeting use the same rules as
    DeepSeek/OpenAI/Moonshot adapters: keep valid assistant+tool blocks
    (repairing adjacency across transparent system noise) and drop orphan
    tool messages that would cause provider 400s.

Dependencies:
    - motet.core.models.adapters.providers.message_history_sanitizer: canonical
      sanitize_orphan_tool_call_messages implementation
    - typing for mixed dict/model message handling

Usage:
    messages, stats = sanitize_orphan_tool_call_messages(messages)

Notes:
    - The sanitizer accepts both canonical Message models and dict-shaped
      messages because prepare_context can receive either during tests and
      distributed command serialization.
    - ``extract_tool_call_ids`` remains here for local helpers/tests that only
      need ID extraction without importing the provider package.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from ...models.adapters.providers.message_history_sanitizer import (
    sanitize_orphan_tool_call_messages as _sanitize_orphan_tool_call_messages,
)


def extract_tool_call_ids(tool_calls: Any) -> List[str]:
    """Extract normalized tool-call IDs from mixed dict/model payloads."""

    ids: List[str] = []
    for call in tool_calls or []:
        call_id = None
        if isinstance(call, dict):
            call_id = call.get("call_id") or call.get("id") or call.get("tool_call_id")
        else:
            call_id = getattr(call, "call_id", None) or getattr(call, "id", None) or getattr(call, "tool_call_id", None)
        if call_id:
            ids.append(str(call_id))
    return ids


def sanitize_orphan_tool_call_messages(messages: List[Any]) -> Tuple[List[Any], Dict[str, int]]:
    """
    Sanitize tool-call transcript spans for prepare_context / token budgeting.

    See ``message_history_sanitizer.sanitize_orphan_tool_call_messages`` for
    behavior (shared with provider adapters).
    """
    return _sanitize_orphan_tool_call_messages(messages)
