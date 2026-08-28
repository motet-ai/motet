"""
Motet - Memory constants

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Shared constants for memory tagging and scoping. Used by the memory manager,
    orchestration, conversation commands, and builtin tools so conversation-scoped
    tags are consistent (conversation:id rather than session:id).

Usage:
    from motet.core.memory.constants import CONVERSATION_SCOPE_TAG_PREFIX
    tag = f"{CONVERSATION_SCOPE_TAG_PREFIX}{conversation_id}"
"""

# Tag prefix for scoping memories/vector items to a conversation (replaces legacy "session:").
CONVERSATION_SCOPE_TAG_PREFIX = "conversation:"
