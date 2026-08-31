"""
Motet - Distributed Memory Commands

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-31

Description:
    Decorator-based memory commands for the Motet distributed framework.
    Provides simplified distributed commands for memory storage, retrieval, tagging,
    targeted forget, and consolidation using the @motet.command decorator pattern.

    Refactored from class-based commands with 73% code reduction while maintaining
    full functionality and compatibility.

Dependencies:
    - pydantic: Data validation and model definitions
    - typing: Type hints and annotations
    - Decorator-based command system
    - Memory management and vector stores
    - MotetContext for unified resource access

Usage:
    from motet.core.commands.builtin.memory import memory_store, memory_recall
    
    # Store memory (decorator pattern) - using motet.do() for automatic unwrapping
    result = motet.do(memory_store, data=MemoryStoreData(
        content="Important information",
        metadata={"source": "user"},
        tags=["important"]
    ))
    
    # Recall memories (decorator pattern) - using motet.do() for automatic unwrapping
    memories = motet.do(memory_recall, data=MemoryRecallData(
        query="search term",
        limit=10
    ))

Notes:
    - Refactored from class-based to decorator-based pattern
    - Achieves 73% code reduction while maintaining functionality
    - Uses MotetContext for unified resource access
    - Supports memory storage, retrieval, tagging, and consolidation
    - Includes vector store operations and semantic search
    - Supports high-concurrency worker optimization for I/O-heavy operations
    - Accepts the current memory command data classes
    - Production-ready decorator-based implementation
"""

from typing import Any, Dict, List, Optional

import structlog

from motet import motet
from motet.core.commands.decorator import get_motet_context, MotetContext
from motet.core.commands.capabilities import WorkerCapability
from motet.core.commands.utils import require_context_field
from motet.core.types import PoolType, MemoryScopeType, serialize_memory_items
from motet.core.commands.command_data_classes import (
    MemoryStoreData,
    MemoryVectorIndexData,
    MemorySearchData,
    MemoryConsolidationData,
    MemoryTagData,
    MemoryForgetData,
    MemoryRecallData,
)
from motet.core.types import MemoryItem
from motet.core.memory.scoping import get_strategy_for_scope, ConversationScopedStrategy

logger = structlog.get_logger(__name__)


def _enforce_tenant_context_for_memory(motet: MotetContext, operation: str) -> None:
    """
    Enforce tenant presence for memory operations when tenant filtering is enabled.

    This prevents silent namespace drift where store/recall falls back to default
    tenant context and reads/writes different prefixes.
    """
    cfg = getattr(getattr(motet, "stack", None), "config", None)
    enforce = bool(getattr(cfg, "tenant_enforce_memory_filter", False))
    if enforce:
        require_context_field(
            motet,
            field_name="tenant_id",
            operation=f"{operation} when tenant_enforce_memory_filter is enabled",
            error_template="{field} is required for {operation}",
        )


def _log_memory_context(motet: MotetContext, operation: str, extra: Optional[Dict[str, Any]] = None) -> None:
    """Log key context fields used for memory namespace routing."""
    fields: Dict[str, Any] = {
        "operation": operation,
        "command_id": getattr(motet, "command_id", None),
        "task_id": getattr(motet, "task_id", None),
        "tenant_id": getattr(motet, "tenant_id", None),
        "motet_id": getattr(motet, "motet_id", None),
        "principal_id": getattr(motet, "principal_id", None),
        "conversation_id": getattr(motet, "conversation_id", None),
    }
    if extra:
        fields.update(extra)
    logger.info("memory_command_context", **fields)


def _hydrate_semantic_results_from_kv(motet: MotetContext, items: List[Any]) -> List[Any]:
    """
    Hydrate semantic vector hits from KV memory rows by memory id.

    Valkey index documents intentionally store minimal payload, so semantic results should
    be enriched with canonical KV content/metadata before returning to callers.
    """
    if not items or not getattr(motet, "memory", None):
        return items

    scoped_store_getter = getattr(motet.memory, "_scoped_kv_store", None)
    if not callable(scoped_store_getter):
        return items

    try:
        kv: Any = scoped_store_getter(motet)
    except Exception as e:
        logger.warning(
            "memory_semantic_hydration_store_resolution_failed",
            error=str(e),
            error_type=type(e).__name__,
        )
        return items

    if not kv or not hasattr(kv, "get"):
        return items

    hydrated: List[Any] = []
    for item in items:
        base_item = item.model_dump() if hasattr(item, "model_dump") else dict(item)  # type: ignore[arg-type]
        memory_id = base_item.get("id")
        if not memory_id:
            hydrated.append(base_item)
            continue

        try:
            kv_item = kv.get(str(memory_id))
        except Exception as e:
            logger.warning(
                "memory_semantic_hydration_lookup_failed",
                memory_id=memory_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            hydrated.append(base_item)
            continue

        if not kv_item:
            hydrated.append(base_item)
            continue

        if hasattr(kv_item, "model_dump"):
            kv_data = kv_item.model_dump()
        elif isinstance(kv_item, dict):
            kv_data = kv_item
        else:
            hydrated.append(base_item)
            continue
        if not isinstance(kv_data, dict):
            hydrated.append(base_item)
            continue
        merged = dict(base_item)
        merged.update(kv_data)
        base_meta = base_item.get("metadata")
        if isinstance(base_meta, dict):
            merged_meta = merged.get("metadata")
            if not isinstance(merged_meta, dict):
                merged_meta = {}
            for key in ("search_score", "distance", "relevance"):
                if key in base_meta:
                    merged_meta[key] = base_meta[key]
            merged["metadata"] = merged_meta
        for key in ("search_score", "distance", "relevance"):
            if key in base_item:
                merged[key] = base_item[key]
        hydrated.append(merged)

    return hydrated


def _memory_agent_scope_mode(cfg: Any) -> str:
    mode = str(getattr(cfg, "memory_agent_scope_mode", "prefer") or "prefer").strip().lower()
    if mode not in {"disabled", "prefer", "strict"}:
        return "prefer"
    return mode


def _resolve_memory_agent_scope_mode(motet: MotetContext, cfg: Any) -> str:
    """Prefer turn metadata (API-stamped) over worker stack Config."""
    meta = getattr(motet, "metadata", None)
    if isinstance(meta, dict):
        raw = meta.get("memory_agent_scope_mode")
        if raw is not None and str(raw).strip():
            mode = str(raw).strip().lower()
            if mode in {"disabled", "prefer", "strict"}:
                return mode
    return _memory_agent_scope_mode(cfg)


def _resolve_active_agent_id(motet: MotetContext) -> Optional[str]:
    for attr in ("agent_id", "configured_agent_id"):
        raw = getattr(motet, attr, None)
        if raw:
            value = str(raw).strip()
            if value:
                return value
    metadata = getattr(motet, "metadata", None)
    if isinstance(metadata, dict):
        for key in ("agent_id", "configured_agent_id", "configured_agent_qualified_id"):
            raw = metadata.get(key)
            if raw:
                value = str(raw).strip()
                if value:
                    return value
    return None


def _apply_agent_scope_to_items(motet: MotetContext, items: List[Any]) -> List[Any]:
    cfg = getattr(motet.stack, "config", None)
    mode = _resolve_memory_agent_scope_mode(motet, cfg)
    if mode == "disabled":
        return items
    agent_id = _resolve_active_agent_id(motet)
    if not agent_id:
        return items
    tag_prefix = str(getattr(cfg, "memory_agent_tag_prefix", "agent:") or "agent:").strip() or "agent:"
    agent_tag = f"{tag_prefix}{agent_id}"
    same_agent: List[Any] = []
    for item in (items or []):
        raw_meta = getattr(item, "metadata", None)
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        item_agent = str(meta.get("agent_id", "") or "").strip()
        raw_tags = getattr(item, "tags", None)
        tags = set(raw_tags) if isinstance(raw_tags, (list, tuple, set)) else set()
        if (item_agent and item_agent == agent_id) or (agent_tag in tags):
            same_agent.append(item)
    if mode == "strict":
        return same_agent
    return same_agent if same_agent else items


def _run_memory_recall_mode(
    *,
    motet: MotetContext,
    query: str,
    limit: int,
    tags: Optional[List[str]],
    mode: str,
    min_relevance: float = 0.5,
    conversation_id: Optional[str] = None,
) -> List[Any]:
    """
    Canonical retrieval executor used by memory_recall and memory_search.

    Modes:
    - semantic: vector-only KNN
    - recent: manager recall path (KV/recency-oriented)
    - hybrid: manager hybrid_retrieve when query is present; otherwise manager recall
    """
    mode_name = (mode or "hybrid").strip().lower()
    effective_tags = tags or []
    top_k = max(int(limit or 1), 1)
    relevance_floor = float(min_relevance)

    if mode_name == "semantic":
        if not getattr(motet.stack, "vector", None):
            raise ValueError("Vector store not available")
        cfg = getattr(motet.stack, "config", None)
        agent_scope_mode = _resolve_memory_agent_scope_mode(motet, cfg) if cfg else "prefer"
        agent_id = _resolve_active_agent_id(motet)
        # Strict: only same-agent. Prefer: try same-agent first, fallback to broad.
        if agent_scope_mode == "strict":
            agent_id_for_query = agent_id
        elif agent_scope_mode == "prefer" and agent_id:
            agent_id_for_query = agent_id
        else:
            agent_id_for_query = None

        def _do_query(use_agent: bool) -> List[Any]:
            return motet.stack.vector.query(  # type: ignore[union-attr]
                query,
                top_k=top_k,
                tags=effective_tags,
                tenant_id=getattr(motet, "tenant_id", None),
                principal_id=getattr(motet, "principal_id", None),
                conversation_id=getattr(motet, "conversation_id", None),
                motet_id=getattr(motet, "motet_id", None),
                agent_id=agent_id if use_agent else None,
            )

        results = _do_query(use_agent=bool(agent_id_for_query))
        # Prefer mode: if we queried with agent_id and got nothing, retry without (cross-agent fallback)
        if agent_scope_mode == "prefer" and agent_id_for_query and not results:
            results = _do_query(use_agent=False)
        filtered_results = _apply_agent_scope_to_items(motet, results)
        return _hydrate_semantic_results_from_kv(motet, filtered_results)

    if not motet.memory:
        raise ValueError("Memory manager not available")

    if mode_name == "recent":
        return motet.memory.recall(
            limit=top_k,
            tags=effective_tags,
            conversation_id=conversation_id,
            motet_context=motet,
        )

    # hybrid (default)
    if query and hasattr(motet.memory, "hybrid_retrieve"):
        # Keyword relevance is query coverage (head-biased), so the default
        # floor works for tagged long reports as well as untagged ones.
        return motet.memory.hybrid_retrieve(
            query=query,
            limit=top_k,
            include_recent=True,
            include_vector=True,
            min_relevance=relevance_floor,
            tags=effective_tags,
            conversation_id=conversation_id,
            motet_context=motet,
        )
    return motet.memory.recall(
        limit=top_k,
        tags=effective_tags,
        conversation_id=conversation_id,
        motet_context=motet,
    )


# === Memory Storage ===

@motet.command(
    description="Store a note or memory item in distributed tenant-isolated memory, with optional tags, metadata, and embedding indexing.",
    timeout_seconds=30,
    required_capabilities=[WorkerCapability.MEMORY_OPERATIONS],
    preferred_pool_type=PoolType.HIGH_CONCURRENCY
)
def memory_store(data: MemoryStoreData) -> Dict[str, Any]:
    """
    Store content in distributed memory systems with multi-motet/multi-tenant isolation (ADR-0027).
    
    Features:
    - Content storage with metadata and tags
    - Automatic embedding generation
    - Multi-motet/multi-tenant isolation
    - Automatic scope-based indexing
    - Principal-scoped memory support
    
    Args:
        data: Memory store data with content, metadata, and tags
        
    Returns:
        Dict with memory_id and stored status
        
    Example:
        result = motet.do(memory_store, data=MemoryStoreData(
            content="Important information",
            metadata={"source": "user"},
            tags=["important"]
        ))
    """

    motet = get_motet_context()

    if not motet.memory:
        raise ValueError("Memory manager not available")
    _enforce_tenant_context_for_memory(motet, "memory_store")
    _log_memory_context(
        motet,
        "memory_store",
        extra={"tags_count": len(data.tags or []), "memory_type": data.type, "scope_type": getattr(data, "scope_type", None)},
    )
    
    # Determine scoping strategy (ADR-0027 Phase 2)
    scope = None
    if hasattr(data, 'scope_type') and data.scope_type:
        # Explicit scope type provided in data
        try:
            scope_type = MemoryScopeType(data.scope_type)
            scope = scope_type
        except (ValueError, KeyError):
            # Invalid scope type, use default
            pass
    
    if not scope and motet.conversation_id:
        # Default to conversation scope if in a conversation context
        scope = MemoryScopeType.CONVERSATION
    
    # Store memory with intelligent scoping
    # Note: motet_context automatically retrieved by MemoryManager from WorkerLocal
    # metadata["conversation_id"] overrides where the manager files the row;
    # that override is for trusted internal writers, not API-supplied metadata.
    caller_metadata = dict(data.metadata or {})
    caller_metadata.pop("conversation_id", None)
    result = motet.memory.store(
        content=data.content,
        type=data.type,
        tags=data.tags or [],
        metadata=caller_metadata,
        scope=scope,
        long_term=data.long_term,
    )
    # Decorator returns {"memory_id": ..., "stored": True}; manager returns {"id": ..., "stored_in": [...]}
    memory_id = result.get("id") or result.get("memory_id")
    return {"memory_id": memory_id, "stored": True}


@motet.command(
    description="Load a memory row by id and upsert its embedding into the vector store for semantic search.",
    timeout_seconds=120,
    required_capabilities=[WorkerCapability.MEMORY_OPERATIONS, WorkerCapability.VECTOR_OPERATIONS],
    preferred_pool_type=PoolType.HIGH_CONCURRENCY,
)
def memory_vector_index(data: MemoryVectorIndexData) -> Dict[str, Any]:
    """
    Load a memory row from KV by id and upsert its embedding into the vector store (ADR-0092).

    Runs as a fire-and-forget follow-on after ``memory_store`` persists to KV. Indexing is
    always async (Valkey-only), so the critical path does not block on embedding or Valkey
    Search I/O.
    """
    motet = get_motet_context()

    if not motet.memory:
        raise ValueError("Memory manager not available")
    if not getattr(motet.stack, "vector", None):
        raise ValueError("Vector store not available")
    _enforce_tenant_context_for_memory(motet, "memory_vector_index")
    _log_memory_context(
        motet,
        "memory_vector_index",
        extra={"memory_id": data.memory_id},
    )

    cfg = getattr(motet.stack, "config", None)
    ltm_tag = getattr(cfg, "memory_long_term_tag", "ltm") if cfg else "ltm"

    kv = motet.memory._scoped_kv_store(motet)
    if not kv or not hasattr(kv, "get"):
        raise ValueError("KV store not available")

    item = kv.get(data.memory_id)
    if not item:
        logger.warning("memory_vector_index_kv_miss", memory_id=data.memory_id)
        return {"indexed": False, "memory_id": data.memory_id, "reason": "not_found"}

    vitem = MemoryItem(
        id=item.id,
        type=item.type,
        content=item.content,
        tags=list(item.tags or []),
        metadata=dict(item.metadata or {}),
        motet_id=item.motet_id,
        tenant_id=item.tenant_id,
        principal_id=item.principal_id,
        conversation_id=item.conversation_id,
        scope_type=item.scope_type,
        scope_id=item.scope_id,
    )
    if ltm_tag not in (vitem.tags or []):
        vitem.tags = (vitem.tags or []) + [ltm_tag]

    try:
        # Prefer centralized EmbeddingService (shared model, avoids duplicate load)
        embedding_service = motet._worker_context.get("embedding_service") if hasattr(motet, "_worker_context") else None
        vector_store = motet.stack.vector
        if embedding_service and hasattr(vector_store, "add_with_vectors"):
            vec = embedding_service.embed(vitem.content)
            vector_store.add_with_vectors([vitem], [vec])
        else:
            vector_store.add([vitem])
    except Exception as e:
        logger.error(
            "memory_vector_index_add_failed",
            memory_id=data.memory_id,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        raise
    return {"indexed": True, "memory_id": data.memory_id}


@motet.command(
    description="Semantic vector search over long-term memory (LTM) for related notes and prior context.",
    timeout_seconds=60,
    required_capabilities=[WorkerCapability.MEMORY_OPERATIONS, WorkerCapability.VECTOR_OPERATIONS],
    preferred_pool_type=PoolType.HIGH_CONCURRENCY,
)
def memory_search(data: MemorySearchData) -> Dict[str, Any]:
    """
    Semantic search over the LTM vector store.

    Embeds the query and runs KNN on the worker (Valkey Search), so the HTTP API does not
    load SentenceTransformer for ``GET /api/v1/memories/search``.
    """
    motet = get_motet_context()

    if not motet.memory:
        raise ValueError("Memory manager not available")
    _enforce_tenant_context_for_memory(motet, "memory_search")
    _log_memory_context(
        motet,
        "memory_search",
        extra={
            "top_k": data.top_k,
            "tags_count": len(data.tags or []),
            "query_len": len(data.query or ""),
        },
    )

    res = _run_memory_recall_mode(
        motet=motet,
        query=data.query,
        limit=data.top_k,
        tags=data.tags,
        mode="semantic",
    )
    items = serialize_memory_items(res)
    return {"items": items, "count": len(items)}


# === Memory Recall (Simplified) ===

@motet.command(
    description="Recall memories by natural-language query and optional tags from short-term and long-term memory.",
    timeout_seconds=30,
    required_capabilities=[WorkerCapability.MEMORY_OPERATIONS],
    preferred_pool_type=PoolType.HIGH_CONCURRENCY
)
def memory_recall(data: MemoryRecallData) -> Dict[str, Any]:
    """
    Recall memories by query with tag filtering.
    
    Args:
        data: Memory recall data with query, tags, and limit
        motet: Motet context for resource access
        
    Returns:
        Dict with recalled memory items
        
    Example:
        result = motet.do(memory_recall, data=MemoryRecallData(
            query="meeting notes",
            tags=["work"],
            limit=5
        ))
    """
    
    motet = get_motet_context()

    if not motet.memory:
        raise ValueError("Memory manager not available")
    _enforce_tenant_context_for_memory(motet, "memory_recall")
    _log_memory_context(
        motet,
        "memory_recall",
        extra={"tags_count": len(data.tags or []), "limit": data.limit, "query_present": bool(data.query)},
    )
    
    results = _run_memory_recall_mode(
        motet=motet,
        query=data.query,
        limit=data.limit,
        tags=data.tags or [],
        mode=getattr(data, "mode", "hybrid"),
        min_relevance=float(getattr(data, "min_relevance", 0.5)),
        conversation_id=data.conversation_id,
    )

    items = serialize_memory_items(results)
    return {"items": items, "count": len(items)}


# === Memory Tagging ===

@motet.command(
    description="Add, remove, or replace tags on existing memory items for filtering and organization.",
    timeout_seconds=30,
    required_capabilities=[WorkerCapability.MEMORY_OPERATIONS],
    preferred_pool_type=PoolType.HIGH_CONCURRENCY
)
def memory_tag(data: MemoryTagData) -> Dict[str, Any]:
    """
    Add, remove, or set tags on memory items.
    
    Features:
    - Bulk tag operations
    - Tag filtering and selection
    - Session-based tagging
    
    Args:
        data: Memory tag data with tags, operation, and filters
        motet: Motet context for resource access
        
    Returns:
        Dict with updated count and affected IDs
        
    Example:
        result = motet.do(memory_tag, data=MemoryTagData(
            tags=["important", "reviewed"],
            op="add",
            memory_ids=["mem_123", "mem_456"]
        ))
    """

    motet = get_motet_context()

    if not motet.memory:
        raise ValueError("Memory manager not available")
    
    op = getattr(data, "op", data.operation)
    memory_ids = data.memory_ids or ([data.memory_id] if data.memory_id else None)
    result = motet.memory.tag(
        tags=data.tags,
        op=op,
        memory_ids=memory_ids,
        conversation_id=data.conversation_id,
        filter_tag=data.filter_tag
    )
    
    return {
        "updated": result.get("updated", 0),
        "ids": result.get("ids", [])
    }


# === Memory Forget ===

@motet.command(
    description="Delete targeted memories from short-term KV and the long-term vector index.",
    timeout_seconds=30,
    required_capabilities=[WorkerCapability.MEMORY_OPERATIONS],
    preferred_pool_type=PoolType.HIGH_CONCURRENCY
)
def memory_forget(data: MemoryForgetData) -> Dict[str, Any]:
    """
    Delete memories by id or conversation/tag filter.

    Conversation and tag together intersect. Does not wipe a tenant or
    memory type. Operator clear stays on HTTP ``POST /api/v1/memories/clear``.

    Args:
        data: Memory forget data with ids or a conversation/tag filter

    Returns:
        Dict with deleted count and affected IDs

    Example:
        result = motet.do(memory_forget, data=MemoryForgetData(
            memory_ids=["mem_123"]
        ))
    """

    motet_ctx = get_motet_context()

    if not motet_ctx.memory:
        raise ValueError("Memory manager not available")
    _enforce_tenant_context_for_memory(motet_ctx, "memory_forget")
    _log_memory_context(
        motet_ctx,
        "memory_forget",
        extra={
            "ids_count": len(data.memory_ids or []),
            "conversation_id": data.conversation_id,
            "filter_tag": data.filter_tag,
        },
    )

    result = motet_ctx.memory.forget(
        memory_ids=data.memory_ids,
        conversation_id=data.conversation_id,
        filter_tag=data.filter_tag,
    )

    return {
        "deleted": result.get("deleted", 0),
        "ids": result.get("ids", []),
        "vector_deleted": result.get("vector_deleted", 0),
    }


# === Memory Consolidation ===

@motet.command(
    description="Consolidate short-term working memories into long-term storage for durable recall.",
    timeout_seconds=60,  # Longer timeout for consolidation
    required_capabilities=[WorkerCapability.MEMORY_OPERATIONS],
    preferred_pool_type=PoolType.HIGH_CONCURRENCY
)
def memory_consolidation(data: MemoryConsolidationData) -> Dict[str, Any]:
    """
    Consolidate short-term memories into long-term storage.
    
    Features:
    - Promote qualifying short-term items into long-term storage
    - Importance scoring
    - Conversation-based consolidation
    
    Args:
        data: Memory consolidation data with conversation and limits
        motet: Motet context for resource access
        
    Returns:
        Dict with consolidated count and summary
        
    Example:
        result = motet.do(memory_consolidation, data=MemoryConsolidationData(
            conversation_id="conversation_123",
            max_items=100
        ))
    """

    motet = get_motet_context()

    if not motet.memory:
        raise ValueError("Memory manager not available")
    
    consolidated_count = motet.memory.consolidate_memories(
        conversation_id=data.conversation_id,
        max_items=data.max_items
    )
    
    return {
        "consolidated": consolidated_count,
        "conversation_id": data.conversation_id
    }


__all__ = [
    "memory_store",
    "memory_vector_index",
    "memory_search",
    "memory_recall",
    "memory_tag",
    "memory_forget",
    "memory_consolidation",
]

