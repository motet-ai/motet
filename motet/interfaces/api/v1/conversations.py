"""
Motet - Conversations API

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Conversations API for the Motet. Provides REST endpoints
    for listing, retrieving, and clearing conversations for a principal in a tenant.
    The API is a thin layer: it invokes distributed conversation commands via the
    global invoker and maps command results to HTTP responses. Get detail includes
    an optional warning when stored transcript rows cannot be decrypted.

Dependencies:
    - fastapi: Web framework for REST API
    - motet.core.commands.builtin.conversation: conversations_list, conversation_get, conversation_clear, conversation_register
    - motet.core.workers: global_invoker

Usage:
    from motet.interfaces.api.v1.conversations import router
    app.include_router(router)

Notes:
    - List/get/clear delegate to conversation commands.
    - Commands run on workers with MEMORY_OPERATIONS capability.
    - List rejects unknown surface_id values and agents the principal
      cannot see.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..shared.auth import get_current_principal
from ..shared.identity import get_principal_context
from ....core.conversations.ownership import (
    ACCESS_DENIED_MESSAGE,
    is_conversation_access_denied,
)
from ....core.types import Principal

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


def _enforce_list_scope(
    agent_id: Optional[str],
    surface_id: Optional[str],
    principal: Principal,
) -> tuple[str, Optional[str]]:
    """Validate agent visibility and surface catalog membership for list filters.

    Returns the qualified agent id and the validated surface id (or None).
    """
    from motet.core.agents import (
        get_agent_registry,
        principal_may_access_agent,
        resolve_agent_id,
    )

    qualified_id = resolve_agent_id(agent_id)
    agent_config = get_agent_registry().get(qualified_id)
    if agent_config is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {qualified_id}")
    if not principal_may_access_agent(agent_config, list(principal.roles or [])):
        raise HTTPException(
            status_code=403,
            detail=f"Not authorized to list conversations for agent '{qualified_id}'",
        )

    if not surface_id:
        return qualified_id, None

    from ..shared.surfaces import require_catalog_surface

    return qualified_id, require_catalog_surface(surface_id)


def _raise_if_conversation_access_denied(message: str) -> None:
    """Map ownership denials (issue #139) to HTTP 403."""
    if is_conversation_access_denied(message):
        raise HTTPException(status_code=403, detail=ACCESS_DENIED_MESSAGE)


def _distributed_command(
    factory: Callable[..., Any],
    *,
    task_id: str,
    conversation_id: str,
    data: BaseModel,
    motet_id: str,
    tenant_id: str,
    principal_id: str,
) -> Any:
    """Instantiate a command from a @distributed_command wrapper (extra kwargs are not on the inner callable's type)."""
    return factory(
        task_id=task_id,
        conversation_id=conversation_id,
        data=data,
        motet_id=motet_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
    )


class ConversationItem(BaseModel):
    """Single conversation in list response, with optional agent_id and surface_id."""
    id: str = Field(..., description="Conversation ID", json_schema_extra={"example": "conv-123"})
    title: str = Field(..., description="Display title", json_schema_extra={"example": "New Chat"})
    created_at: float = Field(..., description="Creation timestamp (Unix)")
    updated_at: float = Field(..., description="Last update timestamp (Unix)")
    agent_id: Optional[str] = Field(None, description="Agent that owns this conversation (e.g. core.default)")
    surface_id: Optional[str] = Field(None, description="Surface/channel (e.g. demo_chat, ops_dashboard)")


class ConversationListResponse(BaseModel):
    """Response model for conversation list."""
    conversations: List[ConversationItem] = Field(
        ...,
        description="List of conversations for the principal in the tenant",
        json_schema_extra={"example": [{"id": "conv-123", "title": "New Chat", "created_at": 1700000000.0, "updated_at": 1700000000.0}]},
    )


class ConversationHistoryAttachment(BaseModel):
    """Single attachment on a history message (matches transcript replay / multimodal storage)."""

    model_config = ConfigDict(extra="ignore")

    artifact_id: str = Field(..., description="Artifact identifier", json_schema_extra={"example": "art-uuid-1"})
    content_type: str = Field(
        ...,
        description="MIME type",
        json_schema_extra={"example": "image/png"},
    )
    filename: str = Field(
        ...,
        description="Original filename",
        json_schema_extra={"example": "screenshot.png"},
    )
    size_bytes: int = Field(
        default=0,
        validation_alias="bytes",
        serialization_alias="bytes",
        description="Size in bytes",
        json_schema_extra={"example": 1024},
    )


class ConversationHistoryMessage(BaseModel):
    """One message in conversation history (canonical transcript replay shape)."""

    model_config = ConfigDict(extra="ignore")

    content: str = Field(
        default="",
        description="Message body text",
        json_schema_extra={"example": "Hello"},
    )
    role: str = Field(
        ...,
        description="Message role (user|assistant|tool|system)",
        json_schema_extra={"example": "user"},
    )
    created_at: str = Field(
        ...,
        description="ISO 8601 timestamp when the message was stored",
        json_schema_extra={"example": "2026-03-22T12:00:00"},
    )
    attachments: Optional[List[ConversationHistoryAttachment]] = Field(
        default=None,
        description="Optional multimodal attachments",
    )
    agent_id: Optional[str] = Field(
        default=None,
        description="Qualified registry id of the agent that authored this message",
        json_schema_extra={"example": "core.default"},
    )
    parent_agent_id: Optional[str] = Field(
        default=None,
        description=(
            "Qualified registry id of the agent that started this loop when the author is nested"
        ),
        json_schema_extra={"example": "core.default"},
    )


def _coerce_conversation_history(raw: Any) -> List[ConversationHistoryMessage]:
    """Map command payload dicts to typed history items for OpenAPI and validation."""
    if not isinstance(raw, list):
        return []
    out: List[ConversationHistoryMessage] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            out.append(ConversationHistoryMessage.model_validate(item))
        except Exception as e:
            logger.debug(
                "conversation_history_item_coerce_failed",
                error=str(e),
                keys=list(item.keys()),
            )
    return out


class ConversationDetailResponse(BaseModel):
    """Response model for conversation details."""
    conversation_id: str = Field(..., description="Conversation ID", json_schema_extra={"example": "conv-123"})
    history: List[ConversationHistoryMessage] = Field(
        ...,
        description="Conversation history from canonical transcript memories; entries may include agent_id",
    )
    counts: Dict[str, int] = Field(
        ...,
        description="Memory and vector counts",
        json_schema_extra={"example": {"memory": 10, "vector": 5}},
    )
    summary: Optional[str] = Field(
        None,
        description="Rolling summary if enabled",
        json_schema_extra={"example": "User discussed AI topics..."},
    )
    warning: Optional[str] = Field(
        None,
        description="Set when stored transcript rows exist but cannot be decrypted",
        json_schema_extra={"example": "This conversation has stored messages that cannot be decrypted."},
    )


class ConversationClearResponse(BaseModel):
    """Response model for conversation clear operation."""
    conversation_id: str = Field(..., description="Cleared conversation ID", json_schema_extra={"example": "conv-123"})
    cleared: Dict[str, int] = Field(
        ...,
        description="Counts of cleared items",
        json_schema_extra={"example": {"memory": 10, "vector": 5}},
    )


class ConversationRenameRequest(BaseModel):
    """Request body for renaming a conversation."""
    title: str = Field(..., description="New display title", json_schema_extra={"example": "My Chat"}, min_length=1)


class ConversationRenameResponse(BaseModel):
    """Response model for conversation rename operation."""
    conversation_id: str = Field(..., description="Conversation ID", json_schema_extra={"example": "conv-123"})
    title: str = Field(..., description="Updated display title", json_schema_extra={"example": "My Chat"})


@router.get(
    "",
    summary="List conversations",
    description="Get list of conversations for the current principal in the tenant",
    response_model=ConversationListResponse,
    response_description="List of conversations with id, title, timestamps, optional agent_id/surface_id",
    responses={
        200: {"description": "Conversation list returned"},
        400: {"description": "Invalid or unknown surface_id"},
        403: {"description": "Not authorized to list the requested agent"},
        404: {"description": "Unknown agent_id"},
    },
)
async def list_conversations(
    request: Request,
    principal: Principal = Depends(get_current_principal),
    agent_id: Optional[str] = Query(None, description="Filter by agent (e.g. core.default). Omitted defaults to core.default."),
    surface_id: Optional[str] = Query(None, description="Filter by surface (e.g. demo_chat, ops_dashboard). Omitted returns all surfaces for agent."),
):
    """
    List all conversations for the principal in the tenant.
    Optional agent_id and surface_id filter; response includes scope fields when present.
    """
    try:
        from motet.core.commands.builtin.conversation import conversations_list as conversations_list_cmd
        from motet.core.commands.command_data_classes import ListConversationsData
        from ....core.workers import global_invoker

        qualified_agent_id, validated_surface_id = _enforce_list_scope(
            agent_id, surface_id, principal
        )

        global_invoker.initialize()
        motet_id, tenant_id, principal_id = get_principal_context(principal)
        command = _distributed_command(
            conversations_list_cmd,
            task_id=str(uuid4()),
            conversation_id="",
            data=ListConversationsData(
                limit=100,
                agent_id=qualified_agent_id,
                surface_id=validated_surface_id,
            ),
            motet_id=motet_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
        )
        result = await asyncio.to_thread(global_invoker.execute_command, command)

        # Accept both shapes: inner ADR-0029 (status/data) or outer invoker envelope (status='completed', result=ADR-0029)
        payload = result.get("result") if result.get("status") == "completed" else result
        if not isinstance(payload, dict):
            payload = {}

        if payload.get("status") == "error" or result.get("status") == "error":
            err = payload.get("error") or result.get("error") or {}
            msg = err.get("message", "Failed to list conversations") if isinstance(err, dict) else str(err)
            logger.warning("list_conversations_command_failed", error=msg)
            return ConversationListResponse(conversations=[])

        data = payload.get("data") or {}
        raw = data.get("conversations") or []
        conversations = [
            ConversationItem(
                id=c.get("id", ""),
                title=c.get("title", "New Chat"),
                created_at=float(c.get("created_at", 0)),
                updated_at=float(c.get("updated_at", 0)),
                agent_id=c.get("agent_id"),
                surface_id=c.get("surface_id"),
            )
            for c in raw
        ]
        return ConversationListResponse(conversations=conversations)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("list_conversations_failed", error=str(e), exc_info=True)
        return ConversationListResponse(conversations=[])


@router.get(
    "/{conversation_id}",
    summary="Get conversation details",
    description=(
        'Get conversation history and counts for a specific conversation. Each history message may include **agent_id** (qualified registry id) for the authoring agent and **parent_agent_id** when the author is a nested loop.'
    ),
    response_model=ConversationDetailResponse,
    response_description="Conversation details with typed history messages and counts",
)
async def get_conversation(
    request: Request,
    conversation_id: str,
    principal: Principal = Depends(get_current_principal),
):
    """
    Get conversation details: history and counts via conversation_get command.
    Auth: same as chat (JWT, X-API-Key, or service account).
    Returns empty history when workers are unavailable (no 500) so the UI keeps working.

    History items are typed in OpenAPI: each message may include **agent_id** when the canonical
    transcript recorded the authoring agent, and **parent_agent_id** when the author
    is a nested loop.
    """
    try:
        from motet.core.commands.builtin.conversation import conversation_get as conversation_get_cmd
        from motet.core.commands.command_data_classes import GetConversationData
        from ....core.workers import global_invoker

        global_invoker.initialize()
        motet_id, tenant_id, principal_id = get_principal_context(principal)
        command = _distributed_command(
            conversation_get_cmd,
            task_id=str(uuid4()),
            conversation_id=conversation_id,
            data=GetConversationData(conversation_id=conversation_id),
            motet_id=motet_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
        )
        result = await asyncio.to_thread(global_invoker.execute_command, command)

        # Unwrap invoker envelope if present (status='completed', result=ADR-0029)
        payload = result.get("result") if result.get("status") == "completed" else result
        if not isinstance(payload, dict):
            payload = {}
        if payload.get("status") == "error" or result.get("status") == "error":
            err = payload.get("error") or result.get("error") or {}
            msg = err.get("message", "Failed to get conversation") if isinstance(err, dict) else str(err)
            _raise_if_conversation_access_denied(msg)
            logger.warning("get_conversation_command_failed", conversation_id=conversation_id, error=msg)
            return ConversationDetailResponse(
                conversation_id=conversation_id,
                history=[],
                counts={"memory": 0, "vector": 0},
                summary=None,
                warning=None,
            )

        data = payload.get("data") or {}
        return ConversationDetailResponse(
            conversation_id=data.get("conversation_id", conversation_id),
            history=_coerce_conversation_history(data.get("history")),
            counts=data.get("counts") or {"memory": 0, "vector": 0},
            summary=data.get("summary"),
            warning=data.get("warning"),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("get_conversation_failed", conversation_id=conversation_id, error=str(e), exc_info=True)
        return ConversationDetailResponse(
            conversation_id=conversation_id,
            history=[],
            counts={"memory": 0, "vector": 0},
            summary=None,
            warning=None,
        )


@router.post(
    "/{conversation_id}/clear",
    summary="Clear conversation",
    description="Clear conversation from registry and associated memories/vectors",
    response_model=ConversationClearResponse,
    response_description="Clear operation results",
)
async def clear_conversation(
    conversation_id: str,
    principal: Principal = Depends(get_current_principal),
):
    """
    Clear a conversation via conversation_clear command (registry + memory/vector).
    Auth: same as chat (JWT, X-API-Key, or service account).
    """
    try:
        from motet.core.commands.builtin.conversation import conversation_clear as conversation_clear_cmd
        from motet.core.commands.command_data_classes import ClearConversationData
        from ....core.workers import global_invoker

        global_invoker.initialize()
        motet_id, tenant_id, principal_id = get_principal_context(principal)
        command = _distributed_command(
            conversation_clear_cmd,
            task_id=str(uuid4()),
            conversation_id=conversation_id,
            data=ClearConversationData(conversation_id=conversation_id),
            motet_id=motet_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
        )
        result = await asyncio.to_thread(global_invoker.execute_command, command)

        payload = result.get("result") if result.get("status") == "completed" else result
        if not isinstance(payload, dict):
            payload = {}
        if payload.get("status") == "error" or result.get("status") == "error":
            err = payload.get("error") or result.get("error") or {}
            msg = err.get("message", "Failed to clear conversation") if isinstance(err, dict) else str(err)
            _raise_if_conversation_access_denied(msg)
            logger.warning("clear_conversation_command_failed", conversation_id=conversation_id, error=msg)
            raise HTTPException(status_code=500, detail=msg)

        data = payload.get("data") or {}
        return ConversationClearResponse(
            conversation_id=data.get("conversation_id", conversation_id),
            cleared=data.get("cleared") or {"memory": 0, "vector": 0},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("clear_conversation_failed", conversation_id=conversation_id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to clear conversation: {str(e)}") from e


@router.patch(
    "/{conversation_id}",
    summary="Rename conversation",
    description="Update the display title of a conversation",
    response_model=ConversationRenameResponse,
    response_description="Updated conversation id and title",
)
async def rename_conversation(
    conversation_id: str,
    body: ConversationRenameRequest,
    principal: Principal = Depends(get_current_principal),
):
    """
    Rename a conversation via conversation_rename command.
    Auth: same as chat (JWT, X-API-Key, or service account).
    """
    try:
        from motet.core.commands.builtin.conversation import conversation_rename as conversation_rename_cmd
        from motet.core.commands.command_data_classes import UpdateConversationTitleData
        from ....core.workers import global_invoker

        global_invoker.initialize()
        motet_id, tenant_id, principal_id = get_principal_context(principal)
        command = _distributed_command(
            conversation_rename_cmd,
            task_id=str(uuid4()),
            conversation_id=conversation_id,
            data=UpdateConversationTitleData(conversation_id=conversation_id, title=body.title.strip()),
            motet_id=motet_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
        )
        result = await asyncio.to_thread(global_invoker.execute_command, command)

        payload = result.get("result") if result.get("status") == "completed" else result
        if not isinstance(payload, dict):
            payload = {}
        if payload.get("status") == "error" or result.get("status") == "error":
            err = payload.get("error") or result.get("error") or {}
            msg = err.get("message", "Failed to rename conversation") if isinstance(err, dict) else str(err)
            _raise_if_conversation_access_denied(msg)
            logger.warning("rename_conversation_command_failed", conversation_id=conversation_id, error=msg)
            raise HTTPException(status_code=500, detail=msg)

        data = payload.get("data") or {}
        return ConversationRenameResponse(
            conversation_id=data.get("conversation_id", conversation_id),
            title=data.get("title", body.title.strip()),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("rename_conversation_failed", conversation_id=conversation_id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to rename conversation: {str(e)}") from e


@router.delete(
    "/{conversation_id}",
    summary="Delete conversation",
    description="Remove conversation from registry and clear associated memories/vectors",
    response_model=ConversationClearResponse,
    response_description="Deleted conversation id and cleared counts",
)
async def delete_conversation(
    conversation_id: str,
    principal: Principal = Depends(get_current_principal),
):
    """
    Delete a conversation (same as clear): registry + memory/vector via conversation_clear command.
    Auth: same as chat (JWT, X-API-Key, or service account).
    """
    try:
        from motet.core.commands.builtin.conversation import conversation_clear as conversation_clear_cmd
        from motet.core.commands.command_data_classes import ClearConversationData
        from ....core.workers import global_invoker

        global_invoker.initialize()
        motet_id, tenant_id, principal_id = get_principal_context(principal)
        command = _distributed_command(
            conversation_clear_cmd,
            task_id=str(uuid4()),
            conversation_id=conversation_id,
            data=ClearConversationData(conversation_id=conversation_id),
            motet_id=motet_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
        )
        result = await asyncio.to_thread(global_invoker.execute_command, command)

        payload = result.get("result") if result.get("status") == "completed" else result
        if not isinstance(payload, dict):
            payload = {}
        if payload.get("status") == "error" or result.get("status") == "error":
            err = payload.get("error") or result.get("error") or {}
            msg = err.get("message", "Failed to delete conversation") if isinstance(err, dict) else str(err)
            _raise_if_conversation_access_denied(msg)
            logger.warning("delete_conversation_command_failed", conversation_id=conversation_id, error=msg)
            raise HTTPException(status_code=500, detail=msg)

        data = payload.get("data") or {}
        return ConversationClearResponse(
            conversation_id=data.get("conversation_id", conversation_id),
            cleared=data.get("cleared") or {"memory": 0, "vector": 0},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("delete_conversation_failed", conversation_id=conversation_id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to delete conversation: {str(e)}") from e
