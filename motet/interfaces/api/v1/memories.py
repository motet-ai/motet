"""
Motet - Memories API

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Memory management API for the Motet distributed framework.
    Provides REST API endpoints for listing, browsing, finding, tagging,
    forgetting, inspecting, clearing, and consolidating memories. Store uses
    ``core.memory_store``. Find/tag/forget call MemoryManager. Browse decrypts
    the newest window; stats totals use the Redis index. Semantic search uses
    ``core.memory_recall``.

Dependencies:
    - fastapi: Web framework for REST API
    - motet.core: Memory system and MotetStack

Usage:
    from motet.interfaces.api.v1.memories import router
    
    # Include in FastAPI app
    app.include_router(router)

Notes:
    - Provides memory CRUD and management operations
    - Manage-app browse/stats/clear honor tenant_id and motet_id query params
    - Browse accepts an agent filter (qualified id, short name, or agent: tag)
    - Browse default and stats sample use COLLECT_DEFAULT_LIMIT; max browse is 5000
    - Index zsets supply totals
    - Forget deletes KV rows and matching vector documents
    - Supports tenant isolation for multi-tenant deployments
"""

import asyncio
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, cast
from fastapi import APIRouter, HTTPException, Header, Body, Request, Query, Depends
from pydantic import BaseModel, Field
import structlog
import uuid as _uuid

from ..shared import (
    apply_principal_to_config,
    attach_principal_to_stack,
    get_current_principal,
    get_principal_context,
)
from ..shared.auth import (
    can_access_all_tenants,
    require_motet_access,
    require_tenant_access,
)
from ..shared.memory_ops import (
    BROWSE_MAX_LIMIT,
    COLLECT_DEFAULT_LIMIT,
    clear_scoped_memory_stores,
    collect_memories_for_scope,
    compute_memory_stats,
    count_memory_index,
    filter_memories,
    memory_created_at_sort_key,
)
from ..shared.scope import ManageAppScope, get_manage_app_scope
from ....core import MotetStack
from ....core.config import Config
from ....core.types import Principal, MemoryItem, serialize_memory_items

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/memories", tags=["memories"])


def _config_for_principal(principal: Principal) -> Config:
    cfg = Config()
    apply_principal_to_config(cfg, principal)
    return cfg


def _stack_for_principal(principal: Principal) -> tuple[Config, MotetStack]:
    cfg = _config_for_principal(principal)
    stack = MotetStack(cfg)
    attach_principal_to_stack(stack, principal)
    return cfg, stack


def _unwrap_command_invoker_payload(result: Any) -> Dict[str, Any]:
    """Normalize ``global_invoker.execute_command`` return value to an ADR-0029 payload dict."""
    if not isinstance(result, dict):
        return {}
    payload = result.get("result") if result.get("status") == "completed" else result
    return payload if isinstance(payload, dict) else {}


async def _memory_search_items(
    *,
    query: str,
    top_k: int,
    tags: Optional[List[str]],
    principal: Principal,
) -> List[Dict[str, Any]]:
    """
    Run ``core.memory_recall`` in semantic mode on workers (query embedding + KNN on the worker).

    Raises:
        RuntimeError: Command failed or returned an error payload.
    """
    from motet.core.commands.command_data_classes import MemoryRecallData
    from motet.core.commands.builtin.memory import memory_recall as memory_recall_cmd
    from ....core.workers import global_invoker

    global_invoker.initialize()
    motet_id, tenant_id, principal_id = get_principal_context(principal)
    _factory = cast(Callable[..., Any], memory_recall_cmd)
    command = _factory(
        task_id=str(_uuid.uuid4()),
        conversation_id="",
        data=MemoryRecallData(query=query, limit=top_k, tags=tags, mode="semantic"),
        motet_id=motet_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
    )
    result = await asyncio.to_thread(global_invoker.execute_command, command)
    payload = _unwrap_command_invoker_payload(result)
    if payload.get("status") == "error" or (
        isinstance(result, dict) and result.get("status") == "error"
    ):
        err = payload.get("error") or (result.get("error") if isinstance(result, dict) else {}) or {}
        msg = err.get("message", "Memory search failed") if isinstance(err, dict) else str(err)
        raise RuntimeError(msg)
    if payload.get("status") != "success":
        raise RuntimeError("Memory search failed")
    data = payload.get("data") or {}
    items = data.get("items") or []
    return list(items) if isinstance(items, list) else []


def _memory_identity_for_principal(
    principal: Principal,
    *,
    conversation_id: Optional[str] = None,
) -> SimpleNamespace:
    """Identity context for MemoryManager.recall / retag."""
    motet_id, tenant_id, principal_id = get_principal_context(principal)
    return SimpleNamespace(
        motet_id=str(motet_id).strip(),
        tenant_id=str(tenant_id).strip(),
        principal_id=str(principal_id).strip(),
        conversation_id=conversation_id,
    )


def _resolve_manage_memory_scope(
    principal: Principal,
    scope: ManageAppScope,
) -> tuple[Optional[str], Optional[str]]:
    """Authorize manage-app tenant/motet filters. ``None`` means all (global callers)."""
    if scope.tenant_id:
        tenant_id: Optional[str] = require_tenant_access(principal, scope.tenant_id)
    elif can_access_all_tenants(principal):
        tenant_id = None
    else:
        tenant_id = principal.tenant_id or "default"

    if scope.motet_id:
        motet_id: Optional[str] = require_motet_access(principal, scope.motet_id)
    elif can_access_all_tenants(principal):
        motet_id = None
    else:
        motet_id = principal.motet_id or "default"

    return tenant_id, motet_id


class MemoryStoreRequest(BaseModel):
    """Request body for storing a memory via ``core.memory_store``."""

    content: str = Field(
        ...,
        min_length=1,
        description="Plain text content to store.",
        json_schema_extra={"example": "Quarterly goals: expand retrieval eval coverage."},
    )
    type: str = Field(
        default="note",
        description="Memory type (e.g. note, conversation_turn, summary).",
        json_schema_extra={"example": "note"},
    )
    tags: Optional[List[str]] = Field(
        default=None,
        description="Optional tags for filtering and retrieval.",
        json_schema_extra={"example": ["docs", "imported"]},
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional metadata key/value pairs (e.g. source path).",
        json_schema_extra={"example": {"source": "/path/to/file.txt"}},
    )
    scope_type: Optional[str] = Field(
        default=None,
        description="Scope: global, principal, conversation, or task.",
        json_schema_extra={"example": "global"},
    )
    long_term: Optional[bool] = Field(
        default=None,
        description=(
            "If true, prefer long-term vector indexing; if null, memory-type heuristics apply."
        ),
    )
    conversation_id: Optional[str] = Field(
        default=None,
        description="Conversation id for command context and default conversation scope.",
        json_schema_extra={"example": "conv-123"},
    )


class MemoryStoreResponse(BaseModel):
    """Response from a successful memory store operation."""

    memory_id: Optional[str] = Field(
        default=None,
        description="Identifier of the stored memory row.",
        json_schema_extra={"example": "mem-uuid"},
    )
    stored: bool = Field(
        default=True,
        description="Whether the memory was persisted.",
        json_schema_extra={"example": True},
    )


class MemoryFindRequest(BaseModel):
    """Request model for finding memories by tags."""
    tags: List[str] = Field(
        default_factory=list,
        description="Tags to filter by",
        json_schema_extra={"example": ["entity:user123", "type:conversation"]}
    )
    match: str = Field(
        default="any",
        description="Match mode: 'any' or 'all'",
        json_schema_extra={"example": "any"}
    )
    limit: int = Field(
        default=5,
        description="Maximum number of results",
        json_schema_extra={"example": 10}
    )
    conversation_id: Optional[str] = Field(
        None,
        description="Filter by conversation ID (scope to memories in this conversation)",
        json_schema_extra={"example": "conv-123"}
    )
    types: Optional[List[str]] = Field(
        None,
        description="Filter by memory types",
        json_schema_extra={"example": ["conversation", "summary"]}
    )
    include_vector: bool = Field(
        default=False,
        description="Include vector embeddings in results",
        json_schema_extra={"example": False}
    )
    scope: Optional[str] = Field(
        None,
        description="Memory scope: wm, stm, ltm, or both",
        json_schema_extra={"example": "ltm"}
    )


class MemoryTagRequest(BaseModel):
    """Request model for tagging memories."""
    memory_ids: Optional[List[str]] = Field(
        None,
        description="Specific memory IDs to tag",
        json_schema_extra={"example": ["mem-123", "mem-456"]}
    )
    conversation_id: Optional[str] = Field(
        None,
        description="Scope to memories in this conversation (tag all in this conversation)",
        json_schema_extra={"example": "conv-123"}
    )
    filter_tag: Optional[str] = Field(
        None,
        description="Filter memories by existing tag",
        json_schema_extra={"example": "type:conversation"}
    )
    tags: List[str] = Field(
        ...,
        description="Tags to add or remove",
        json_schema_extra={"example": ["important", "reviewed"]}
    )
    op: str = Field(
        default="add",
        description="Operation: 'add' or 'remove'",
        json_schema_extra={"example": "add"}
    )


class MemoryForgetRequest(BaseModel):
    """Request body for targeted forget (KV + vector)."""

    memory_ids: Optional[List[str]] = Field(
        None,
        description="Specific memory IDs to delete",
        json_schema_extra={"example": ["mem-123", "mem-456"]},
    )
    conversation_id: Optional[str] = Field(
        None,
        description="Forget memories in this conversation (intersects with filter_tag when both set)",
        json_schema_extra={"example": "conv-123"},
    )
    filter_tag: Optional[str] = Field(
        None,
        description="Forget memories that already have this tag",
        json_schema_extra={"example": "temporary"},
    )
    tenant_id: Optional[str] = Field(
        None,
        description="Tenant store to forget from. Admin callers may set another tenant.",
        json_schema_extra={"example": "acme"},
    )
    motet_id: Optional[str] = Field(
        None,
        description="Motet store to forget from. Admin callers may set another motet.",
        json_schema_extra={"example": "production"},
    )


class MemoryBrowseResponse(BaseModel):
    """Paginated memory browse result for the manage app."""

    items: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Memory rows for this page, newest first",
    )
    total: int = Field(
        ...,
        description="Matching rows in the newest decrypted window after filters",
    )
    limit: int = Field(..., description="Page size applied to this response")
    offset: int = Field(..., description="Number of matching rows skipped")
    query: Optional[str] = Field(
        None,
        description="Contains query applied to content and tags, if any",
    )


class MemoryStatsResponse(BaseModel):
    """Aggregated memory counts for the manage-app Memory page."""

    total_memories: int = Field(
        ...,
        description="Index member count for the authorized scope",
    )
    last_24h: int = Field(
        ...,
        description="Index members scored in the last 24 hours",
    )
    memory_types: int = Field(..., description="Distinct memory type count in the recent sample")
    tagged_count: int = Field(..., description="Sampled memories that have at least one tag")
    type_breakdown: Dict[str, int] = Field(
        default_factory=dict,
        description="Count by memory type in the recent sample",
    )
    tier_breakdown: Dict[str, int] = Field(
        default_factory=dict,
        description="Count by tier tag (wm, stm, ltm, untagged) in the recent sample",
    )
    scope_breakdown: Dict[str, int] = Field(
        default_factory=dict,
        description="Count by scope_type in the recent sample",
    )
    motet_breakdown: Dict[str, int] = Field(
        default_factory=dict,
        description="Count by motet_id in the recent sample",
    )
    tenant_breakdown: Dict[str, int] = Field(
        default_factory=dict,
        description="Count by tenant_id in the recent sample",
    )
    agent_breakdown: Dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Count by agent id in the recent sample "
            "(metadata.agent_id or agent: tag; unattributed if none)"
        ),
    )
    vector_enabled: bool = Field(
        ...,
        description="Whether MOTET_ENABLE_VECTOR_MEMORY is on for this process",
    )
    error: Optional[str] = Field(
        None,
        description="Error message when collection failed",
    )


@router.get(
    "",
    summary="List recent memories",
    description="Get list of recent memories with optional filtering",
    response_description="List of memory objects"
)
async def list_memories(
    limit: int = 10,
    tag: Optional[str] = None,
    entity: Optional[str] = None,
    principal: Principal = Depends(get_current_principal)
):
    """
    List recent memories.
    
    Returns the most recent memories with optional filtering by tag or entity.
    Entity filter is a shorthand for 'entity:' tag filtering.
    
    Supports tenant isolation - automatically filters by tenant if configured.
    
    Args:
        limit: Maximum number of memories to return (default: 10)
        tag: Optional tag to filter by (e.g., "type:conversation")
        entity: Optional entity to filter by (converted to "entity:{entity}" tag)
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        List of memory objects
        
    Raises:
        HTTPException: 401 if authentication fails
    """
    
    try:
        cfg, stack = _stack_for_principal(principal)
        
        if not stack.memory:
            return []
        
        # Support entity filter as shorthand for entity: tags
        if entity:
            tag = f"entity:{entity}"
        
        items = stack.memory.recent(limit=limit, tag=tag)
        
        # Enforce tenant filter if configured
        try:
            if getattr(cfg, "tenant_enforce_memory_filter", False):
                tenant_id = principal.tenant_id
                if tenant_id:
                    ttag = f"tenant:{tenant_id}"
                    items = [m for m in items if ttag in (m.tags or [])]
        except Exception as e:
            logger.debug("Tenant filter failed", error=str(e))
        
        return [m.model_dump() for m in items]
        
    except Exception as e:
        logger.error("Failed to list memories", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list memories: {str(e)}")


@router.get(
    "/browse",
    summary="Browse memories for the manage app",
    description=(
        "List the newest memories with contains, type, tag, tier, agent, and conversation "
        "filters. Decrypts a recent window rather than the full store. Honors the "
        "manage-app tenant/motet selector."
    ),
    response_model=MemoryBrowseResponse,
    response_description="Paginated memory rows",
    responses={
        200: {"description": "Browse page"},
        403: {"description": "Caller cannot access the requested tenant or motet"},
        500: {"description": "Browse failed"},
    },
)
async def browse_memories(
    q: Optional[str] = Query(None, description="Case-insensitive substring match on content or tags"),
    type: Optional[str] = Query(None, description="Exact memory type filter"),
    tag: Optional[str] = Query(None, description="Require this tag"),
    tier: Optional[str] = Query(None, description="Tier filter: wm, stm, or ltm"),
    conversation_id: Optional[str] = Query(None, description="Exact conversation id"),
    agent: Optional[str] = Query(
        None,
        description="Agent filter: qualified id (core.default), short name (default), or agent: tag",
    ),
    limit: int = Query(
        COLLECT_DEFAULT_LIMIT,
        ge=1,
        le=BROWSE_MAX_LIMIT,
        description="Newest rows to decrypt and return",
    ),
    offset: int = Query(0, ge=0, description="Rows to skip after filtering"),
    scope: ManageAppScope = Depends(get_manage_app_scope),
    principal: Principal = Depends(get_current_principal),
) -> MemoryBrowseResponse:
    """Browse collected memories for the authenticated (and optionally scoped) caller."""
    try:
        tenant_id, motet_id = _resolve_manage_memory_scope(principal, scope)
        cfg, stack = _stack_for_principal(principal)
        if not getattr(stack, "memory", None):
            return MemoryBrowseResponse(items=[], total=0, limit=limit, offset=offset, query=q)

        window = min(max(offset + limit, limit), BROWSE_MAX_LIMIT)
        collected = await asyncio.to_thread(
            collect_memories_for_scope,
            stack,
            tenant_id,
            motet_id,
            window,
        )
        filtered = filter_memories(
            collected,
            query=q,
            memory_type=type,
            conversation_id=conversation_id,
            tag=tag,
            tier=tier,
            agent=agent,
            wm_tag=getattr(cfg, "memory_working_tag", "wm"),
            stm_tag=getattr(cfg, "memory_short_term_tag", "stm"),
            ltm_tag=getattr(cfg, "memory_long_term_tag", "ltm"),
            agent_tag_prefix=getattr(cfg, "memory_agent_tag_prefix", "agent:"),
        )
        filtered.sort(key=memory_created_at_sort_key, reverse=True)
        page = filtered[offset : offset + limit]
        return MemoryBrowseResponse(
            items=serialize_memory_items(page),
            total=len(filtered),
            limit=limit,
            offset=offset,
            query=q,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to browse memories", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to browse memories: {str(e)}")


@router.get(
    "/stats",
    summary="Memory statistics for the manage app",
    description=(
        "Totals and last-24h come from the Redis index. Type/tier/scope/agent "
        "breakdowns use a recent decrypted sample. Honors the manage-app tenant/motet selector."
    ),
    response_model=MemoryStatsResponse,
    response_description="Aggregated memory statistics",
    responses={
        200: {"description": "Statistics"},
        403: {"description": "Caller cannot access the requested tenant or motet"},
    },
)
async def memories_stats(
    scope: ManageAppScope = Depends(get_manage_app_scope),
    principal: Principal = Depends(get_current_principal),
) -> MemoryStatsResponse:
    """Return manage-app memory statistics for the authorized scope."""
    try:
        tenant_id, motet_id = _resolve_manage_memory_scope(principal, scope)
        cfg, stack = _stack_for_principal(principal)
        if not getattr(stack, "memory", None):
            return MemoryStatsResponse(
                total_memories=0,
                last_24h=0,
                memory_types=0,
                tagged_count=0,
                vector_enabled=bool(getattr(cfg, "enable_vector_memory", False)),
                error="Memory system not enabled",
            )
        collected = await asyncio.to_thread(
            collect_memories_for_scope,
            stack,
            tenant_id,
            motet_id,
            COLLECT_DEFAULT_LIMIT,
        )
        stats = compute_memory_stats(
            collected,
            vector_enabled=bool(getattr(cfg, "enable_vector_memory", False)),
            wm_tag=getattr(cfg, "memory_working_tag", "wm"),
            stm_tag=getattr(cfg, "memory_short_term_tag", "stm"),
            ltm_tag=getattr(cfg, "memory_long_term_tag", "ltm"),
            agent_tag_prefix=getattr(cfg, "memory_agent_tag_prefix", "agent:"),
        )
        index_total, index_last_24h = await asyncio.to_thread(
            count_memory_index,
            tenant_id,
            motet_id,
        )
        if index_total:
            stats["total_memories"] = index_total
            stats["last_24h"] = index_last_24h
        return MemoryStatsResponse(**stats)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to compute memory stats", error=str(e), exc_info=True)
        cfg = _config_for_principal(principal)
        return MemoryStatsResponse(
            total_memories=0,
            last_24h=0,
            memory_types=0,
            tagged_count=0,
            vector_enabled=bool(getattr(cfg, "enable_vector_memory", False)),
            error=str(e),
        )


def _parse_memory_store_command_result(execute_result: Any) -> MemoryStoreResponse:
    """Interpret ``global_invoker.execute_command`` return for ``memory_store``."""
    if not isinstance(execute_result, dict):
        raise HTTPException(status_code=500, detail="Invalid memory_store command result")
    d = execute_result
    if d.get("status") == "error":
        err = d.get("error") or {}
        msg = err.get("message", "memory_store failed") if isinstance(err, dict) else str(err)
        raise HTTPException(status_code=500, detail=msg)
    if d.get("status") == "success" and isinstance(d.get("data"), dict):
        data = d["data"]
        return MemoryStoreResponse(
            memory_id=data.get("memory_id"),
            stored=bool(data.get("stored", True)),
        )
    memory_id = d.get("memory_id")
    if memory_id is not None or d.get("stored") is not None:
        return MemoryStoreResponse(memory_id=memory_id, stored=bool(d.get("stored", True)))
    payload = _unwrap_command_invoker_payload(execute_result)
    if payload.get("status") == "error":
        err = payload.get("error") or {}
        msg = err.get("message", "memory_store failed") if isinstance(err, dict) else str(err)
        raise HTTPException(status_code=500, detail=msg)
    if payload.get("status") == "success" and isinstance(payload.get("data"), dict):
        data = payload["data"]
        return MemoryStoreResponse(
            memory_id=data.get("memory_id"),
            stored=bool(data.get("stored", True)),
        )
    raise HTTPException(status_code=500, detail="Unexpected memory_store response shape")


@router.post(
    "/store",
    summary="Store a memory",
    description=(
        "Persist text via distributed ``core.memory_store`` (KV + optional LTM vector indexing). "
        "Replaces the legacy local-only ingestion CLI for API-driven imports."
    ),
    response_model=MemoryStoreResponse,
    response_description="Stored memory id and status",
    responses={
        200: {"description": "Memory stored"},
        503: {"description": "Memory manager unavailable"},
        500: {"description": "Store failed"},
    },
)
async def store_memory(
    req: MemoryStoreRequest = Body(...),
    principal: Principal = Depends(get_current_principal),
):
    """Store one memory for the authenticated principal using ``core.memory_store``."""
    from motet.core.commands.command_data_classes import MemoryStoreData
    from motet.core.commands.builtin.memory import memory_store as memory_store_cmd

    try:
        _, stack = _stack_for_principal(principal)
        if not stack.memory:
            raise HTTPException(status_code=503, detail="Memory manager not available")

        from ....core.workers import global_invoker

        global_invoker.initialize()
        motet_id, tenant_id, principal_id = get_principal_context(principal)
        meta: Dict[str, Any] = dict(req.metadata or {})
        data = MemoryStoreData(
            content=req.content,
            type=req.type,
            tags=list(req.tags) if req.tags else [],
            metadata=meta,
            scope_type=req.scope_type,
            long_term=req.long_term,
        )
        conv = str(req.conversation_id).strip() if req.conversation_id else ""
        _factory = cast(Callable[..., Any], memory_store_cmd)
        command = _factory(
            task_id=str(_uuid.uuid4()),
            conversation_id=conv,
            data=data,
            motet_id=motet_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
        )
        try:
            raw = await asyncio.to_thread(global_invoker.execute_command, command)
        except RuntimeError as e:
            logger.error("memory_store command failed", error=str(e), exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e
        return _parse_memory_store_command_result(raw)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to store memory", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to store memory: {str(e)}") from e


@router.post(
    "/find",
    summary="Find memories by tags",
    description="Find memories using tag-based filtering with advanced options",
    response_description="Matching memories"
)
async def find_memories(
    req: MemoryFindRequest = Body(...),
    principal: Principal = Depends(get_current_principal)
):
    """Find memories by tag intersection via MemoryManager.recall."""
    try:
        cfg, stack = _stack_for_principal(principal)
        manager = getattr(stack, "memory_manager", None)
        if manager is None:
            raise HTTPException(status_code=503, detail="Memory manager not available")

        tags = list(req.tags or [])
        try:
            if getattr(cfg, "tenant_enforce_memory_filter", False):
                tenant_id = principal.tenant_id
                if tenant_id:
                    ttag = f"tenant:{tenant_id}"
                    if ttag not in tags:
                        tags.append(ttag)
        except Exception as e:
            logger.debug("Tenant filter failed", error=str(e))

        identity = _memory_identity_for_principal(
            principal, conversation_id=req.conversation_id
        )
        items = manager.recall(
            tags=tags,
            match=req.match,
            limit=req.limit,
            conversation_id=req.conversation_id,
            types=req.types,
            scope=req.scope or "both",
            include_vector=req.include_vector,
            motet_context=identity,
        )
        return {"items": serialize_memory_items(items)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to find memories", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to find memories: {str(e)}")


@router.post(
    "/tag",
    summary="Tag memories",
    description="Add or remove tags from memories",
    response_description="Tag operation result"
)
async def tag_memories(
    req: MemoryTagRequest = Body(...),
    principal: Principal = Depends(get_current_principal)
):
    """Tag memories via MemoryManager.retag. Requires ids or a conversation/tag filter."""
    try:
        cfg, stack = _stack_for_principal(principal)
        manager = getattr(stack, "memory_manager", None)
        if manager is None:
            raise HTTPException(status_code=503, detail="Memory manager not available")

        if not req.memory_ids and not req.conversation_id and not req.filter_tag:
            raise HTTPException(
                status_code=400,
                detail="memory_ids, conversation_id, or filter_tag is required",
            )

        tags = list(req.tags or [])
        try:
            if getattr(cfg, "tenant_enforce_memory_filter", False):
                tenant_id = principal.tenant_id
                if tenant_id and req.op == "add":
                    ttag = f"tenant:{tenant_id}"
                    if ttag not in tags:
                        tags.append(ttag)
        except Exception as e:
            logger.debug("Tenant tag addition failed", error=str(e))

        identity = _memory_identity_for_principal(
            principal, conversation_id=req.conversation_id
        )
        result = manager.retag(
            tags=tags,
            op=req.op,
            memory_ids=req.memory_ids,
            conversation_id=req.conversation_id,
            filter_tag=req.filter_tag,
            motet_context=identity,
        )
        return {
            "status": "success",
            "updated": int(result.get("updated") or 0),
            "ids": list(result.get("ids") or [])[:50],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to tag memories", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to tag memories: {str(e)}")


@router.post(
    "/forget",
    summary="Forget targeted memories",
    description=(
        "Delete specific memories from the KV store and matching vector documents. "
        "Requires memory_ids, conversation_id, or filter_tag."
    ),
    response_description="Forget operation result",
    responses={
        200: {"description": "Forget completed"},
        400: {"description": "No selector provided"},
        403: {"description": "Caller cannot access the requested tenant or motet"},
        503: {"description": "Memory manager unavailable"},
    },
)
async def forget_memories(
    req: MemoryForgetRequest = Body(...),
    principal: Principal = Depends(get_current_principal),
):
    """Forget memories via MemoryManager.forget. Requires ids or a conversation/tag filter."""
    try:
        if not req.memory_ids and not req.conversation_id and not req.filter_tag:
            raise HTTPException(
                status_code=400,
                detail="memory_ids, conversation_id, or filter_tag is required",
            )

        _, stack = _stack_for_principal(principal)
        manager = getattr(stack, "memory_manager", None)
        if manager is None:
            raise HTTPException(status_code=503, detail="Memory manager not available")

        p_motet_id, p_tenant_id, p_principal_id = get_principal_context(principal)
        tenant_id = require_tenant_access(principal, req.tenant_id, fallback=p_tenant_id)
        motet_id = require_motet_access(principal, req.motet_id, fallback=p_motet_id)
        identity = SimpleNamespace(
            motet_id=str(motet_id).strip(),
            tenant_id=str(tenant_id).strip(),
            principal_id=str(p_principal_id).strip(),
            conversation_id=req.conversation_id,
        )
        result = manager.forget(
            memory_ids=req.memory_ids,
            conversation_id=req.conversation_id,
            filter_tag=req.filter_tag,
            motet_context=identity,
        )
        return {
            "status": "success",
            "deleted": int(result.get("deleted") or 0),
            "ids": list(result.get("ids") or [])[:50],
            "vector_deleted": int(result.get("vector_deleted") or 0),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to forget memories", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to forget memories: {str(e)}")


@router.get(
    "/inspect",
    summary="Inspect memory system",
    description="Get detailed inspection of memory system state and statistics",
    response_description="Memory system inspection data"
)
async def memories_inspect(
    limit: int = 5,
    collection: Optional[str] = None,
    principal: Principal = Depends(get_current_principal)
):
    """
    Inspect memory system.
    
    Returns detailed statistics and recent examples from the memory system,
    including counts by type, collection, and entity tags.
    
    Useful for debugging and understanding memory system state.
    
    Args:
        limit: Number of recent examples to include (default: 5)
        collection: Optional collection filter
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        Dictionary with memory and vector store inspection data
        
    Raises:
        HTTPException: 401 if authentication fails
    """
    
    try:
        cfg, stack = _stack_for_principal(principal)
        
        info: Dict[str, Any] = {"memory": {}, "vector": {}}
        
        # In-memory/Redis
        if getattr(stack, "memory", None):
            try:
                # Get recent items
                if collection:
                    recent = stack.memory.recent(limit=100, tag=f"collection:{collection}")
                else:
                    recent = stack.memory.recent(limit=100)
                
                # Count by type
                by_type: Dict[str, int] = {}
                for it in recent:
                    by_type[it.type] = by_type.get(it.type, 0) + 1
                info["memory"]["counts_by_type"] = by_type
                info["memory"]["recent_examples"] = [m.model_dump() for m in recent[:limit]]
                
                # Collections summary if enabled
                try:
                    if getattr(cfg, "memory_collections_enabled", False):
                        by_collection: Dict[str, int] = {}
                        for it in recent:
                            for t in (it.tags or []):
                                if t.startswith("collection:"):
                                    by_collection[t] = by_collection.get(t, 0) + 1
                        info["memory"]["counts_by_collection_tag"] = by_collection
                    
                    # Entity tag counts
                    by_entity: Dict[str, int] = {}
                    for it in recent:
                        for t in (it.tags or []):
                            if t.startswith("entity:"):
                                by_entity[t] = by_entity.get(t, 0) + 1
                    if by_entity:
                        info["memory"]["counts_by_entity_tag"] = by_entity
                except Exception as e:
                    logger.debug("Collection/entity counting failed", error=str(e))
                    
            except Exception as e:
                logger.warning("Memory inspection failed", error=str(e))
        
        return info
        
    except Exception as e:
        logger.error("Failed to inspect memories", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to inspect memories: {str(e)}")


@router.post(
    "/clear",
    summary="Clear memories",
    description=(
        "Clear memories by type, tag, or clear all. When tenant_id or motet_id is set, "
        "clears that manage-app scope instead of only the process default store."
    ),
    response_description="Clear operation results",
    responses={
        200: {"description": "Clear completed"},
        403: {"description": "Caller cannot access the requested tenant or motet"},
    },
)
async def clear_memories(
    type: Optional[str] = None,
    tag: Optional[str] = None,
    clear_vector: bool = False,
    scope: ManageAppScope = Depends(get_manage_app_scope),
    principal: Principal = Depends(get_current_principal),
):
    """
    Clear memories.

    Clear memories by type, by tag, or clear all memories.
    Optionally also clear vector store entries with matching tag.
    A tenant_id / motet_id query pair wipes that scoped store.

    Args:
        type: Clear memories of this type only
        tag: Clear memories with this tag only
        clear_vector: Also clear matching entries from vector store (default: False)
        scope: Optional manage-app tenant/motet selector
        principal: Authenticated principal (from JWT, service account, or headers)

    Returns:
        Dictionary with counts of cleared memories and vectors

    Raises:
        HTTPException: 401 if authentication fails
    """

    try:
        _cfg, stack = _stack_for_principal(principal)

        cleared = {"memory": 0, "vector": 0}

        if scope.is_set:
            tenant_id, motet_id = _resolve_manage_memory_scope(principal, scope)
            if type or tag:
                collected = await asyncio.to_thread(
                    collect_memories_for_scope,
                    stack,
                    tenant_id,
                    motet_id,
                    None,
                )
                targets = filter_memories(collected, memory_type=type, tag=tag)
                manager = getattr(stack, "memory_manager", None)
                if manager and targets:
                    p_motet_id, p_tenant_id, p_principal_id = get_principal_context(principal)
                    identity = SimpleNamespace(
                        motet_id=str(motet_id or p_motet_id).strip(),
                        tenant_id=str(tenant_id or p_tenant_id).strip(),
                        principal_id=str(p_principal_id).strip(),
                        conversation_id=None,
                    )
                    result = manager.forget(
                        memory_ids=[str(getattr(item, "id", "")) for item in targets if getattr(item, "id", None)],
                        motet_context=identity,
                    )
                    cleared["memory"] = int(result.get("deleted") or 0)
                    cleared["vector"] = int(result.get("vector_deleted") or 0)
            else:
                cleared["memory"] = clear_scoped_memory_stores(
                    stack,
                    tenant_id,
                    motet_id,
                    clear_unscoped_default=False,
                )
            return cleared

        if getattr(stack, "memory", None):
            try:
                if type:
                    cleared["memory"] = stack.memory.clear_by_type(type)  # type: ignore[attr-defined]
                elif tag:
                    cleared["memory"] = stack.memory.clear_by_tag(tag)  # type: ignore[attr-defined]
                else:
                    cleared["memory"] = stack.memory.clear_all()  # type: ignore[attr-defined]
            except Exception as e:
                logger.warning("Memory clear failed", error=str(e))

        if clear_vector and getattr(stack, "vector", None) and tag:
            try:
                motet_id, tenant_id, principal_id = get_principal_context(principal)
                cleared["vector"] = stack.vector.delete_by_tag(  # type: ignore[attr-defined]
                    tag,
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    motet_id=motet_id,
                )
            except Exception as e:
                logger.warning("Vector clear failed", error=str(e))

        return cleared

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to clear memories", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to clear memories: {str(e)}")


@router.post(
    "/consolidate",
    summary="Consolidate memories",
    description="Consolidate short-term memories into long-term storage",
    response_description="Consolidation result"
)
async def consolidate_memories_endpoint(
    principal: Principal = Depends(get_current_principal)
):
    """
    Consolidate memories.
    
    Promotes short-term memories to long-term memory storage.
    This operation helps manage memory lifecycle and improve retrieval performance.
    
    Requires memory manager with consolidation support.
    
    Args:
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        Dictionary with count of promoted memories
        
    Raises:
        HTTPException: 401 if auth fails, 500 if stack not initialized or consolidation fails
    """
    
    try:
        cfg, stack = _stack_for_principal(principal)
        
        if not stack:
            raise HTTPException(status_code=500, detail="stack not initialized")
        
        created = 0
        # Use memory manager's consolidate_memories method
        memory_manager = getattr(stack, 'memory_manager', None)
        if memory_manager:
            created = memory_manager.consolidate_memories()
        else:
            logger.warning("Memory manager not available for consolidation")
        
        return {"promoted": int(created)}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to consolidate memories", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to consolidate memories: {str(e)}")


# Retrieval/Search Models
class RetrievalEvalRequest(BaseModel):
    """Request model for retrieval evaluation."""
    corpus: List[Dict[str, Any]] = Field(
        ...,
        description="Corpus of documents to evaluate against",
        json_schema_extra={"example": [{"id": "doc1", "text": "Sample text", "tags": ["conversation"]}]}
    )
    queries: List[Dict[str, Any]] = Field(
        ...,
        description="Queries to evaluate",
        json_schema_extra={"example": [{"q": "sample query", "relevant_ids": ["doc1"], "contains": "sample"}]}
    )
    top_k: int = Field(
        default=5,
        description="Number of top results to retrieve",
        ge=1,
        le=100,
        json_schema_extra={"example": 5}
    )


class RetrievalEvalResponse(BaseModel):
    """Response model for retrieval evaluation."""
    top_k: int = Field(..., description="Number of top results retrieved", json_schema_extra={"example": 5})
    n_queries: int = Field(..., description="Number of queries evaluated", json_schema_extra={"example": 10})
    precision_at_k: Dict[str, float] = Field(
        ...,
        description="Average precision at k for vector-only and hybrid retrieval",
        json_schema_extra={"example": {"vector_only": 0.85, "hybrid": 0.92}}
    )


# Retrieval/Search Endpoints
@router.get(
    "/search",
    summary="Search memories by semantic query",
    description="Perform semantic search on vector store to retrieve relevant memories",
    response_description="List of retrieved memories"
)
async def search_memories(
    request: Request,
    q: str = Query(..., description="Query text for semantic search", json_schema_extra={"example": "What is AI?"}),
    top_k: int = Query(5, description="Number of top results to return", ge=1, le=100, json_schema_extra={"example": 5}),
    tag: Optional[str] = Query(None, description="Tag to filter by", json_schema_extra={"example": "conversation"}),
    collection: Optional[str] = Query(None, description="Collection name to filter by", json_schema_extra={"example": "docs"}),
    entity: Optional[str] = Query(None, description="Entity ID to filter by", json_schema_extra={"example": "user123"}),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    principal: Principal = Depends(get_current_principal)
) -> List[Dict[str, Any]]:
    """
    Search memories using semantic search.
    
    Performs semantic search on the vector store and returns the top-k most
    relevant memories. Supports filtering by tag, collection, or entity.
    
    Args:
        q: Query text for semantic search
        top_k: Number of top results to return (1-100)
        tag: Optional tag to filter by
        collection: Optional collection name to filter by
        entity: Optional entity ID to filter by
        request: FastAPI request object for tenant extraction
        x_api_key: API key for authentication (optional, JWT/service account can be used instead)
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        List of retrieved memories with metadata
        
    Raises:
        HTTPException: 500 if search fails
    """
    try:
        from ....core.security import RateLimiter
        
        cfg = _config_for_principal(principal)
        rate_limiter = RateLimiter(
            backend=cfg.rate_limit_backend,
            redis_url=cfg.redis_url if cfg.rate_limit_backend == "redis" else None,
            limit_per_minute=cfg.rate_limit_per_minute,
        )
        rate_limiter.rate_limit(principal.id if principal else (x_api_key or "public"))
        
        if not getattr(cfg, "enable_vector_memory", False):
            return []
        
        # Build tags list
        tags = None
        if entity:
            tags = [f"entity:{entity}"]
        elif tag:
            tags = [tag]
        elif collection:
            tags = [f"collection:{collection}"]
        
        # Apply tenant filtering if enabled
        try:
            if getattr(cfg, "tenant_enforce_memory_filter", False):
                tenant_id = getattr(request.state, "tenant_id", None) if request else (principal.tenant_id if principal else None)
                if tenant_id:
                    tt = f"tenant:{tenant_id}"
                    tags = [tt] + (tags or [])
        except Exception as e:
            logger.debug("Tenant filtering failed", error=str(e))
        
        try:
            return await _memory_search_items(
                query=q,
                top_k=top_k,
                tags=tags,
                principal=principal,
            )
        except RuntimeError as exc:
            logger.error("Memory search failed", query=q, error=str(exc), exc_info=True)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Memory search failed", query=q, error=str(exc), exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post(
    "/search/eval",
    summary="Evaluate memory search performance",
    description="Evaluate memory search performance using precision@k metrics",
    response_model=RetrievalEvalResponse,
    response_description="Evaluation results with precision metrics"
)
async def eval_memory_search(
    req: RetrievalEvalRequest,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    principal: Principal = Depends(get_current_principal)
) -> RetrievalEvalResponse:
    """
    Evaluate memory search performance using precision@k metrics.
    
    Ingests a corpus of documents into the vector store and evaluates search
    performance. Queries use the same ``core.memory_recall`` (``mode="semantic"``) worker path as
    ``GET /search``; ``vector_only`` and ``hybrid`` precision match until hybrid
    is exposed on that command.
    
    Args:
        req: Evaluation request with corpus, queries, and top_k
        x_api_key: API key for authentication (optional, JWT/service account can be used instead)
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        Evaluation results with precision@k metrics for both methods
        
    Raises:
        HTTPException: 500 if evaluation fails
    """
    try:
        from ....core.security import require_api_key
        
        cfg = _config_for_principal(principal)
        require_api_key(cfg, x_api_key)
        
        stack = MotetStack(cfg)
        attach_principal_to_stack(stack, principal)
        if not getattr(stack, "vector", None):
            raise HTTPException(status_code=503, detail="Vector store not available")
        
        # Ingest corpus into vector store
        docs: List[MemoryItem] = []
        for obj in req.corpus or []:
            doc_id = obj.get("id") or _uuid.uuid4().hex
            text = obj.get("text") or ""
            tags = obj.get("tags") or ["conversation"]
            try:
                docs.append(MemoryItem(id=doc_id, type="rag_chunk", content=text, tags=tags))
            except Exception as e:
                logger.debug("Failed to create memory item", error=str(e))
                continue
        
        if docs:
            try:
                stack.vector.add(docs)  # type: ignore[attr-defined]
            except Exception as e:
                logger.warning("Failed to add documents to vector store", error=str(e))
        
        # Evaluate search performance
        def precision_at_k(results_ids: List[str], relevant_ids: set) -> float:
            """Calculate precision@k."""
            if not results_ids:
                return 0.0
            hits = sum(1 for i in results_ids if i in relevant_ids)
            return hits / float(len(results_ids))
        
        p_vec: List[float] = []
        p_hybrid: List[float] = []
        eval_tags: List[str] = ["conversation"]
        try:
            if getattr(cfg, "tenant_enforce_memory_filter", False):
                tid = getattr(principal, "tenant_id", None) if principal else None
                if tid:
                    eval_tags = [f"tenant:{tid}", "conversation"]
        except Exception as e:
            logger.debug("Eval tenant tag failed", error=str(e))
        
        for qobj in req.queries or []:
            query_text = qobj.get("q") or ""
            relevant_ids = qobj.get("relevant_ids")
            
            # If relevant_ids not provided, infer from contains field
            if not relevant_ids and qobj.get("contains"):
                needle = str(qobj.get("contains")).lower()
                relevant_ids = [d.id for d in docs if needle in (d.content or "").lower()]
            
            relevant_ids = set(relevant_ids or [])
            
            # Worker-side semantic search (same path as GET /search via memory_recall mode=semantic).
            # Hybrid toggle is not applied in this eval path yet; vector_only and hybrid report
            # the same metric.
            res_raw = await _memory_search_items(
                query=query_text,
                top_k=req.top_k,
                tags=eval_tags,
                principal=principal,
            )
            ids = [str(r.get("id", "") or "") for r in res_raw]
            pk = precision_at_k(ids, relevant_ids)
            p_vec.append(pk)
            p_hybrid.append(pk)
        
        def avg(xs: List[float]) -> float:
            """Calculate average of a list of floats."""
            return sum(xs) / len(xs) if xs else 0.0
        
        return RetrievalEvalResponse(
            top_k=req.top_k,
            n_queries=len(req.queries or []),
            precision_at_k={"vector_only": avg(p_vec), "hybrid": avg(p_hybrid)}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Memory search evaluation failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Memory search evaluation error: {str(e)}")


@router.get(
    "/vector/list",
    summary="List memories from vector store",
    description="List memories from vector store with optional filtering",
    response_description="List of memories"
)
async def list_vector_memories(
    request: Request,
    limit: int = Query(10, description="Maximum number of results", ge=1, le=100, json_schema_extra={"example": 10}),
    tag: Optional[str] = Query(None, description="Tag to filter by", json_schema_extra={"example": "conversation"}),
    collection: Optional[str] = Query(None, description="Collection name to filter by", json_schema_extra={"example": "docs"}),
    entity: Optional[str] = Query(None, description="Entity ID to filter by", json_schema_extra={"example": "user123"}),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    principal: Principal = Depends(get_current_principal)
) -> List[Dict[str, Any]]:
    """
    List memories from vector store with optional filtering.
    
    Returns a list of memories from the vector store, optionally filtered by
    tag, collection, or entity. Supports tenant isolation when enabled.
    
    Args:
        limit: Maximum number of results to return (1-100)
        tag: Optional tag to filter by
        collection: Optional collection name to filter by
        entity: Optional entity ID to filter by
        request: FastAPI request object for tenant extraction
        x_api_key: API key for authentication (optional, JWT/service account can be used instead)
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        List of memories with metadata
        
    Raises:
        HTTPException: 401 if authentication fails
    """
    try:
        from ....core.security import require_api_key, RateLimiter
        
        cfg = _config_for_principal(principal)
        require_api_key(cfg, x_api_key)
        
        rate_limiter = RateLimiter(
            backend=cfg.rate_limit_backend,
            redis_url=cfg.redis_url if cfg.rate_limit_backend == "redis" else None,
            limit_per_minute=cfg.rate_limit_per_minute,
        )
        rate_limiter.rate_limit(principal.id if principal else (x_api_key or "public"))
        
        stack = MotetStack(cfg)
        attach_principal_to_stack(stack, principal)
        if not getattr(stack, "vector", None):
            return []
        
        try:
            # Build tag filter
            if entity:
                tag = f"entity:{entity}"
            elif collection:
                tag = f"collection:{collection}"
            
            # Apply tenant filtering if enabled
            eff_tag = tag
            try:
                if getattr(cfg, "tenant_enforce_memory_filter", False):
                    tenant_id = getattr(request.state, "tenant_id", None) if request else (principal.tenant_id if principal else None)
                    if tenant_id:
                        eff_tag = f"tenant:{tenant_id}"
            except Exception as e:
                logger.debug("Tenant filtering failed", error=str(e))
            
            motet_id, tenant_id, principal_id = get_principal_context(principal)
            vec_filters = {"tenant_id": tenant_id, "principal_id": principal_id, "motet_id": motet_id}
            if eff_tag:
                items = stack.vector.list_by_tag(eff_tag, limit=limit, **vec_filters)  # type: ignore[attr-defined]
            else:
                # No generic list on all backends; fall back to a common tag
                eff_tag = "conversation"
                try:
                    if getattr(cfg, "tenant_enforce_memory_filter", False):
                        tenant_id = getattr(request.state, "tenant_id", None) if request else (principal.tenant_id if principal else None)
                        if tenant_id:
                            eff_tag = f"tenant:{tenant_id}"
                except Exception as e:
                    logger.debug("Tenant filtering failed", error=str(e))
                items = stack.vector.list_by_tag(eff_tag, limit=limit, **vec_filters)  # type: ignore[attr-defined]

            return [i.model_dump() for i in items]
        except Exception as e:
            logger.debug("Failed to list vector memories", error=str(e))
            return []
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to list vector memories", error=str(e), exc_info=True)
        return []

