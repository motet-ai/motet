"""
Motet - Conversation Commands

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Distributed commands for conversation list, get, clear, and register.
    Used by the Conversations API (thin HTTP layer) and callable from CLI or other services.
    Tenant/principal come from MotetContext (set by API when invoking).
    conversation_get returns an optional warning when the conversation index
    has rows but transcript replay is empty.

Dependencies:
    - conversation_registry (sync): list_conversations_sync, register_or_touch_conversation_sync, remove_conversation_sync
    - motet.command / decorator: get_motet_context
    - command_data_classes: ListConversationsData, GetConversationData, ClearConversationData, RegisterConversationData

Usage:
    from motet.core.commands.builtin.conversation import conversations_list, conversation_get
    from motet.core.workers import global_invoker
    cmd = conversations_list(task_id="...", conversation_id="", data=ListConversationsData(), tenant_id="t1", principal_id="p1")
    result = global_invoker.execute_command(cmd)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog

from motet import motet
from motet.core.commands.decorator import get_motet_context
from motet.core.commands.capabilities import WorkerCapability
from motet.core.commands.utils import require_context_identity
from motet.core.commands.command_data_classes import (
    ListConversationsData,
    GetConversationData,
    ClearConversationData,
    RegisterConversationData,
    UpdateConversationTitleData,
)
from motet.core.agents import resolve_agent_id
from motet.core.conversations.ownership import (
    ConversationAccessDenied,
    authorize_conversation_access_sync,
    delete_conversation_owner_sync,
    get_conversation_owner_sync,
)
from motet.core.conversations.registry import (
    list_conversations_sync,
    register_or_touch_conversation_sync,
    remove_conversation_sync,
)

logger = structlog.get_logger(__name__)


def _require_conversation_identity(operation: str) -> tuple[str, str, str]:
    """Require motet/tenant/principal identity from MotetContext for user-scoped operations."""
    motet = get_motet_context()
    motet_id, tenant_id, principal_id = require_context_identity(
        motet,
        operation=operation,
        error_template="{field} is required for {operation}",
    )
    return motet_id, tenant_id, principal_id


@motet.command(
    description="List conversations for the current user and tenant, for browsing or picking a chat history.",
    timeout_seconds=30,
    required_capabilities=[WorkerCapability.MEMORY_OPERATIONS],
)
def conversations_list(data: ListConversationsData) -> Dict[str, Any]:
    """
    List conversations for the principal in the tenant (from motet context).
    ADR-0083: filters by agent_id and optional surface_id; returns scope fields in each item.
    Returns list of {id, title, created_at, updated_at, agent_id?, surface_id?} sorted by updated_at desc.
    """
    motet_id, tenant_id, principal_id = _require_conversation_identity("conversations_list")
    # Resolve agent_id to qualified form so filter matches registry (ADR-0083); None/empty -> core.default.
    effective_agent_id = resolve_agent_id(data.agent_id)
    convs = list_conversations_sync(
        motet_id=motet_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        limit=data.limit,
        agent_id=effective_agent_id,
        surface_id=data.surface_id,
    )
    return {"conversations": convs}


@motet.command(
    description="Fetch one conversation's transcript and metadata by conversation id.",
    timeout_seconds=30,
    required_capabilities=[WorkerCapability.MEMORY_OPERATIONS],
)
def conversation_get(data: GetConversationData) -> Dict[str, Any]:
    """
    Get one conversation: history from canonical conversation_transcript memories
    (same as prepare_context replay) plus memory count. Memory count is
    the conversation index size and does not decrypt payloads. ``counts.vector``
    stays 0 on this path (no Search).
    History items include attachments (artifact_id, content_type, etc.) when the
    message has media content_parts so the UI can display images and other artifacts.
    """
    motet = get_motet_context()
    motet_id, tenant_id, principal_id = _require_conversation_identity("conversation_get")
    conversation_id = data.conversation_id
    # Issue #139: reject cross-principal transcript reads. Unclaimed ids that are
    # not in this principal's registry are treated as not-found (empty history)
    # so clients can probe freshly minted ids without a 403.
    try:
        authorize_conversation_access_sync(
            motet_id=motet_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            conversation_id=conversation_id,
            bind_if_unclaimed=False,
        )
    except ConversationAccessDenied:
        if get_conversation_owner_sync(motet_id, tenant_id, conversation_id) is None:
            return {
                "conversation_id": conversation_id,
                "history": [],
                "counts": {"memory": 0, "vector": 0},
                "summary": None,
                "warning": None,
            }
        raise

    hist: List[Dict[str, Any]] = []

    if motet.memory:
        try:
            from motet.core.conversations import load_history
            from motet.core.conversations.transcript_replay import message_to_history_item
            for created_at, msg in load_history(motet, conversation_id, limit=250):
                item = message_to_history_item(msg, created_at)
                if item is not None:
                    hist.append(item)
        except Exception as e:
            logger.debug("recall_conversation_failed", conversation_id=conversation_id, error=str(e))

    mem = {"memory": 0, "vector": 0}
    warning: Optional[str] = None
    stack = motet.stack if motet else None
    if stack:
        try:
            store = getattr(stack, "memory", None)
            if store is not None and hasattr(store, "conversation_index_count"):
                mem["memory"] = int(store.conversation_index_count(conversation_id))
        except Exception as e:
            logger.debug("count_memory_failed", error=str(e))
        # Vector tag listing SCANs the index. Skip it on the history GET path so
        # clicking a chat does not saturate Valkey while the thread is loading.

    if mem["memory"] > 0 and not hist:
        warning = (
            "This conversation has stored messages that cannot be decrypted "
            "with the current tenant encryption key. Start a new chat to continue."
        )
        logger.warning(
            "conversation_history_unreadable",
            conversation_id=conversation_id,
            index_count=mem["memory"],
        )

    return {
        "conversation_id": conversation_id,
        "history": hist,
        "counts": mem,
        "summary": None,
        "warning": warning,
    }


@motet.command(
    description="Delete a conversation: remove it from the registry and clear its scoped memory and vector entries.",
    timeout_seconds=30,
    required_capabilities=[WorkerCapability.MEMORY_OPERATIONS],
)
def conversation_clear(data: ClearConversationData) -> Dict[str, Any]:
    """
    Clear a conversation: remove from registry and clear memory/vector by conversation-scope tag.
    """
    motet = get_motet_context()
    conversation_id = data.conversation_id
    motet_id, tenant_id, principal_id = _require_conversation_identity("conversation_clear")

    # Issue #139: authorize before clear_by_tag (tenant-shared memory).
    authorize_conversation_access_sync(
        motet_id=motet_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        conversation_id=conversation_id,
        bind_if_unclaimed=False,
    )

    remove_conversation_sync(
        motet_id=motet_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        conversation_id=conversation_id,
    )
    delete_conversation_owner_sync(motet_id, tenant_id, conversation_id)

    cleared = {"memory": 0, "vector": 0}
    stack = motet.stack if motet else None
    if stack:
        from motet.core.memory.constants import CONVERSATION_SCOPE_TAG_PREFIX
        conv_tag = f"{CONVERSATION_SCOPE_TAG_PREFIX}{conversation_id}"
        try:
            if getattr(stack, "memory", None):
                cleared["memory"] = stack.memory.clear_by_tag(conv_tag)  # type: ignore[attr-defined]
        except Exception as e:
            logger.warning("clear_memory_failed", conversation_id=conversation_id, error=str(e))
        try:
            if getattr(stack, "vector", None):
                cleared["vector"] = stack.vector.delete_by_tag(conv_tag)  # type: ignore[attr-defined]
        except Exception as e:
            logger.warning("clear_vector_failed", conversation_id=conversation_id, error=str(e))

    return {"conversation_id": conversation_id, "cleared": cleared}


@motet.command(
    description="Register or touch a conversation in the registry so it exists and updated_at is current.",
    timeout_seconds=15,
    required_capabilities=[WorkerCapability.MEMORY_OPERATIONS],
)
def conversation_register(data: RegisterConversationData) -> Dict[str, Any]:
    """
    Register or touch a conversation in the registry (ensure it exists, update updated_at).
    """
    motet_id, tenant_id, principal_id = _require_conversation_identity("conversation_register")

    # Issue #139: do not let a foreign principal adopt another principal's id
    # into their registry list.
    authorize_conversation_access_sync(
        motet_id=motet_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        conversation_id=data.conversation_id,
        bind_if_unclaimed=True,
    )

    register_or_touch_conversation_sync(
        motet_id=motet_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        conversation_id=data.conversation_id,
        title=data.title,
        agent_id=data.agent_id,
        surface_id=data.surface_id,
    )
    return {"conversation_id": data.conversation_id, "registered": True}


@motet.command(
    description="Rename a conversation by updating its display title in the registry.",
    timeout_seconds=15,
    required_capabilities=[WorkerCapability.MEMORY_OPERATIONS],
)
def conversation_rename(data: UpdateConversationTitleData) -> Dict[str, Any]:
    """
    Rename a conversation: update its display title in the registry.
    """
    motet_id, tenant_id, principal_id = _require_conversation_identity("conversation_rename")

    authorize_conversation_access_sync(
        motet_id=motet_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        conversation_id=data.conversation_id,
        bind_if_unclaimed=False,
    )

    register_or_touch_conversation_sync(
        motet_id=motet_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        conversation_id=data.conversation_id,
        title=data.title or "New Chat",
    )
    return {"conversation_id": data.conversation_id, "title": data.title or "New Chat"}
