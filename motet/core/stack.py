"""
Motet - Core MotetStack Implementation

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Core MotetStack class that provides the main interface for the distributed AI framework.
    Handles memory management, tool registry, vector stores, and orchestration coordination.

Dependencies:
    - Orchestration and agent modules
    - Memory management (Redis, in-memory, vector stores)
    - Tool registry
    - Embedding service when vector memory is enabled

Usage:
    from motet.core.stack import MotetStack
    
    stack = MotetStack()
    response = await stack.chat(messages)

Notes:
    - Supports multiple memory backends (Redis, in-memory, vector stores)
    - When ``enable_vector_memory`` is true, pass ``embedding_fn`` and ``embedding_dim``
      from the worker embedding service so Valkey LTM does not load SentenceTransformer
      locally. If omitted, MotetStack builds an EmbeddingService from config
      (sibling topology when MOTET_EMBEDDING_ENDPOINT is set).
    - Integrates with distributed orchestration system
    - Provides fallback mechanisms for memory initialization
    - Includes turn counter for conversation tracking
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from .config import Config
from .memory import InMemoryStore, RedisStore, store_registry
from .memory.manager import MemoryManager
from .types import Message, Response
from .orchestration import Orchestrator
from .tools import registry as _tool_registry
from .constants import DEFAULT_REDIS_URL


class MotetStack:
    def __init__(
        self,
        config: Config | None = None,
        *,
        embedding_fn: Optional[Callable[..., Any]] = None,
        embedding_dim: Optional[int] = None,
    ) -> None:
        self.config = config or Config()
        self._turn_counter: int = 0
        
        # Make tool registry available to reasoning strategies
        self.tool_registry = _tool_registry
        if self.config.enable_memory:
            try:
                mem_backend = getattr(self.config, "memory_backend", "inmemory") or "inmemory"
                if mem_backend == "redis" and self.config.redis_url:
                    # Use UnifiedRedisManager for gevent/eventlet compatibility (ADR-0033)
                    from .distributed.redis_manager import get_redis_manager
                    redis_manager = get_redis_manager()
                    # Use sync binary client for memory operations (decode_responses=False for manual decoding)
                    # Binary client required because RedisStore expects raw bytes (redis_store.py:73)
                    client = redis_manager.get_sync_binary_client("memory_store")
                    
                    # Multi-motet/multi-tenant isolation (ADR-0027)
                    motet_id = getattr(self.config, "motet_id", None) or "default"
                    tenant_id = getattr(self.config, "tenant_id", None) or "default"
                    
                    self.memory = store_registry.build(
                        "memory", 
                        "redis", 
                        redis_client=client,
                        motet_id=motet_id,
                        tenant_id=tenant_id
                    )  # type: ignore[arg-type]
                else:
                    ttl = getattr(self.config, "memory_ttl_seconds", None)
                    self.memory = store_registry.build("memory", "inmemory", ttl_seconds=ttl)
            except Exception as exc:
                # fallback to in-memory if anything fails, and log
                ttl = getattr(self.config, "memory_ttl_seconds", None)
                try:
                    import structlog
                    structlog.get_logger().warning("memory_init_failed", error=str(exc))
                except Exception:
                    pass  # logging must not crash fallback path
                self.memory = InMemoryStore(ttl_seconds=ttl)
        else:
            self.memory = None
        # Optional vector memory (Valkey Search only - ADR-0092)
        self.vector = None
        if getattr(self.config, "enable_vector_memory", False):
            try:
                # ADR-0107: never fall back to in-image SentenceTransformer when the
                # caller omitted embedding_fn (API process, ad-hoc stacks). Build the
                # shared EmbeddingService from config/env (sibling server in compose).
                if embedding_fn is None:
                    from .embedding import create_embedding_service

                    embedding_service = create_embedding_service(
                        default_model=(
                            getattr(self.config, "embedding_text_model", None)
                            or self.config.embedding_model
                        ),
                        topology=getattr(self.config, "embedding_topology", None),
                        endpoint=getattr(self.config, "embedding_endpoint", None),
                        request_timeout_seconds=getattr(
                            self.config, "embedding_request_timeout_seconds", None
                        ),
                        max_attempts=getattr(self.config, "embedding_request_max_attempts", None),
                        retry_backoff_seconds=getattr(
                            self.config, "embedding_retry_backoff_seconds", None
                        ),
                        circuit_breaker_failure_threshold=getattr(
                            self.config,
                            "embedding_circuit_breaker_failure_threshold",
                            None,
                        ),
                        circuit_breaker_recovery_timeout_seconds=getattr(
                            self.config,
                            "embedding_circuit_breaker_recovery_timeout_seconds",
                            None,
                        ),
                    )
                    embedding_fn = embedding_service.embed
                    embedding_dim = embedding_service.get_embedding_dimension()

                vector_kwargs: dict[str, Any] = {
                    "index_name": getattr(self.config, "memory_vector_valkey_index", None),
                    "key_prefix": getattr(self.config, "memory_vector_valkey_prefix", None),
                    "redis_client_id": getattr(
                        self.config, "memory_vector_redis_client_id", "memory_vector_valkey"
                    ),
                    "embedding_model": self.config.embedding_model,
                    "enable_embedding_cache": getattr(self.config, "enable_embedding_cache", True),
                    "enable_result_cache": getattr(self.config, "enable_result_cache", False),
                    "embedding_fn": embedding_fn,
                    "embedding_dim": embedding_dim,
                }
                self.vector = store_registry.build(
                    "vector",
                    "valkey",
                    **vector_kwargs,
                )
            except Exception as exc:
                try:
                    import structlog
                    structlog.get_logger().warning("vector_init_failed", error=str(exc))
                except Exception:
                    pass  # logging must not crash fallback path
                self.vector = None
        # Orchestrator (mandatory) - Always distributed
        import os
        from .orchestration import OrchestrationConfig
        
        # Always use Redis URL from environment (defaults to standard Redis port)
        redis_url = os.getenv('MOTET_REDIS_URL', DEFAULT_REDIS_URL)
        
        # Create orchestrator configuration (always distributed)
        orchestrator_config = OrchestrationConfig(
            enable_observers=bool(getattr(self.config, "events_enabled", True))
        )
        
        # Initialize the unified orchestrator (always distributed)
        self.orchestrator = Orchestrator(
            redis_url=redis_url,
            config=orchestrator_config
        )
        
        # Initialize the orchestrator (ADR-0033: gevent/eventlet compatible)
        from .utils.async_helpers import run_async_safe
        try:
            run_async_safe(self.orchestrator.initialize(self))
        except Exception as e:
            import structlog
            try:
                structlog.get_logger().warning("orchestrator_init_partial_failure", error=str(e))
            except Exception:
                pass  # logging must not crash init path
        
        print(f"Stack initialized with Orchestrator (distributed mode, redis_url={redis_url})")
        # Memory manager
        try:
            self.memory_manager = MemoryManager(self)
        except Exception:
            self.memory_manager = None
        _tool_registry.set_config(self.config)

    # Removed message-prep helpers; orchestrator now owns prep

    async def chat(self, messages: List[Message], context: Optional[dict] = None) -> Response:
        self._turn_counter += 1
        # Initialize orchestrator if not already done
        if not hasattr(self.orchestrator, '_initialized'):
            await self.orchestrator.initialize(self)
            self.orchestrator._initialized = True
        
        # Use the orchestrator's native run method with context
        return await self.orchestrator.run(self, messages, context=context)

    # removed: store_last_user_message now handled in orchestrator


__all__ = ["MotetStack"]


