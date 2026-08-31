"""
Motet - Memory Manager

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-29

Description:
    Comprehensive memory manager for the Motet distributed framework.
    Provides centralized memory storage, retrieval, and management capabilities
    with working and long-term memory support. Includes memory tagging,
    targeted forget (KV + vector), metadata management, and distributed
    memory coordination.
    Conversation-scoped stores auto-add ``conversation:{id}`` tags so
    ``hybrid_retrieve`` conversation filtering can see them.
    ``store_memory`` files an item under ``metadata["conversation_id"]`` when
    set (falling back to the caller's context id), so a command can write
    rows onto another conversation — e.g. spawn_agents persisting a child's
    first turn from the parent's tool context.

Dependencies:
    - typing: Type hints and annotations
    - Memory types and item definitions
    - Stack configuration and management

Usage:
    from motet.core.memory.manager import MemoryManager

    # Create manager
    manager = MemoryManager(stack)

    # Store memory
    result = manager.store_memory(
        content="Important information",
        type="note",
        tags=["important", "work"]
    )

    # Retrieve memories
    memories = manager.retrieve_memories(
        query="search term",
        limit=10
    )

    # Forget targeted memories
    manager.forget(memory_ids=["mem_123"], motet_context=motet)

Notes:
    - Provides comprehensive memory management capabilities
    - Includes working and long-term memory support
    - Supports memory tagging, targeted forget, and metadata management
    - Includes distributed memory coordination
    - Supports memory filtering and search
    - Keyword relevance uses query coverage (head-biased), not Jaccard, so
      long reports remain findable and buried single-word hits do not pass
    - recall_principal accepts query/tags/min_relevance for topic recall
    - Integrates with stack configuration and management
    - Includes comprehensive observability and logging
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Union
from types import SimpleNamespace

import structlog

from ..types import MemoryItem, MemoryScopeType

logger = structlog.get_logger(__name__)
from .constants import CONVERSATION_SCOPE_TAG_PREFIX
from .scoping import MemoryScopingStrategy, get_strategy_for_scope

# Keyword relevance: query coverage over whole-word tokens, biased toward the
# document head (and metadata.topic / tags) so a word buried in a long report
# cannot pass typical min_relevance floors by itself. Jaccard was length-
# dependent and scored long research reports near zero even on perfect matches.
_KEYWORD_WORD_RE = re.compile(r"[a-z0-9']+")
_KEYWORD_STOPWORDS = frozenset(
    {"the", "and", "for", "with", "from", "that", "this", "into", "about", "a", "an", "of", "to", "in", "on", "or"}
)
_KEYWORD_HEAD_CHARS = 400


class MemoryManager:
    def __init__(self, stack) -> None:
        self._stack = stack

    def _get_motet_context_best_effort(self) -> Optional[Any]:
        """
        Best-effort lookup of the current MotetContext.

        This lets MemoryManager scope Redis-backed memory operations by the live
        tenant/motet context during distributed execution (ADR-0027 / ADR-0056).
        """
        try:
            from motet.core.commands.decorator import get_motet_context

            return get_motet_context()
        except Exception:
            return None

    def _resolve_identity_context(
        self,
        *,
        motet_context: Optional[Any],
        operation: str,
    ) -> Any:
        """
        Resolve and validate identity context for memory operations.

        Memory operations must run with explicit identity context to prevent
        implicit fallback to shared stack defaults.
        """
        context = motet_context or self._get_motet_context_best_effort()
        if context is None:
            raise ValueError(
                f"{operation} requires MotetContext with tenant_id, motet_id, and principal_id"
            )

        motet_id = str(getattr(context, "motet_id", "") or "").strip()
        tenant_id = str(getattr(context, "tenant_id", "") or "").strip()
        principal_id = str(getattr(context, "principal_id", "") or "").strip()
        conversation_id = getattr(context, "conversation_id", None)
        agent_id = self._extract_agent_id(context)

        if not motet_id or not tenant_id or not principal_id:
            raise ValueError(
                f"{operation} requires non-empty tenant_id, motet_id, and principal_id"
            )

        return SimpleNamespace(
            motet_id=motet_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
        )

    def _scoped_kv_store(self, motet_context: Optional[Any]) -> Any:
        """
        Return a memory KV store scoped to the current motet/tenant.

        MotetStack initializes `stack.memory` once at process start (often with config
        defaults). In distributed multi-tenant runs, the live request tenant/motet
        can differ. For RedisStore, we create a lightweight scoped view sharing the
        same Redis client + encryption service but using the correct key prefix.
        """
        mem = getattr(self._stack, "memory", None)
        if mem is None or motet_context is None:
            return mem

        # Avoid hard import cycles at module load time.
        try:
            from .redis_store import RedisStore
        except Exception:
            return mem

        if not isinstance(mem, RedisStore):
            return mem

        # Align with DistributedCommandContext defaults (distributed.py) so worker store/recall
        # use the same namespace when envelope omits tenant_id/motet_id.
        motet_id = getattr(motet_context, "motet_id", None) or "default"
        tenant_id = getattr(motet_context, "tenant_id", None) or "default"

        # Reuse the already-initialized redis client + encryption service.
        return RedisStore(
            redis_client=getattr(mem, "_r", None),
            motet_id=str(motet_id),
            tenant_id=str(tenant_id),
            encryption_service=getattr(mem, "_encryption_service", None),
        )

    def _recall_from_kv_store(
        self,
        mem: Any,
        *,
        tags: List[str],
        match_mode: str,
        conversation_id: Optional[str],
        types: Optional[List[str]],
        scope: str,
        wm_tag: str,
    ) -> List[MemoryItem]:
        """Run tag-based recall against a single KV store. Used for primary scope and fallback scopes."""
        items: List[MemoryItem] = []
        ids_by_tag: List[Set[str]] = []
        if tags:
            for tg in tags:
                try:
                    matched = {m.id for m in mem.search_by_tag(tg)}  # type: ignore[attr-defined]
                except Exception:
                    matched = set()
                ids_by_tag.append(matched)
        if ids_by_tag:
            candidate_ids = set.intersection(*ids_by_tag) if match_mode == "all" else set.union(*ids_by_tag)
        else:
            try:
                candidate_ids = {m.id for m in mem.all()}  # type: ignore[attr-defined]
            except Exception:
                candidate_ids = {m.id for m in mem.recent(limit=200)}  # type: ignore[attr-defined]
        if conversation_id:
            try:
                conversation_ids = {m.id for m in mem.search_by_tag(f"{CONVERSATION_SCOPE_TAG_PREFIX}{conversation_id}")}  # type: ignore[attr-defined]
            except Exception:
                conversation_ids = set()
            candidate_ids &= conversation_ids
        try:
            pool = {m.id: m for m in mem.all()}  # type: ignore[attr-defined]
        except Exception:
            pool = {m.id: m for m in mem.recent(limit=500)}  # type: ignore[attr-defined]
        for mid in candidate_ids:
            it = pool.get(mid)
            if not it:
                continue
            if types and it.type not in set(types):
                continue
            if scope in {"wm", "working", "working_memory", "working-memory"}:
                if wm_tag not in (it.tags or []):
                    continue
            items.append(it)
        return items

    def _get_tags(self, *candidates: Optional[str]) -> List[str]:
        tags: List[str] = []
        for c in candidates:
            if c and c not in tags:
                tags.append(c)
        return tags

    @staticmethod
    def _memory_agent_scope_mode(cfg: Any) -> str:
        """Return memory agent scope mode: disabled|prefer|strict."""
        mode = str(getattr(cfg, "memory_agent_scope_mode", "prefer") or "prefer").strip().lower()
        if mode not in {"disabled", "prefer", "strict"}:
            return "prefer"
        return mode

    def _resolve_memory_agent_scope_mode(
        self,
        cfg: Any,
        motet_context: Optional[Any] = None,
    ) -> str:
        """
        Resolve agent-scope mode, preferring per-turn metadata over stack config.

        Chat stamps ``memory_agent_scope_mode`` from the API Config into turn
        context/metadata so distributed workers honor the caller's policy even
        when worker boot env differs (integration tests, mixed deployments).
        """
        context = motet_context or self._get_motet_context_best_effort()
        if context is not None:
            for raw in (
                getattr(context, "memory_agent_scope_mode", None),
                (getattr(context, "metadata", None) or {}).get("memory_agent_scope_mode")
                if isinstance(getattr(context, "metadata", None), dict)
                else None,
            ):
                if raw is None:
                    continue
                mode = str(raw).strip().lower()
                if mode in {"disabled", "prefer", "strict"}:
                    return mode
        return self._memory_agent_scope_mode(cfg)

    @staticmethod
    def _memory_agent_tag_prefix(cfg: Any) -> str:
        """Return configured tag prefix used for agent facet tagging."""
        return str(getattr(cfg, "memory_agent_tag_prefix", "agent:") or "agent:").strip() or "agent:"

    @staticmethod
    def _extract_agent_id(context: Any) -> Optional[str]:
        """Best-effort resolve of active agent id from MotetContext-like objects."""
        if context is None:
            return None
        for attr in ("agent_id", "configured_agent_id"):
            raw = getattr(context, attr, None)
            if raw:
                value = str(raw).strip()
                if value:
                    return value
        metadata = getattr(context, "metadata", None)
        if isinstance(metadata, dict):
            for key in ("agent_id", "configured_agent_id", "configured_agent_qualified_id"):
                raw = metadata.get(key)
                if raw:
                    value = str(raw).strip()
                    if value:
                        return value
        return None

    def _apply_agent_scope(
        self,
        *,
        items: List[MemoryItem],
        identity_context: Any,
        cfg: Any,
    ) -> List[MemoryItem]:
        """
        Apply agent-aware retrieval segmentation.

        - disabled: do nothing
        - strict: return only same-agent items
        - prefer: prefer same-agent items, fallback to original list when none match
        """
        mode = self._resolve_memory_agent_scope_mode(cfg)
        agent_id = str(getattr(identity_context, "agent_id", "") or "").strip()
        if mode == "disabled" or not agent_id:
            return items

        agent_tag = f"{self._memory_agent_tag_prefix(cfg)}{agent_id}"
        same_agent: List[MemoryItem] = []
        for item in items:
            raw_meta = getattr(item, "metadata", None)
            meta = raw_meta if isinstance(raw_meta, dict) else {}
            item_agent = str(meta.get("agent_id", "") or "").strip()
            raw_tags = getattr(item, "tags", None)
            item_tags = set(raw_tags) if isinstance(raw_tags, (list, tuple, set)) else set()
            if (item_agent and item_agent == agent_id) or (agent_tag in item_tags):
                same_agent.append(item)

        if mode == "strict":
            return same_agent
        # prefer
        return same_agent if same_agent else items

    @staticmethod
    def _should_async_vector_index(cfg: Any) -> bool:
        """Always async: LTM indexing via ``core.memory_vector_index`` (Valkey-only, ADR-0092)."""
        return True

    def _try_dispatch_vector_index(self, memory_id: str) -> bool:
        """Fire-and-forget vector index job; returns False if dispatch is unavailable or fails."""
        motet = self._get_motet_context_best_effort()
        if motet is None or not hasattr(motet, "dispatch"):
            return False
        try:
            from motet.core.commands.builtin.memory import memory_vector_index
            from motet.core.commands.command_data_classes import MemoryVectorIndexData

            motet.dispatch([(memory_vector_index, MemoryVectorIndexData(memory_id=memory_id))])
            return True
        except Exception as e:
            logger.warning(
                "memory_vector_index_dispatch_failed",
                memory_id=memory_id,
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            return False

    def store_memory(
        self,
        *,
        content: str,
        type: str,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        item_id: Optional[str] = None,
        working: Optional[bool] = None,
        long_term: Optional[bool] = None,
        motet_context: Optional[Any] = None,  # MotetContext - using Any to avoid circular import
        scope: Optional[Union[MemoryScopingStrategy, MemoryScopeType]] = None,
    ) -> Dict[str, Any]:
        cfg = getattr(self._stack, "config", None)
        stm_tag = getattr(cfg, "memory_short_term_tag", "stm") if cfg else "stm"
        wm_tag = getattr(cfg, "memory_working_tag", "wm") if cfg else "wm"

        base_tags = list(tags or [])

        # Heuristics for destinations
        dest_working = bool(working)
        dest_long = bool(long_term)

        if type == "assistant_response":
            # Short-lived scratch (WM/STM); optionally LTM
            dest_working = True if working is None else bool(working)
            if long_term is None:
                dest_long = bool(getattr(cfg, "store_assistant_vector", False))
        elif type == "user_message":
            # Session context; also LTM if vector enabled
            if working is None:
                dest_working = False
            if long_term is None:
                dest_long = bool(getattr(cfg, "enable_vector_memory", False))
        elif type == "summary":
            dest_long = True if long_term is None else bool(long_term)

        # Build MemoryItem once; tags adjusted per destination
        from uuid import uuid4
        mid = item_id or str(uuid4())
        
        # Multi-motet/multi-tenant isolation (ADR-0027 / ADR-0090):
        # require explicit identity context, never implicit stack-config fallback.
        identity_context = self._resolve_identity_context(
            motet_context=motet_context,
            operation="store_memory",
        )
        motet_id = identity_context.motet_id
        tenant_id = identity_context.tenant_id
        principal_id = identity_context.principal_id
        conversation_id = identity_context.conversation_id
        
        # Explicit metadata conversation_id wins over the caller's context so a
        # command can write rows onto another conversation (e.g. spawn_agents
        # persisting a child's turn from the parent's tool context). The item
        # is indexed and tagged under this id — using the context id here
        # would file the row on the wrong conversation.
        meta = metadata or {}
        if getattr(identity_context, "agent_id", None):
            meta = dict(meta)
            meta.setdefault("agent_id", identity_context.agent_id)
        meta_conversation_id = str(meta.get("conversation_id") or "").strip()
        if meta_conversation_id:
            conversation_id = meta_conversation_id

        agent_tag = None
        if getattr(identity_context, "agent_id", None):
            agent_tag = f"{self._memory_agent_tag_prefix(cfg)}{identity_context.agent_id}"
            if agent_tag not in base_tags:
                base_tags.append(agent_tag)

        # hybrid_retrieve / recall filter conversation scope via tags, not only
        # MemoryItem.conversation_id. Keep the tag in sync whenever we have one.
        if conversation_id:
            conv_tag = f"{CONVERSATION_SCOPE_TAG_PREFIX}{conversation_id}"
            if conv_tag not in base_tags:
                base_tags.append(conv_tag)
        
        # Apply scoping strategy (ADR-0027 Phase 2)
        scope_type_str = "working" if dest_working else "conversation"  # Default
        scope_id = conversation_id  # Default
        
        if scope:
            # Get strategy instance
            if isinstance(scope, MemoryScopeType):
                strategy = get_strategy_for_scope(scope)
            else:
                strategy = scope
            
            # Apply strategy if motet_context is available
            if identity_context:
                try:
                    scope_type = strategy.determine_scope(identity_context, content, meta)
                    scope_type_str = scope_type.value
                    scope_id = strategy.generate_scope_id(identity_context)
                except Exception as e:
                    logger.warning(
                        "memory_scope_strategy_fallback",
                        operation="store_memory",
                        error=str(e),
                    )
        
        item = MemoryItem(
            id=mid,
            type=type,
            content=content,
            tags=list(base_tags),
            metadata=meta,
            # Multi-motet/multi-tenant isolation (ADR-0027)
            motet_id=motet_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            conversation_id=conversation_id,
            scope_type=scope_type_str,
            scope_id=scope_id
        )

        stored_in: List[str] = []

        # Working/short-term: primary KV store
        kv_store = self._scoped_kv_store(identity_context)
        if kv_store:
            # If storing to working or short-term, ensure tags
            if dest_working:
                if wm_tag not in item.tags:
                    item.tags.append(wm_tag)
            if stm_tag not in item.tags:
                item.tags.append(stm_tag)
            try:
                kv_store.upsert(item)
                stored_in.append("memory")
            except Exception as e:
                logger.warning(
                    "memory_kv_store_write_failed",
                    operation="store_memory",
                    error=str(e),
                )

        # Long-term: vector indexing is async via memory_vector_index (ADR-0092)
        if dest_long and getattr(self._stack, "vector", None):
            # LTM tagging happens in core.memory_vector_index before vector.add.
            if "memory" in stored_in and self._try_dispatch_vector_index(item.id):
                # Dispatch success means indexing is queued, not query-ready yet.
                stored_in.append("vector_pending")

        return {"id": item.id, "stored_in": stored_in}

    def recall(
        self,
        *,
        tags: Optional[List[str]] = None,
        match: str = "any",
        limit: int = 5,
        conversation_id: Optional[str] = None,
        types: Optional[List[str]] = None,
        scope: Optional[str] = None,
        include_vector: bool = False,
        motet_context: Optional[Any] = None,  # Optional override, auto-retrieved if not provided
    ) -> List[MemoryItem]:
        cfg = getattr(self._stack, "config", None)
        wm_tag = getattr(cfg, "memory_working_tag", "wm") if cfg else "wm"

        tags = [t for t in (tags or []) if t]
        match_mode = (match or "any").lower()
        scope = (scope or "both").lower()

        items: List[MemoryItem] = []

        # Short-term/working from KV store. Require explicit identity context.
        identity_context = self._resolve_identity_context(
            motet_context=motet_context,
            operation="recall",
        )
        mem = self._scoped_kv_store(identity_context)
        if mem and scope not in {"ltm", "long", "long_term", "long-term"}:
            items = self._recall_from_kv_store(
                mem,
                tags=tags,
                match_mode=match_mode,
                conversation_id=conversation_id,
                types=types,
                scope=scope,
                wm_tag=wm_tag,
            )

        # Vector augmentation or LTM-only
        if getattr(self._stack, "vector", None) and (include_vector or scope in {"ltm", "long", "long_term", "long-term", "both", "all"}) and tags:
            try:
                seen = {i.id for i in items}
                mode = self._resolve_memory_agent_scope_mode(cfg)
                vec_filters: Dict[str, Optional[str]] = {
                    "tenant_id": getattr(identity_context, "tenant_id", None),
                    "principal_id": getattr(identity_context, "principal_id", None),
                    "conversation_id": getattr(identity_context, "conversation_id", None),
                    "motet_id": getattr(identity_context, "motet_id", None),
                }
                if mode == "strict" and getattr(identity_context, "agent_id", None):
                    vec_filters["agent_id"] = identity_context.agent_id
                for tg in tags:
                    # Prefer mode: try same-agent first, fallback to broad
                    if mode == "prefer" and getattr(identity_context, "agent_id", None):
                        vec_filters["agent_id"] = identity_context.agent_id
                        vector_items = list(
                            self._stack.vector.list_by_tag(tg, limit=limit, **vec_filters)  # type: ignore[attr-defined]
                        )
                        if not vector_items:
                            vec_filters.pop("agent_id", None)
                            vector_items = list(
                                self._stack.vector.list_by_tag(tg, limit=limit, **vec_filters)  # type: ignore[attr-defined]
                            )
                    else:
                        vector_items = list(
                            self._stack.vector.list_by_tag(tg, limit=limit, **vec_filters)  # type: ignore[attr-defined]
                        )
                    for v in vector_items:
                        if v.id in seen:
                            continue
                        items.append(v)
                        seen.add(v.id)
            except Exception as e:
                logger.warning(
                    "memory_vector_augmentation_failed",
                    operation="recall",
                    error=str(e),
                )

        items = self._apply_agent_scope(items=items, identity_context=identity_context, cfg=cfg)

        try:
            items.sort(key=lambda m: getattr(m, "created_at", None) or 0, reverse=True)
        except Exception as e:
            logger.warning(
                "memory_recall_sort_failed",
                operation="recall",
                error=str(e),
            )
        return items[:limit]

    def recall_conversation(
        self,
        *,
        conversation_id: str,
        limit: int = 10,
        types: Optional[List[str]] = None,
        motet_context: Optional[Any] = None,  # Optional override, auto-retrieved if not provided
    ) -> List[MemoryItem]:
        """
        Recall memories scoped to a specific conversation (ADR-0027 Phase 2).
        
        Uses indexed O(k) retrieval via RedisStore.by_conversation() where k is the
        number of items in the conversation, instead of O(n) scan of all memories.
        
        Args:
            conversation_id: Conversation ID to filter by
            limit: Maximum number of memories to return
            types: Optional list of memory types to filter
            motet_context: Optional MotetContext override (auto-retrieved if not provided)
            
        Returns:
            List of MemoryItem scoped to the conversation
        """
        identity_context = self._resolve_identity_context(
            motet_context=motet_context,
            operation="recall_conversation",
        )
        mem = self._scoped_kv_store(identity_context)
        if not mem:
            return []
        
        items: List[MemoryItem] = []
        try:
            # Efficient indexed retrieval - O(k) where k = items in conversation
            items = mem.by_conversation(
                conversation_id=conversation_id,
                limit=limit,
                types=types
            )
            # Filter by scope_type (the index doesn't filter by scope_type)
            items = [
                m for m in items 
                if getattr(m, "scope_type", None) == MemoryScopeType.CONVERSATION.value
            ]
        except Exception as e:
            logger.warning(
                "memory_recall_conversation_indexed_failed",
                operation="recall_conversation",
                conversation_id=conversation_id,
                error=str(e),
            )
        
        # Sort by creation time (most recent first)
        # Note: by_conversation already returns sorted by timestamp, but we ensure consistency
        try:
            items.sort(key=lambda m: getattr(m, "created_at", None) or 0, reverse=True)
        except Exception as e:
            logger.warning(
                "memory_recall_sort_failed",
                operation="recall_conversation",
                error=str(e),
            )
        
        return items[:limit]

    def recall_principal(
        self,
        *,
        principal_id: str,
        limit: int = 10,
        types: Optional[List[str]] = None,
        query: Optional[str] = None,
        tags: Optional[List[str]] = None,
        min_relevance: float = 0.5,
        motet_context: Optional[Any] = None,  # Optional override, auto-retrieved if not provided
    ) -> List[MemoryItem]:
        """
        Recall memories scoped to a specific principal/user (ADR-0027 Phase 2).

        Uses indexed O(k) retrieval via RedisStore.by_principal() where k is the
        number of items for the principal, instead of O(n) scan of all memories.

        When ``query`` is set, candidates are ranked by keyword relevance (query
        coverage, head-biased) and filtered by ``min_relevance``. Without a
        query, results are newest-first and ``min_relevance`` is ignored.

        Args:
            principal_id: Principal/user ID to filter by
            limit: Maximum number of memories to return
            types: Optional list of memory types to filter
            query: Optional topic/query text for relevance ranking
            tags: Optional tags filter (match any)
            min_relevance: Minimum keyword relevance when ``query`` is set
            motet_context: Optional MotetContext override (auto-retrieved if not provided)

        Returns:
            List of MemoryItem scoped to the principal
        """
        identity_context = self._resolve_identity_context(
            motet_context=motet_context,
            operation="recall_principal",
        )
        mem = self._scoped_kv_store(identity_context)
        if not mem:
            return []

        tag_set = {t for t in (tags or []) if t}
        query_text = (query or "").strip()
        # Over-fetch when we will filter/rank locally; the index returns by recency.
        fetch_limit = limit
        if query_text or tag_set:
            fetch_limit = min(max(limit * 10, 50), 200)

        items: List[MemoryItem] = []
        try:
            items = mem.by_principal(
                principal_id=principal_id,
                limit=fetch_limit,
                types=types,
            )
            items = [
                m for m in items
                if getattr(m, "scope_type", None) == MemoryScopeType.PRINCIPAL.value
            ]
        except Exception as e:
            logger.warning(
                "memory_recall_principal_indexed_failed",
                operation="recall_principal",
                principal_id=principal_id,
                error=str(e),
            )

        if tag_set:
            items = [
                m for m in items
                if tag_set.intersection(set(getattr(m, "tags", None) or []))
            ]

        if query_text:
            scored: List[tuple[float, MemoryItem]] = []
            for item in items:
                score = self._calculate_keyword_relevance(
                    query_text,
                    getattr(item, "content", "") or "",
                    metadata=getattr(item, "metadata", None),
                    tags=getattr(item, "tags", None),
                )
                if score < min_relevance:
                    continue
                meta = getattr(item, "metadata", None)
                if not isinstance(meta, dict):
                    meta = {}
                    item.metadata = meta
                meta["relevance_score"] = score
                scored.append((score, item))
            scored.sort(
                key=lambda pair: (
                    pair[0],
                    getattr(pair[1], "created_at", None) or 0,
                ),
                reverse=True,
            )
            return [item for _, item in scored[:limit]]

        try:
            items.sort(key=lambda m: getattr(m, "created_at", None) or 0, reverse=True)
        except Exception as e:
            logger.warning(
                "memory_recall_sort_failed",
                operation="recall_principal",
                error=str(e),
            )

        return items[:limit]

    def recall_multi_scope(
        self,
        *,
        scope_types: List[MemoryScopeType],
        limit: int = 10,
        types: Optional[List[str]] = None,
        motet_context: Optional[Any] = None,  # Optional override, auto-retrieved if not provided
    ) -> List[MemoryItem]:
        """
        Recall memories across multiple scope types (ADR-0027 Phase 2).
        
        Supports hierarchical memory recall, e.g., GLOBAL → PRINCIPAL → CONVERSATION
        to get context from multiple scopes in priority order.
        
        MotetContext is automatically retrieved from WorkerLocal for filtering.
        
        Args:
            scope_types: List of scope types to query (in priority order)
            limit: Maximum number of memories to return
            types: Optional list of memory types to filter
            motet_context: Optional MotetContext override (auto-retrieved if not provided)
            
        Returns:
            List of MemoryItem from multiple scopes (deduplicated)
        """
        identity_context = self._resolve_identity_context(
            motet_context=motet_context,
            operation="recall_multi_scope",
        )

        mem = self._scoped_kv_store(identity_context)
        if not mem:
            return []
        
        items: List[MemoryItem] = []
        seen_ids: Set[str] = set()
        
        try:
            # Get all memories
            all_memories = mem.all()
            
            # Process scopes in priority order
            for scope_type in scope_types:
                scope_value = scope_type.value
                
                for m in all_memories:
                    # Skip if already seen
                    if m.id in seen_ids:
                        continue
                    
                    # Filter by scope_type
                    if getattr(m, "scope_type", None) != scope_value:
                        continue
                    
                    # Filter by motet_context if provided
                    if identity_context:
                        # Check motet_id
                        if hasattr(identity_context, "motet_id"):
                            if getattr(m, "motet_id", None) != identity_context.motet_id:
                                continue
                        # Check tenant_id
                        if hasattr(identity_context, "tenant_id"):
                            if getattr(m, "tenant_id", None) != identity_context.tenant_id:
                                continue
                        # Check principal_id for PRINCIPAL scope
                        if scope_type == MemoryScopeType.PRINCIPAL and hasattr(identity_context, "principal_id"):
                            if getattr(m, "principal_id", None) != identity_context.principal_id:
                                continue
                        # Check conversation_id for CONVERSATION scope
                        if scope_type == MemoryScopeType.CONVERSATION and hasattr(identity_context, "conversation_id"):
                            if getattr(m, "conversation_id", None) != identity_context.conversation_id:
                                continue
                    
                    # Filter by types if specified
                    if types and m.type not in types:
                        continue
                    
                    items.append(m)
                    seen_ids.add(m.id)
                    
                    # Stop if we've reached the limit
                    if len(items) >= limit:
                        break
                
                if len(items) >= limit:
                    break
                    
        except Exception as e:
            logger.warning(
                "memory_recall_multi_scope_failed",
                operation="recall_multi_scope",
                error=str(e),
            )
        
        # Sort by creation time (most recent first)
        try:
            items.sort(key=lambda m: getattr(m, "created_at", None) or 0, reverse=True)
        except Exception as e:
            logger.warning(
                "memory_recall_sort_failed",
                operation="recall_multi_scope",
                error=str(e),
            )
        
        return items[:limit]

    def _search_ids_by_tag(
        self,
        mem: Any,
        tag: str,
        *,
        operation: str,
        log_event: str,
        **log_fields: Any,
    ) -> List[str]:
        """Return distinct item ids matching ``tag``, preserving store order."""
        try:
            items = mem.search_by_tag(tag)
        except Exception as e:
            logger.warning(log_event, operation=operation, error=str(e), **log_fields)
            return []
        seen: Set[str] = set()
        ids: List[str] = []
        for it in items:
            if it.id not in seen:
                seen.add(it.id)
                ids.append(it.id)
        return ids

    def _resolve_memory_target_ids(
        self,
        mem: Any,
        *,
        memory_ids: Optional[List[str]],
        conversation_id: Optional[str],
        filter_tag: Optional[str],
        operation: str,
    ) -> List[str]:
        """Resolve forget/retag targets from ids or conversation/tag filters.

        ``memory_ids`` wins. Conversation and tag together are an intersection
        so a dual filter cannot widen the set.
        """
        if memory_ids:
            return list(memory_ids)
        conv_ids: List[str] = []
        filter_ids: List[str] = []
        if conversation_id:
            conv_ids = self._search_ids_by_tag(
                mem,
                f"{CONVERSATION_SCOPE_TAG_PREFIX}{conversation_id}",
                operation=operation,
                log_event="memory_target_search_by_conversation_failed",
                conversation_id=conversation_id,
            )
        if filter_tag:
            filter_ids = self._search_ids_by_tag(
                mem,
                filter_tag,
                operation=operation,
                log_event="memory_target_search_by_filter_tag_failed",
                filter_tag=filter_tag,
            )
        if conversation_id and filter_tag:
            filter_set = set(filter_ids)
            return [mid for mid in conv_ids if mid in filter_set]
        if conversation_id:
            return conv_ids
        if filter_tag:
            return filter_ids
        return []

    def retag(
        self,
        *,
        tags: List[str],
        op: str,
        memory_ids: Optional[List[str]] = None,
        conversation_id: Optional[str] = None,
        filter_tag: Optional[str] = None,
        motet_context: Optional[Any] = None,  # Optional override, auto-retrieved if not provided
    ) -> Dict[str, Any]:
        """Apply tag operation to existing memories.

        Updates the KV store and, when a vector store is present, matching LTM tags.
        Conversation and tag together intersect.
        """
        op = (op or "add").lower()
        updated: int = 0
        affected: List[str] = []

        identity_context = self._resolve_identity_context(
            motet_context=motet_context,
            operation="retag",
        )
        mem = self._scoped_kv_store(identity_context)
        if not mem:
            return {"updated": 0, "ids": []}

        target_ids = self._resolve_memory_target_ids(
            mem,
            memory_ids=memory_ids,
            conversation_id=conversation_id,
            filter_tag=filter_tag,
            operation="retag",
        )

        for mid in target_ids:
            try:
                item = mem.get(mid)
                if not item:
                    continue
                current = set(item.tags or [])
                if op == "add":
                    current.update(tags)
                elif op == "remove":
                    for t in tags:
                        current.discard(t)
                elif op == "set":
                    preserve = {t for t in current if t.startswith(CONVERSATION_SCOPE_TAG_PREFIX)}
                    current = set(tags) | preserve
                item.tags = sorted(list(current))
                mem.upsert(item)
                updated += 1
                affected.append(mid)
            except Exception as e:
                logger.debug("memory_retag_item_skipped", memory_id=mid, error=str(e))
                continue

        # Vector retagging (LTM) when explicit IDs are provided
        vec = getattr(self._stack, "vector", None)
        if vec and target_ids:
            try:
                updated_vec = vec.update_tags(target_ids, tags, op)  # type: ignore[attr-defined]
                updated += int(updated_vec or 0)
            except Exception as e:
                logger.warning(
                    "memory_vector_retag_failed",
                    operation="retag",
                    error=str(e),
                )
        return {"updated": int(updated), "ids": affected[:50]}

    def forget(
        self,
        *,
        memory_ids: Optional[List[str]] = None,
        conversation_id: Optional[str] = None,
        filter_tag: Optional[str] = None,
        motet_context: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Delete targeted memories from KV and the vector index.

        Empty selectors are a no-op. Conversation and tag together intersect.
        Does not wipe a tenant or type (HTTP ``/memories/clear`` stays
        operator-only).
        """
        identity_context = self._resolve_identity_context(
            motet_context=motet_context,
            operation="forget",
        )
        mem = self._scoped_kv_store(identity_context)
        if not mem:
            return {"deleted": 0, "ids": [], "vector_deleted": 0}

        target_ids = self._resolve_memory_target_ids(
            mem,
            memory_ids=memory_ids,
            conversation_id=conversation_id,
            filter_tag=filter_tag,
            operation="forget",
        )

        deleted_ids: List[str] = []
        for mid in target_ids:
            try:
                if mem.delete(mid):
                    deleted_ids.append(mid)
            except Exception as e:
                logger.debug("memory_forget_item_skipped", memory_id=mid, error=str(e))
                continue

        vector_deleted = 0
        vec = getattr(self._stack, "vector", None)
        if vec is not None and target_ids:
            try:
                vector_deleted = int(
                    vec.delete_ids(
                        target_ids,
                        tenant_id=identity_context.tenant_id,
                        principal_id=identity_context.principal_id,
                        conversation_id=conversation_id or identity_context.conversation_id,
                        motet_id=identity_context.motet_id,
                        agent_id=identity_context.agent_id,
                    )
                    or 0
                )
            except Exception as e:
                logger.warning(
                    "memory_vector_forget_failed",
                    operation="forget",
                    error=str(e),
                )
        return {
            "deleted": len(deleted_ids),
            "ids": deleted_ids[:50],
            "vector_deleted": vector_deleted,
        }

    def consolidate_memories(
        self,
        conversation_id: Optional[str] = None,
        max_items: int = 100,
        motet_context: Optional[Any] = None,  # Optional override, auto-retrieved if not provided
    ) -> int:
        """
        Move important short-term memories to long-term storage.
        
        This method:
        1. Identifies memories that should be consolidated based on age, importance, and access patterns
        2. Moves qualifying memories from short-term (KV) to long-term (vector) storage
        3. Marks consolidated memories to avoid duplication
        
        Args:
            conversation_id: Optional conversation filter for consolidation
            max_items: Maximum number of items to consolidate in one operation
            motet_context: Optional MotetContext override (auto-retrieved if not provided)
            
        Returns:
            Number of memories successfully consolidated
        """
        from datetime import datetime, timedelta
        
        consolidated_count = 0
        
        try:
            # Get memories from short-term storage (KV store) with explicit identity context.
            identity_context = self._resolve_identity_context(
                motet_context=motet_context,
                operation="consolidate_memories",
            )
            mem = self._scoped_kv_store(identity_context)
            vector = getattr(self._stack, "vector", None)
            
            if not mem or not vector:
                return 0
            
            # Get candidate memories for consolidation
            try:
                if conversation_id:
                    # Filter by conversation if specified
                    conversation_memories = mem.search_by_tag(f"{CONVERSATION_SCOPE_TAG_PREFIX}{conversation_id}")
                    candidate_memories = list(conversation_memories)[:max_items * 2]  # Get more to filter from
                else:
                    # Get recent memories from short-term storage
                    candidate_memories = mem.recent(limit=max_items * 2)
            except Exception:
                candidate_memories = []
            
            if not candidate_memories:
                return 0
            
            # Evaluate each memory for consolidation
            for memory_item in candidate_memories:
                if consolidated_count >= max_items:
                    break
                    
                # Skip if already consolidated
                if memory_item.metadata and memory_item.metadata.get("consolidated"):
                    continue
                
                # Check if memory should be consolidated
                should_consolidate = self._should_consolidate_memory(memory_item)
                
                if should_consolidate:
                    try:
                        # Create long-term version with enhanced metadata
                        lt_memory = MemoryItem(
                            id=memory_item.id,  # Keep same ID to avoid duplication
                            type=memory_item.type,
                            content=memory_item.content,
                            tags=(memory_item.tags or []) + ["ltm", "consolidated"],
                            metadata={
                                **(memory_item.metadata or {}),
                                "consolidated": True,
                                "consolidated_at": datetime.now().isoformat(),
                                "consolidation_reason": "automatic",
                                "original_created_at": getattr(memory_item, 'created_at', datetime.now()).isoformat() if hasattr(memory_item, 'created_at') else datetime.now().isoformat()
                            },
                            # Preserve isolation fields (ADR-0027)
                            motet_id=getattr(memory_item, "motet_id", None),
                            tenant_id=getattr(memory_item, "tenant_id", None),
                            principal_id=getattr(memory_item, "principal_id", None),
                            conversation_id=getattr(memory_item, "conversation_id", None),
                            scope_type=getattr(memory_item, "scope_type", None),
                            scope_id=getattr(memory_item, "scope_id", None),
                        )
                        
                        # Store LTM version to KV (async vector indexing will pick it up)
                        mem.upsert(lt_memory)
                        self._try_dispatch_vector_index(lt_memory.id)
                        
                        consolidated_count += 1
                        
                    except Exception as e:
                        # Log error but continue with other memories
                        continue
            
            return consolidated_count
            
        except Exception as e:
            # Return partial count if something went wrong
            return consolidated_count
    
    def _should_consolidate_memory(self, memory_item: MemoryItem) -> bool:
        """
        Determine if a memory should be consolidated to long-term storage.
        
        Consolidation criteria:
        1. Age-based: Older memories with sufficient importance
        2. Access-based: Frequently accessed memories
        3. Content-based: Memories with important content patterns
        4. Type-based: Certain memory types are prioritized
        """
        from datetime import datetime, timedelta
        
        try:
            # Get memory age
            if hasattr(memory_item, 'created_at') and memory_item.created_at:
                age = datetime.now() - memory_item.created_at
                age_hours = age.total_seconds() / 3600
            else:
                # If no timestamp, assume it's old enough
                age_hours = 25  # Just over 24 hours
            
            # Age-based consolidation
            if age_hours > 24:  # Memories older than 1 day
                return True
            
            # Access-based consolidation (if access tracking is available)
            access_count = memory_item.metadata.get("access_count", 0) if memory_item.metadata else 0
            if access_count > 3:  # Frequently accessed memories
                return True
            
            # Content-based consolidation - important content patterns
            content_lower = memory_item.content.lower()
            important_patterns = [
                "important", "remember", "note", "key", "critical", 
                "decision", "outcome", "result", "conclusion", "summary"
            ]
            if any(pattern in content_lower for pattern in important_patterns):
                return True
            
            # Type-based consolidation
            important_types = ["summary", "decision", "outcome", "user_preference", "system_config"]
            if memory_item.type in important_types:
                return True
            
            # Tag-based consolidation
            important_tags = ["important", "keep", "permanent", "user_data", "preferences"]
            memory_tags = memory_item.tags or []
            if any(tag in important_tags for tag in memory_tags):
                return True
            
            return False
            
        except Exception:
            # Default to not consolidating if evaluation fails
            return False
    
    def hybrid_retrieve(
        self,
        query: str,
        *,
        limit: int = 10,
        min_relevance: float = 0.5,
        include_recent: bool = True,
        include_vector: bool = True,
        conversation_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        context_window_hours: int = 24,
        motet_context: Optional[Any] = None,  # Optional override, auto-retrieved if not provided
    ) -> List[MemoryItem]:
        """
        Advanced hybrid retrieval combining multiple search strategies.
        
        This method combines:
        1. Recent memory search (temporal relevance)
        2. Vector similarity search (semantic relevance)  
        3. Tag-based filtering
        4. Relevance scoring and ranking
        
        Args:
            query: Search query text
            limit: Maximum number of results
            min_relevance: Minimum relevance score threshold
            include_recent: Include recent memories from KV store
            include_vector: Include vector similarity results
            conversation_id: Filter by conversation scope
            tags: Optional tags filter (match any)
            context_window_hours: Hours to look back for recent memories
            motet_context: Optional MotetContext override (auto-retrieved if not provided)
            
        Returns:
            List of MemoryItem objects ranked by hybrid relevance score
        """
        from datetime import datetime, timedelta, timezone
        import re
        
        results: Dict[str, MemoryItem] = {}
        scores: Dict[str, float] = {}
        tag_set = {t for t in (tags or []) if t}
        # Query+tags recall benefits from a broader recency window because
        # deep reports are often older than tool/audit memories in the same scope.
        recent_limit = limit * 2
        if query and tag_set:
            recent_limit = max(recent_limit, limit * 10, 30)
        recent_limit = min(recent_limit, 200)
        logger = structlog.get_logger(__name__)
        debug_stats: Dict[str, Any] = {
            "recent_limit": recent_limit,
            "recent_items_scanned": 0,
            "vector_items_scanned": 0,
            "filtered_time_window": 0,
            "filtered_conversation": 0,
            "filtered_tags": 0,
            "filtered_relevance": 0,
            "added_from_recent": 0,
            "added_from_vector": 0,
            "combined_from_both": 0,
        }
        
        # 1. Recent memory search (temporal + keyword relevance)
        identity_context = self._resolve_identity_context(
            motet_context=motet_context,
            operation="hybrid_retrieve",
        )
        mem = self._scoped_kv_store(identity_context)
        if include_recent and mem:
            try:
                # Get recent memories within context window
                cutoff_time = datetime.now(timezone.utc) - timedelta(hours=context_window_hours)
                recent_items = mem.recent(limit=recent_limit)
                debug_stats["recent_items_scanned"] = len(recent_items or [])
                
                for item in recent_items:
                    # Skip if outside time window
                    item_created_at = getattr(item, "created_at", None)
                    if item_created_at:
                        # Normalize mixed naive/aware datetimes to UTC to avoid
                        # TypeError and silent loss of the recent-query branch.
                        if getattr(item_created_at, "tzinfo", None) is None:
                            item_created_at = item_created_at.replace(tzinfo=timezone.utc)
                        if item_created_at < cutoff_time:
                            debug_stats["filtered_time_window"] += 1
                            continue
                        
                    # Conversation filtering
                    if conversation_id and f"{CONVERSATION_SCOPE_TAG_PREFIX}{conversation_id}" not in (item.tags or []):
                        debug_stats["filtered_conversation"] += 1
                        continue
                    # Tag filtering (match any requested tag)
                    if tag_set and not tag_set.intersection(set(item.tags or [])):
                        debug_stats["filtered_tags"] += 1
                        continue
                    
                    # Calculate keyword relevance (coverage, head-biased)
                    keyword_score = self._calculate_keyword_relevance(
                        query,
                        getattr(item, "content", "") or "",
                        metadata=getattr(item, "metadata", None),
                        tags=getattr(item, "tags", None),
                    )

                    # Calculate temporal relevance (more recent = higher score)
                    temporal_score = self._calculate_temporal_relevance(item)
                    
                    # Combined score for recent items
                    combined_score = (keyword_score * 0.6) + (temporal_score * 0.4)
                    
                    if combined_score >= min_relevance:
                        results[item.id] = item
                        scores[item.id] = combined_score
                        debug_stats["added_from_recent"] += 1
                    else:
                        debug_stats["filtered_relevance"] += 1
                        
            except Exception as e:
                logger.warning(
                    "memory_hybrid_retrieve_recent_failed",
                    operation="hybrid_retrieve",
                    error=str(e),
                )
        
        # 2. Vector similarity search (semantic relevance)
        if include_vector and getattr(self._stack, "vector", None):
            try:
                cfg = getattr(self._stack, "config", None)
                mode = self._resolve_memory_agent_scope_mode(cfg) if cfg else "prefer"
                agent_id = getattr(identity_context, "agent_id", None)
                # Only pass conversation_id when the caller requested it.
                # Implicit conversation scoping drops principal-scoped memories
                # (e.g. deep-research reports) that intentionally omit
                # conversation: tags so they survive across chats.
                vec_filters = {
                    "tenant_id": getattr(identity_context, "tenant_id", None),
                    "principal_id": getattr(identity_context, "principal_id", None),
                    "conversation_id": conversation_id,
                    "motet_id": getattr(identity_context, "motet_id", None),
                }
                use_agent = agent_id and (mode == "strict" or mode == "prefer")
                if use_agent:
                    vec_filters["agent_id"] = agent_id
                vector_results = self._stack.vector.query(query, top_k=limit, **vec_filters)
                # Prefer mode: if same-agent query returned nothing, retry without agent (cross-agent fallback)
                if mode == "prefer" and use_agent and not vector_results:
                    vec_filters.pop("agent_id", None)
                    vector_results = self._stack.vector.query(query, top_k=limit, **vec_filters)
                debug_stats["vector_items_scanned"] = len(vector_results or [])
                
                for item in vector_results:
                    # Conversation filtering
                    if conversation_id and f"{CONVERSATION_SCOPE_TAG_PREFIX}{conversation_id}" not in (item.tags or []):
                        debug_stats["filtered_conversation"] += 1
                        continue
                    # Tag filtering (match any requested tag)
                    if tag_set and not tag_set.intersection(set(item.tags or [])):
                        debug_stats["filtered_tags"] += 1
                        continue
                    
                    # Vector similarity score (assuming it's in metadata)
                    vector_score = item.metadata.get("search_score", 0.7)
                    
                    # If item already found in recent search, combine scores
                    if item.id in results:
                        # Boost score for items found in both searches
                        existing_score = scores[item.id]
                        combined_score = (existing_score * 0.5) + (vector_score * 0.5) + 0.1  # Boost
                        scores[item.id] = min(combined_score, 1.0)
                        debug_stats["combined_from_both"] += 1
                    else:
                        # New item from vector search
                        if vector_score >= min_relevance:
                            results[item.id] = item
                            scores[item.id] = vector_score
                            debug_stats["added_from_vector"] += 1
                        else:
                            debug_stats["filtered_relevance"] += 1
                            
            except Exception as e:
                logger.warning(
                    "memory_hybrid_retrieve_vector_failed",
                    operation="hybrid_retrieve",
                    error=str(e),
                )
        
        # 3. Rank and return results
        ranked_items = []
        for item_id, item in results.items():
            item.metadata = item.metadata or {}
            item.metadata["hybrid_score"] = scores[item_id]
            ranked_items.append(item)
        
        # Sort by hybrid score (descending)
        ranked_items.sort(key=lambda x: x.metadata.get("hybrid_score", 0), reverse=True)
        
        ranked_items = self._apply_agent_scope(items=ranked_items, identity_context=identity_context, cfg=getattr(self._stack, "config", None))
        returned = ranked_items[:limit]
        logger.info(
            "memory_hybrid_retrieve_stats",
            operation="hybrid_retrieve",
            tenant_id=getattr(identity_context, "tenant_id", None),
            motet_id=getattr(identity_context, "motet_id", None),
            principal_id=getattr(identity_context, "principal_id", None),
            conversation_id=conversation_id,
            query_preview=(query or "")[:80],
            query_len=len(query or ""),
            limit=limit,
            min_relevance=min_relevance,
            include_recent=include_recent,
            include_vector=include_vector,
            tag_count=len(tag_set),
            **debug_stats,
            returned_count=len(returned),
        )
        return returned
    
    @staticmethod
    def _tokenize_relevance_words(text: str) -> Set[str]:
        """Whole-word tokens for keyword relevance (drops stopwords / 1-char noise)."""
        return {
            w
            for w in _KEYWORD_WORD_RE.findall((text or "").lower())
            if len(w) > 1 and w not in _KEYWORD_STOPWORDS
        }

    def _calculate_keyword_relevance(
        self,
        query: str,
        content: str,
        *,
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
    ) -> float:
        """
        Query-coverage relevance, biased toward where a document names its subject.

        Returns ``|Q ∩ D| / |Q|`` over whole-word tokens (length-independent),
        combining a head/topic/tags haystack (weight 0.75) with the full body
        (weight 0.25). Buried single-word hits therefore cannot clear typical
        ``min_relevance`` floors (0.4–0.5) on their own, while a report whose
        opening or ``metadata.topic`` names the query still scores near 1.0
        regardless of body length. Jaccard similarity was the previous metric
        and collapsed toward zero on long research reports even when every
        query term was present.
        """
        query_words = self._tokenize_relevance_words(query)
        if not query_words:
            return 0.0

        meta = metadata if isinstance(metadata, dict) else {}
        head_haystack = " ".join(
            [
                (content or "")[:_KEYWORD_HEAD_CHARS],
                str(meta.get("topic") or ""),
                " ".join(str(t) for t in (tags or [])),
            ]
        )
        head_words = self._tokenize_relevance_words(head_haystack)
        body_words = self._tokenize_relevance_words(content or "")

        head_coverage = sum(1 for w in query_words if w in head_words) / len(query_words)
        body_coverage = sum(1 for w in query_words if w in body_words) / len(query_words)
        score = (0.75 * head_coverage) + (0.25 * body_coverage)

        phrase = (query or "").strip().lower()
        if phrase and phrase in (content or "").lower():
            score = min(score + 0.15, 1.0)

        return score
    
    def _calculate_temporal_relevance(self, item: MemoryItem) -> float:
        """Calculate temporal relevance score based on recency."""
        if not hasattr(item, 'created_at') or not item.created_at:
            return 0.5  # Default score for items without timestamps
            
        from datetime import datetime, timezone
        
        now = datetime.now(timezone.utc)
        created_at = item.created_at
        if getattr(created_at, "tzinfo", None) is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        age = now - created_at
        
        # Exponential decay: more recent = higher score
        # Items from last hour get score ~0.9
        # Items from last day get score ~0.7  
        # Items from last week get score ~0.3
        hours_old = age.total_seconds() / 3600
        temporal_score = max(0.1, 0.9 * (0.95 ** hours_old))
        
        return min(temporal_score, 1.0)
    
    def apply_vector_recall(
        self,
        messages: List[Any],
        query: str,
        *,
        max_context_items: int = 5,
        min_relevance: float = 0.6,
        conversation_id: Optional[str] = None,
        motet_context: Optional[Any] = None,  # Optional override, auto-retrieved if not provided
        memory_items: Optional[List[Any]] = None,  # Pre-retrieved items — skips internal hybrid_retrieve when set
    ) -> List[Any]:
        """
        Apply vector-based memory recall to enhance message context.

        This method:
        1. Performs hybrid retrieval based on the query (or uses pre-retrieved items)
        2. Formats retrieved memories as context
        3. Inserts context into the message list appropriately

        Args:
            messages: List of message objects to enhance
            query: Query for memory retrieval
            max_context_items: Maximum context items to include
            min_relevance: Minimum relevance threshold
            conversation_id: Conversation ID for filtering
            motet_context: Optional MotetContext override (auto-retrieved if not provided)
            memory_items: Optional pre-retrieved MemoryItems. When provided, skips the
                internal hybrid_retrieve call and uses these items directly. Callers that
                have already retrieved memory (e.g. the prepare_context memory stage)
                should pass them here to avoid doubling vector lookup cost (#132).

        Returns:
            Enhanced message list with memory context
        """
        try:
            # Use pre-retrieved items when provided; otherwise perform hybrid retrieval
            if memory_items is not None:
                # Apply injection-level constraints (max_context_items, min_relevance)
                # on the pre-retrieved pool so callers don't need to re-query.
                # Empty list means "already retrieved, nothing relevant" — do not re-fetch.
                filtered = []
                for item in memory_items:
                    score = getattr(item, "metadata", {}).get("hybrid_score", 0.0) if hasattr(item, "metadata") else 0.0
                    if score >= min_relevance:
                        filtered.append(item)
                if len(filtered) > max_context_items:
                    # Preserve original ranking (caller's `hybrid_retrieve` already ranked them)
                    filtered = filtered[:max_context_items]
                relevant_memories = filtered
            else:
                relevant_memories = self.hybrid_retrieve(
                    query=query,
                    limit=max_context_items,
                    min_relevance=min_relevance,
                    conversation_id=conversation_id,
                    motet_context=motet_context,
                )
            
            if not relevant_memories:
                return messages
            
            # Format memories as context
            context_parts = []
            for memory in relevant_memories:
                score = memory.metadata.get("hybrid_score", 0.0)
                context_parts.append(f"[Relevance: {score:.2f}] {memory.content}")
            
            context_content = "Relevant context from memory:\n" + "\n".join(context_parts)
            
            # Create context message
            from ..types import Message
            context_message = Message(
                role="system",
                content=context_content,
                # cache_volatile: recall results change per turn — provider adapters
                # keep this out of the cached stable system prefix (ADR-0124).
                metadata={"source": "memory_recall", "cache_volatile": True},
            )
            
            # Insert context before the last user message
            enhanced_messages = list(messages)
            if enhanced_messages and enhanced_messages[-1].role == "user":
                enhanced_messages.insert(-1, context_message)
            else:
                enhanced_messages.append(context_message)
            
            return enhanced_messages
            
        except Exception as e:
            # Return original messages if context enhancement fails
            return messages


__all__ = ["MemoryManager"]


