"""
Motet - Unified Redis Manager

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Centralized Redis connection manager for all distributed services in the Motet.
    Provides unified Redis operations with connection pooling, automatic reconnection,
    and both synchronous and asynchronous client support. Includes structured data
    storage, distributed locks, and comprehensive error handling.

Dependencies:
    - redis.asyncio: Asynchronous Redis client (pubsub + fallback)
    - redis: Synchronous Redis client (Celery-adjacent fallback)
    - valkey-glide / valkey-glide-sync: optional application client
    - pydantic: Data validation and configuration models
    - asyncio: Asynchronous I/O operations
    - typing: Type hints and annotations

Usage:
    from motet.core.distributed.redis_manager import UnifiedRedisManager, store_structured_data
    
    # Create manager
    manager = UnifiedRedisManager()
    
    # Store structured data
    await store_structured_data("service_name", "key", data, format_type="hash")
    
    # Get distributed lock
    lock = await acquire_distributed_lock("service_name", "lock_key", ttl_seconds=90)

Notes:
    - Provides connection pooling for both sync and async operations
    - Includes event loop safe initialization and resource cleanup
    - Supports structured data storage with multiple format types (hash, json_string)
    - Includes distributed lock management with TTL and renewal
    - Provides centralized Redis operations to prevent data type inconsistencies
    - Supports automatic reconnection and comprehensive error handling
    - Integrates with distributed architecture for state management
    - Application get/set/hash/zset/BLPOP use redis-py unless
      MOTET_VALKEY_CLIENT=glide. Pub/Sub objects, pipelines, and Celery
      broker/backend stay on redis-py.
    - Sync GLIDE is one process-wide client. Adapter close / health-check
      eviction must not close it; a closed client is recreated on next use.
"""


import os
import asyncio
import ssl
import base64

from motet.core.constants import (
    DEFAULT_REDIS_URL,
    REDIS_MAX_CONNECTIONS,
    REDIS_PUBSUB_MAX_CONNECTIONS,
)
from typing import Optional, Dict, Any, Union, List
import redis.asyncio as async_redis
import redis
import redis as sync_redis
import structlog

logger = structlog.get_logger(__name__)

from pydantic import BaseModel, Field


class RedisConfig(BaseModel):
    """Redis connection configuration."""
    url: str
    max_connections: int = REDIS_MAX_CONNECTIONS  # ≥ Celery concurrency + short-op headroom (ADR-0131)
    pubsub_max_connections: int = REDIS_PUBSUB_MAX_CONNECTIONS
    retry_on_timeout: bool = True
    decode_responses: bool = True  # Fixed: Default to True to avoid bytes serialization issues
    socket_keepalive: bool = True
    socket_keepalive_options: Optional[Dict[str, Any]] = Field(default_factory=dict)


class UnifiedRedisManager:
    """
    Centralized Redis connection manager for all distributed services.
    
    Features:
    - Connection pooling for performance (both sync and async)
    - Event loop safe initialization
    - Consistent error handling
    - Automatic reconnection
    - Resource cleanup
    - Support for both synchronous and asynchronous clients
    """
    
    def __init__(self, config: Optional[RedisConfig] = None):
        self.config = config or self._get_default_config()
        self._async_connection_pool: Optional[async_redis.ConnectionPool] = None
        self._sync_connection_pool: Optional[sync_redis.ConnectionPool] = None
        self._pubsub_connection_pool: Optional[async_redis.ConnectionPool] = None
        self._async_clients: Dict[str, Any] = {}
        self._sync_clients: Dict[str, Any] = {}
        self._pubsub_clients: Dict[str, async_redis.Redis] = {}
        self._initialized = False
        self._encryption_service = None  # Lazy-loaded encryption service singleton
        self._valkey_backend = "redis"
        self._shared_sync_glide: Any = None
    
    @staticmethod
    def _resolve_max_connections(default: int = REDIS_MAX_CONNECTIONS) -> int:
        """Resolve command-pool size from MOTET_REDIS_MAX_CONNECTIONS."""
        raw = os.getenv("MOTET_REDIS_MAX_CONNECTIONS")
        if not raw:
            return default
        try:
            return max(1, int(raw))
        except ValueError:
            logger.warning(
                "Invalid MOTET_REDIS_MAX_CONNECTIONS; using default",
                value=raw,
                default=default,
            )
            return default

    @staticmethod
    def _resolve_pubsub_max_connections(default: int = REDIS_PUBSUB_MAX_CONNECTIONS) -> int:
        """Resolve pub/sub pool size from MOTET_REDIS_PUBSUB_MAX_CONNECTIONS."""
        raw = os.getenv("MOTET_REDIS_PUBSUB_MAX_CONNECTIONS")
        if not raw:
            return default
        try:
            return max(1, int(raw))
        except ValueError:
            logger.warning(
                "Invalid MOTET_REDIS_PUBSUB_MAX_CONNECTIONS; using default",
                value=raw,
                default=default,
            )
            return default

    def _get_default_config(self) -> RedisConfig:
        """Get default Redis configuration from environment."""
        redis_url = os.getenv('MOTET_REDIS_URL', DEFAULT_REDIS_URL)
        return RedisConfig(
            url=redis_url,
            max_connections=self._resolve_max_connections(),
            pubsub_max_connections=self._resolve_pubsub_max_connections(),
            retry_on_timeout=True,
            decode_responses=True,  # Decode responses to strings for easier handling
            socket_keepalive=False,  # Disable keepalive to avoid socket option issues
            socket_keepalive_options={}
        )

    def _build_connection_kwargs(self, *, max_connections: int, decode_responses: bool) -> Dict[str, Any]:
        """Build shared redis-py pool kwargs."""
        ssl_params = self._get_ssl_params()
        connection_kwargs: Dict[str, Any] = {
            "max_connections": max_connections,
            "retry_on_timeout": self.config.retry_on_timeout,
            "decode_responses": decode_responses,
            "socket_keepalive": self.config.socket_keepalive,
            "socket_keepalive_options": self.config.socket_keepalive_options,
            "health_check_interval": 30,
        }
        connection_kwargs.update(ssl_params)
        return connection_kwargs

    def _get_ssl_params(self) -> Dict[str, Any]:
        """Build SSL params for redis-py connection pools based on environment variables."""
        ssl_params: Dict[str, Any] = {}
        
        # Only add SSL params if using rediss:// URL
        redis_url = os.getenv('MOTET_REDIS_URL', DEFAULT_REDIS_URL)
        if not redis_url.startswith('rediss://'):
            return ssl_params
        
        ca_path = os.getenv('MOTET_REDIS_CA_CERT')
        if ca_path:
            ssl_params['ssl_ca_certs'] = ca_path

        cert_reqs = os.getenv('MOTET_REDIS_SSL_CERT_REQS')
        if cert_reqs:
            normalized = cert_reqs.upper()
            if normalized in {'NONE', 'OPTIONAL', 'REQUIRED'}:
                ssl_params['ssl_cert_reqs'] = getattr(ssl, f'CERT_{normalized}')
            else:
                ssl_params['ssl_cert_reqs'] = cert_reqs
        
        # For development with self-signed certs, disable hostname checking
        if cert_reqs and cert_reqs.upper() == 'NONE':
            ssl_params['ssl_check_hostname'] = False
        
        logger.debug("SSL params for Redis", ssl_params=ssl_params, redis_url=redis_url)
        return ssl_params
    
    def initialize(self) -> None:
        """
        Initialize Redis connection pools (synchronous, event-loop safe).
        
        This method is synchronous and can be called from any context,
        including during module import or class construction.
        Creates both async and sync connection pools.
        """
        if self._initialized:
            return
            
        try:
            connection_kwargs = self._build_connection_kwargs(
                max_connections=self.config.max_connections,
                decode_responses=self.config.decode_responses,
            )
            pubsub_connection_kwargs = self._build_connection_kwargs(
                max_connections=self.config.pubsub_max_connections,
                decode_responses=self.config.decode_responses,
            )
            
            # Create async connection pool
            self._async_connection_pool = async_redis.ConnectionPool.from_url(
                self.config.url,
                **connection_kwargs
            )
            
            # Create sync connection pool
            self._sync_connection_pool = sync_redis.ConnectionPool.from_url(
                self.config.url,
                **connection_kwargs
            )

            # Dedicated async pool for long-lived pub/sub listeners (SSE, observers)
            self._pubsub_connection_pool = async_redis.ConnectionPool.from_url(
                self.config.url,
                **pubsub_connection_kwargs
            )
            
            from .glide_backend import resolve_valkey_client_backend

            self._valkey_backend = resolve_valkey_client_backend()
            self._initialized = True
            logger.info(
                "Redis connection pools initialized (async + sync + pubsub)",
                url=self.config.url,
                max_connections=self.config.max_connections,
                pubsub_max_connections=self.config.pubsub_max_connections,
                valkey_client=self._valkey_backend,
            )
            
        except Exception as e:
            logger.error("Redis connection pool initialization failed", error=str(e))
            raise
    
    def get_client(self, client_id: str = "default") -> async_redis.Redis:
        """
        Get an async Redis client instance (synchronous, event-loop safe).
        
        Args:
            client_id: Unique identifier for this client instance
            
        Returns:
            Async Redis client ready for async operations
        """
        if not self._initialized:
            self.initialize()
        
        if client_id not in self._async_clients:
            redis_client = async_redis.Redis(connection_pool=self._async_connection_pool)
            if self._valkey_backend == "glide":
                from .glide_backend import create_async_glide_adapter

                self._async_clients[client_id] = create_async_glide_adapter(
                    self.config.url,
                    decode_responses=True,
                    fallback=redis_client,
                )
            else:
                self._async_clients[client_id] = redis_client
        
        return self._async_clients[client_id]
    
    def get_pubsub_client(self, client_id: str = "pubsub_default") -> async_redis.Redis:
        """
        Get an async Redis client backed by the dedicated pub/sub pool.

        Long-lived pub/sub listeners (SSE streams, event observers) must use this
        pool so they do not exhaust the command connection pool used by auth,
        command dispatch, and other short-lived operations.
        """
        if not self._initialized:
            self.initialize()

        if client_id not in self._pubsub_clients:
            if self._pubsub_connection_pool is None:
                raise RuntimeError("Pub/sub Redis connection pool is not initialized")
            self._pubsub_clients[client_id] = async_redis.Redis(
                connection_pool=self._pubsub_connection_pool
            )

        return self._pubsub_clients[client_id]
    
    def get_sync_client(self, client_id: str = "default") -> sync_redis.Redis:
        """
        Get a synchronous Redis client instance.
        
        Args:
            client_id: Unique identifier for this client instance
            
        Returns:
            Sync Redis client ready for synchronous operations
        """
        if not self._initialized:
            self.initialize()
        
        if client_id not in self._sync_clients:
            redis_client = sync_redis.Redis(connection_pool=self._sync_connection_pool)
            if self._valkey_backend == "glide":
                from .glide_backend import SyncGlideRedisAdapter

                self._sync_clients[client_id] = SyncGlideRedisAdapter(
                    self._ensure_shared_sync_glide(),
                    decode_responses=True,
                    fallback=redis_client,
                )
            else:
                self._sync_clients[client_id] = redis_client
        
        return self._sync_clients[client_id]
    
    def get_binary_client(self, client_id: str = "binary_default") -> async_redis.Redis:
        """
        Get an async Redis client instance configured for binary data.
        
        This client has decode_responses=False to handle binary data like MsgPack.
        
        Args:
            client_id: Unique identifier for this client instance
            
        Returns:
            Async Redis client configured for binary data operations
        """
        if not self._initialized:
            self.initialize()
        
        # Use a different key prefix to avoid conflicts with regular clients
        binary_client_id = f"binary_{client_id}"
        
        if binary_client_id not in self._async_clients:
            pool_kwargs = self._build_connection_kwargs(
                max_connections=self.config.max_connections,
                decode_responses=False,
            )
            binary_pool = async_redis.ConnectionPool.from_url(
                self.config.url,
                **pool_kwargs
            )
            
            redis_client = async_redis.Redis(connection_pool=binary_pool)
            if self._valkey_backend == "glide":
                from .glide_backend import create_async_glide_adapter

                self._async_clients[binary_client_id] = create_async_glide_adapter(
                    self.config.url,
                    decode_responses=False,
                    fallback=redis_client,
                )
            else:
                self._async_clients[binary_client_id] = redis_client
        
        return self._async_clients[binary_client_id]
    
    def get_sync_binary_client(self, client_id: str = "binary_default") -> sync_redis.Redis:
        """
        Get a synchronous Redis client instance configured for binary data.
        
        This client has decode_responses=False to handle binary data like MsgPack.
        
        Args:
            client_id: Unique identifier for this client instance
            
        Returns:
            Sync Redis client configured for binary data operations
        """
        if not self._initialized:
            self.initialize()
        
        # Use a different key prefix to avoid conflicts with regular clients
        binary_client_id = f"sync_binary_{client_id}"
        
        if binary_client_id not in self._sync_clients:
            pool_kwargs = self._build_connection_kwargs(
                max_connections=self.config.max_connections,
                decode_responses=False,
            )
            binary_pool = sync_redis.ConnectionPool.from_url(
                self.config.url,
                **pool_kwargs
            )
            
            redis_client = sync_redis.Redis(connection_pool=binary_pool)
            if self._valkey_backend == "glide":
                from .glide_backend import SyncGlideRedisAdapter

                self._sync_clients[binary_client_id] = SyncGlideRedisAdapter(
                    self._ensure_shared_sync_glide(),
                    decode_responses=False,
                    fallback=redis_client,
                )
            else:
                self._sync_clients[binary_client_id] = redis_client
        
        return self._sync_clients[binary_client_id]

    def _evict_sync_glide_adapters(self) -> None:
        """Drop adapter wrappers after the shared GLIDE client is closed or replaced."""
        from .glide_backend import SyncGlideRedisAdapter

        for client_id in list(self._sync_clients.keys()):
            adapter = self._sync_clients[client_id]
            if isinstance(adapter, SyncGlideRedisAdapter):
                try:
                    adapter.close()
                except Exception:
                    pass
                del self._sync_clients[client_id]

    def _ensure_shared_sync_glide(self) -> Any:
        """Return the process-wide sync GLIDE client, recreating it if closed."""
        from .glide_backend import create_sync_glide_client, glide_client_is_closed

        if self._shared_sync_glide is not None and not glide_client_is_closed(
            self._shared_sync_glide
        ):
            return self._shared_sync_glide
        if self._shared_sync_glide is not None:
            logger.warning("shared_sync_glide_closed_recreating")
            self._evict_sync_glide_adapters()
        self._shared_sync_glide = create_sync_glide_client(self.config.url)
        return self._shared_sync_glide
    
    def _get_encryption_service(self):
        """Get encryption service singleton (lazy-loaded)."""
        if self._encryption_service is None:
            try:
                from ..security.encryption_service import get_encryption_service
                self._encryption_service = get_encryption_service()
            except Exception as e:
                logger.warning("Failed to initialize encryption service",
                             error=str(e))
                self._encryption_service = None
        return self._encryption_service
    
    async def health_check(self, client_id: str = "default") -> bool:
        """Check if Redis connection is healthy."""
        try:
            client = self.get_client(client_id)
            await client.ping()
            return True
        except Exception as e:
            logger.warning("Redis health check failed", client_id=client_id, error=str(e))
            return False
    
    def health_check_sync(self, client_id: str = "default") -> bool:
        """Check if synchronous Redis connection is healthy."""
        try:
            client = self.get_sync_client(client_id)
            client.ping()
            return True
        except Exception as e:
            logger.warning("Redis sync health check failed", client_id=client_id, error=str(e))
            # Evict this adapter wrapper only. SyncGlideRedisAdapter.close()
            # must not close the process-wide GLIDE client.
            if client_id in self._sync_clients:
                try:
                    self._sync_clients[client_id].close()
                except Exception:
                    pass  # best-effort cleanup; client is being evicted
                del self._sync_clients[client_id]
            if self._valkey_backend == "glide":
                from .glide_backend import glide_client_is_closed

                if glide_client_is_closed(self._shared_sync_glide):
                    self._shared_sync_glide = None
                    self._evict_sync_glide_adapters()
            return False
    
    async def close_client(self, client_id: str) -> None:
        """Close a specific Redis client."""
        if client_id in self._async_clients:
            try:
                await self._async_clients[client_id].close()
                del self._async_clients[client_id]
                logger.debug("Closed Redis client", client_id=client_id)
            except Exception as e:
                logger.warning("Error closing Redis client", client_id=client_id, error=str(e))
    
    async def close_all(self) -> None:
        """Close all Redis clients and connection pools."""
        for client_id in list(self._async_clients.keys()):
            await self.close_client(client_id)

        for client_id, client in list(self._pubsub_clients.items()):
            try:
                await client.aclose()
            except Exception as e:
                logger.warning("Error closing pub/sub Redis client", client_id=client_id, error=str(e))
            finally:
                self._pubsub_clients.pop(client_id, None)
        
        if self._async_connection_pool:
            try:
                await self._async_connection_pool.disconnect()
                self._async_connection_pool = None
                logger.debug("Redis async connection pool closed")
            except Exception as e:
                logger.warning("Error closing Redis async connection pool", error=str(e))

        if self._pubsub_connection_pool:
            try:
                await self._pubsub_connection_pool.disconnect()
                self._pubsub_connection_pool = None
                logger.debug("Redis pub/sub connection pool closed")
            except Exception as e:
                logger.warning("Error closing Redis pub/sub connection pool", error=str(e))

        if self._shared_sync_glide is not None:
            closer = getattr(self._shared_sync_glide, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception as e:
                    logger.warning("Error closing shared sync GLIDE client", error=str(e))
            self._shared_sync_glide = None
        self._evict_sync_glide_adapters()
        
        self._initialized = False
    
    async def scan_keys(self, pattern: str, client_id: str = "default", limit: Optional[int] = None) -> List[str]:
        """
        Scan Redis keys matching a pattern.
        
        Args:
            pattern: Redis key pattern to match (e.g., "cmd:meta:*")
            client_id: Redis client ID to use
            limit: Maximum number of keys to return (optional)
            
        Returns:
            List of matching keys
        """
        try:
            client = self.get_client(client_id)
            keys = []
            
            async for key in client.scan_iter(match=pattern, count=1000):
                keys.append(key)
                if limit and len(keys) >= limit:
                    break
            
            return keys
            
        except Exception as e:
            logger.warning("Redis key scan failed", pattern=pattern, error=str(e))
            return []
    
    def scan_keys_sync(self, pattern: str, client_id: str = "default", limit: Optional[int] = None) -> List[str]:
        """
        Scan Redis keys matching a pattern (synchronous version).
        
        Args:
            pattern: Redis key pattern to match (e.g., "cmd:meta:*")
            client_id: Redis client ID to use
            limit: Maximum number of keys to return (optional)
            
        Returns:
            List of matching keys
        """
        try:
            client = self.get_sync_client(client_id)
            keys = []
            
            for key in client.scan_iter(match=pattern, count=1000):
                keys.append(key)
                if limit and len(keys) >= limit:
                    break
            
            return keys
            
        except Exception as e:
            logger.warning("Redis key scan failed", pattern=pattern, error=str(e))
            return []

    def get_stats(self) -> Dict[str, Any]:
        """Get connection pool statistics."""
        if not self._async_connection_pool:
            return {"status": "not_initialized"}
        
        return {
            "status": "initialized",
            "max_connections": self.config.max_connections,
            "pubsub_max_connections": self.config.pubsub_max_connections,
            "active_clients": len(self._async_clients),
            "active_pubsub_clients": len(self._pubsub_clients),
            "client_ids": list(self._async_clients.keys()),
            "pubsub_client_ids": list(self._pubsub_clients.keys()),
            "redis_url": self.config.url,
            "valkey_client": self._valkey_backend,
        }


def warn_if_redis_pool_below_concurrency(
    *,
    max_connections: int,
    concurrency: int,
) -> bool:
    """Warn when the sync Redis pool cannot cover parked BLPOP waiters (ADR-0131).

    Parked waiters hold one pool connection each and cannot exceed Celery
    concurrency, so ``max_connections <= concurrency`` means short ops (cancel
    EXISTS, result-done, sticky writes) have no headroom. Returns True when
    undersized. Does not raise — sizing is a deploy concern.
    """
    if concurrency <= 0:
        return False
    if max_connections > concurrency:
        return False
    logger.warning(
        "redis_pool_undersized_for_celery_concurrency",
        max_connections=max_connections,
        celery_concurrency=concurrency,
        hint=(
            "Set MOTET_REDIS_MAX_CONNECTIONS greater than Celery --concurrency "
            "so BLPOP waiters do not starve cancel/result probes (ADR-0131)"
        ),
    )
    return True


# Global Redis manager instance
_redis_manager: Optional[UnifiedRedisManager] = None


def get_redis_manager() -> UnifiedRedisManager:
    """
    Get the global Redis manager instance.
    
    This function is synchronous and event-loop safe.
    It can be called from any context including module imports.
    """
    global _redis_manager
    
    if _redis_manager is None:
        _redis_manager = UnifiedRedisManager()
        _redis_manager.initialize()
    
    return _redis_manager


def get_redis_client(client_id: str = "default") -> async_redis.Redis:
    """
    Get an async Redis client instance (synchronous, event-loop safe).
    
    This is the main function that all services should use for async Redis access.
    
    Args:
        client_id: Unique identifier for this client (e.g., "worker_readiness", "state_registry")
        
    Returns:
        Async Redis client ready for async operations
    """
    manager = get_redis_manager()
    return manager.get_client(client_id)


def get_pubsub_redis_client(client_id: str = "pubsub_default") -> async_redis.Redis:
    """
    Get an async Redis client for long-lived pub/sub listeners.

    SSE streams and event observers should use this instead of get_redis_client()
    so command/auth traffic is not starved when many listeners are connected.
    """
    manager = get_redis_manager()
    return manager.get_pubsub_client(client_id)


def get_sync_redis_client(client_id: str = "default") -> sync_redis.Redis:
    """
    Get a synchronous Redis client instance.
    
    This is the main function that Celery tasks should use for sync Redis access.
    
    Args:
        client_id: Unique identifier for this client (e.g., "worker_readiness", "state_registry")
        
    Returns:
        Sync Redis client ready for synchronous operations
    """
    manager = get_redis_manager()
    return manager.get_sync_client(client_id)


def get_binary_redis_client(client_id: str = "binary_default") -> async_redis.Redis:
    """
    Get an async Redis client instance configured for binary data.
    
    This client has decode_responses=False to handle binary data like MsgPack.
    Use this for services that need to store/retrieve binary data in Redis.
    
    Args:
        client_id: Unique identifier for this client (e.g., "command_data_manager", "binary_storage")
        
    Returns:
        Async Redis client configured for binary data operations
    """
    manager = get_redis_manager()
    return manager.get_binary_client(client_id)


def get_sync_binary_redis_client(client_id: str = "binary_default") -> sync_redis.Redis:
    """
    Get a synchronous Redis client instance configured for binary data.
    
    This client has decode_responses=False to handle binary data like MsgPack.
    Use this for Celery tasks that need to store/retrieve binary data in Redis.
    
    Args:
        client_id: Unique identifier for this client (e.g., "command_data_manager", "binary_storage")
        
    Returns:
        Sync Redis client configured for binary data operations
    """
    manager = get_redis_manager()
    return manager.get_sync_binary_client(client_id)


async def redis_health_check(client_id: str = "default") -> bool:
    """Check Redis connection health."""
    manager = get_redis_manager()
    return await manager.health_check(client_id)


async def close_redis_client(client_id: str) -> None:
    """Close a specific Redis client."""
    manager = get_redis_manager()
    await manager.close_client(client_id)


async def close_all_redis_connections() -> None:
    """Close all Redis connections (for cleanup)."""
    global _redis_manager
    if _redis_manager:
        await _redis_manager.close_all()
        _redis_manager = None


def get_redis_stats() -> Dict[str, Any]:
    """Get Redis connection statistics."""
    manager = get_redis_manager()
    return manager.get_stats()


# Convenience functions for common patterns
def create_redis_client_for_service(service_name: str) -> async_redis.Redis:
    """
    Create a Redis client for a specific service.
    
    This is the recommended pattern for all distributed services.
    
    Args:
        service_name: Name of the service (e.g., "worker_readiness", "state_registry")
        
    Returns:
        Redis client ready for async operations
    """
    return get_redis_client(f"service_{service_name}")


# Standardized Redis Data Access Patterns
# These methods enforce consistent data formats across all services

async def store_structured_data(client_id: str, key: str, data: Dict[str, Any], 
                              format_type: str = "hash") -> None:
    """
    Store structured data with consistent format.
    
    Args:
        client_id: Redis client identifier
        key: Redis key
        data: Data to store
        format_type: "hash" (default) or "json_string"
    """
    import json
    
    client = get_redis_client(client_id)
    
    if format_type == "hash":
        # Convert all values to strings for Redis hash storage
        redis_data = {}
        for k, v in data.items():
            if v is None:
                # Store None as the string "None" (will be converted back in retrieval)
                redis_data[k] = "None"
            elif isinstance(v, bool):
                redis_data[k] = "true" if v else "false"
            elif isinstance(v, (list, dict)):
                redis_data[k] = json.dumps(v)
            else:
                redis_data[k] = str(v)
        
        await client.hset(key, mapping=redis_data)  # type: ignore[misc]
    
    elif format_type == "json_string":
        await client.set(key, json.dumps(data))  # type: ignore[misc]
    
    else:
        raise ValueError(f"Unsupported format_type: {format_type}")


async def retrieve_structured_data(client_id: str, key: str, 
                                 format_type: str = "hash") -> Optional[Dict[str, Any]]:
    """
    Retrieve structured data with consistent format.
    
    Args:
        client_id: Redis client identifier  
        key: Redis key
        format_type: "hash" (default) or "json_string"
        
    Returns:
        Parsed data dictionary or None if not found
    """
    import json
    
    client = get_redis_client(client_id)
    
    if format_type == "hash":
        data: Dict[str, Any] = await client.hgetall(key)  # type: ignore[assignment]
        if not data:
            return None
            
        # Convert bytes to strings and parse JSON fields
        result = {}
        for k, v in data.items():
            key_str = k.decode() if isinstance(k, bytes) else k
            val_str = v.decode() if isinstance(v, bytes) else v
            
            # Try to parse as JSON for lists/dicts
            if val_str.startswith(('[', '{')):
                try:
                    result[key_str] = json.loads(val_str)
                except json.JSONDecodeError:
                    result[key_str] = val_str
            # Parse booleans
            elif val_str.lower() in ('true', 'false'):
                result[key_str] = val_str.lower() == 'true'
            # Try to parse as numbers
            elif val_str.replace('.', '').replace('-', '').isdigit():
                try:
                    result[key_str] = int(val_str) if '.' not in val_str else float(val_str)
                except ValueError:
                    result[key_str] = val_str
            else:
                result[key_str] = val_str
                
        return result
    
    elif format_type == "json_string":
        raw: Optional[str] = await client.get(key)  # type: ignore[assignment]
        if not raw:
            return None
        return json.loads(raw)
    
    else:
        raise ValueError(f"Unsupported format_type: {format_type}")


def store_structured_data_sync(client_id: str, key: str, data: Dict[str, Any], 
                              format_type: str = "hash") -> None:
    """
    Synchronous version of store_structured_data for Celery tasks.
    Uses UnifiedRedisManager for consistent connection management.
    """
    import json
    
    # Use UnifiedRedisManager for consistent connection management
    sync_client = get_sync_redis_client(client_id)
    
    if format_type == "hash":
        # Convert all values to strings for Redis hash storage
        redis_data = {}
        for k, v in data.items():
            if v is None:
                # Store None as the string "None" (will be converted back in retrieval)
                redis_data[k] = "None"
            elif isinstance(v, bool):
                redis_data[k] = "true" if v else "false"
            elif isinstance(v, (list, dict)):
                redis_data[k] = json.dumps(v)
            else:
                redis_data[k] = str(v)
        
        sync_client.hset(key, mapping=redis_data)
    
    elif format_type == "json_string":
        sync_client.set(key, json.dumps(data))
    
    else:
        raise ValueError(f"Unsupported format_type: {format_type}")


def retrieve_structured_data_sync(client_id: str, key: str, 
                                format_type: str = "hash") -> Optional[Dict[str, Any]]:
    """
    Synchronous version of retrieve_structured_data for Celery tasks.
    Uses UnifiedRedisManager for consistent connection management.
    """
    import json
    
    # Use UnifiedRedisManager for consistent connection management
    sync_client = get_sync_redis_client(client_id)
    
    if format_type == "hash":
        hash_data: Dict[str, str] = sync_client.hgetall(key)  # type: ignore[assignment]
        if not hash_data:
            return None
            
        result: Dict[str, Any] = {}
        for k, v in hash_data.items():
            if v.startswith(('[', '{')):
                try:
                    result[k] = json.loads(v)
                except json.JSONDecodeError:
                    result[k] = v
            elif v.lower() in ('true', 'false'):
                result[k] = v.lower() == 'true'
            elif v.replace('.', '').replace('-', '').isdigit():
                try:
                    result[k] = int(v) if '.' not in v else float(v)
                except ValueError:
                    result[k] = v
            else:
                result[k] = v
                
        return result
    
    elif format_type == "json_string":
        raw_val: Optional[str] = sync_client.get(key)  # type: ignore[assignment]
        if not raw_val:
            return None
        return json.loads(raw_val)
    
    else:
        raise ValueError(f"Unsupported format_type: {format_type}")


# Distributed Lock Management
class DistributedLock:
    """
    Redis-based distributed lock with TTL and automatic renewal.
    
    This class provides a standardized way to implement distributed locks
    across the Motet system, ensuring consistent behavior and error handling.
    """
    
    def __init__(self, client_id: str, lock_key: str, ttl_seconds: int = 90):
        self.client_id = client_id
        self.lock_key = lock_key
        self.ttl_seconds = ttl_seconds
        self.lock_value = None
        self._acquired = False
    
    async def acquire(self, lock_value: Optional[str] = None) -> bool:
        """
        Acquire the distributed lock.
        
        Args:
            lock_value: Value to store in the lock (defaults to current process PID)
            
        Returns:
            True if lock was acquired, False otherwise
        """
        import os
        
        if lock_value is None:
            lock_value = str(os.getpid())
        
        self.lock_value = lock_value
        
        try:
            client = get_redis_client(self.client_id)
            
            # Try to acquire lock with TTL
            acquired = await client.set(
                self.lock_key,
                lock_value,
                nx=True,  # Only set if not exists
                ex=self.ttl_seconds  # TTL in seconds
            )
            
            self._acquired = bool(acquired)
            return self._acquired
            
        except Exception as e:
            import structlog
            logger = structlog.get_logger(__name__)
            logger.error("Failed to acquire distributed lock",
                        client_id=self.client_id,
                        lock_key=self.lock_key,
                        lock_value=lock_value,
                        error=str(e))
            return False
    
    def acquire_sync(self, lock_value: Optional[str] = None) -> bool:
        """
        Synchronous version of acquire for Celery tasks.
        """
        import os
        
        if lock_value is None:
            lock_value = str(os.getpid())
        
        self.lock_value = lock_value
        
        try:
            sync_client = get_sync_redis_client(self.client_id)
            
            acquired = sync_client.set(
                self.lock_key,
                lock_value,
                nx=True,
                ex=self.ttl_seconds
            )
            
            self._acquired = bool(acquired)
            return self._acquired
            
        except Exception as e:
            import structlog
            logger = structlog.get_logger(__name__)
            logger.error("Failed to acquire distributed lock (sync)",
                        client_id=self.client_id,
                        lock_key=self.lock_key,
                        lock_value=lock_value,
                        error=str(e))
            return False
    
    async def renew(self) -> bool:
        """
        Renew the lock TTL if we still hold it.
        
        Returns:
            True if lock was renewed, False otherwise
        """
        if not self._acquired or not self.lock_value:
            return False
        
        try:
            client = get_redis_client(self.client_id)
            
            # Check if we still hold the lock
            current_holder = await client.get(self.lock_key)
            
            if current_holder and current_holder == self.lock_value:
                # We still hold the lock, renew it
                await client.expire(self.lock_key, self.ttl_seconds)
                return True
            else:
                # We lost the lock
                self._acquired = False
                return False
                
        except Exception as e:
            import structlog
            logger = structlog.get_logger(__name__)
            logger.error("Failed to renew distributed lock",
                        client_id=self.client_id,
                        lock_key=self.lock_key,
                        lock_value=self.lock_value,
                        error=str(e))
            return False
    
    def renew_sync(self) -> bool:
        """
        Synchronous version of renew for Celery tasks.
        """
        if not self._acquired or not self.lock_value:
            return False
        
        try:
            sync_client = get_sync_redis_client(self.client_id)
            
            current_holder = sync_client.get(self.lock_key)
            
            if current_holder and current_holder == self.lock_value:
                sync_client.expire(self.lock_key, self.ttl_seconds)
                return True
            else:
                self._acquired = False
                return False
                
        except Exception as e:
            import structlog
            logger = structlog.get_logger(__name__)
            logger.error("Failed to renew distributed lock (sync)",
                        client_id=self.client_id,
                        lock_key=self.lock_key,
                        lock_value=self.lock_value,
                        error=str(e))
            return False
    
    async def release(self) -> bool:
        """
        Release the lock if we hold it.
        
        Returns:
            True if lock was released, False otherwise
        """
        if not self._acquired or not self.lock_value:
            return False
        
        try:
            client = get_redis_client(self.client_id)
            
            # Use Lua script for atomic check-and-delete
            lua_script = """
            if redis.call("GET", KEYS[1]) == ARGV[1] then
                return redis.call("DEL", KEYS[1])
            else
                return 0
            end
            """
            
            result = await client.eval(lua_script, 1, self.lock_key, self.lock_value)  # type: ignore[misc]
            
            if result:
                self._acquired = False
                return True
            else:
                return False
                
        except Exception as e:
            import structlog
            logger = structlog.get_logger(__name__)
            logger.error("Failed to release distributed lock",
                        client_id=self.client_id,
                        lock_key=self.lock_key,
                        lock_value=self.lock_value,
                        error=str(e))
            return False
    
    def release_sync(self) -> bool:
        """
        Synchronous version of release for Celery tasks.
        """
        if not self._acquired or not self.lock_value:
            return False
        
        try:
            sync_client = get_sync_redis_client(self.client_id)
            
            lua_script = """
            if redis.call("GET", KEYS[1]) == ARGV[1] then
                return redis.call("DEL", KEYS[1])
            else
                return 0
            end
            """
            
            result = sync_client.eval(lua_script, 1, self.lock_key, self.lock_value)
            
            if result:
                self._acquired = False
                return True
            else:
                return False
                
        except Exception as e:
            import structlog
            logger = structlog.get_logger(__name__)
            logger.error("Failed to release distributed lock (sync)",
                        client_id=self.client_id,
                        lock_key=self.lock_key,
                        lock_value=self.lock_value,
                        error=str(e))
            return False
    
    @property
    def is_acquired(self) -> bool:
        """Check if the lock is currently acquired by this instance."""
        return self._acquired


# Convenience functions for distributed locks
def create_distributed_lock(client_id: str, lock_key: str, ttl_seconds: int = 90) -> DistributedLock:
    """
    Create a distributed lock instance.
    
    Args:
        client_id: Redis client identifier
        lock_key: Key for the lock in Redis
        ttl_seconds: Time-to-live for the lock in seconds
        
    Returns:
        DistributedLock instance
    """
    return DistributedLock(client_id, lock_key, ttl_seconds)


async def acquire_distributed_lock(client_id: str, lock_key: str, 
                                 lock_value: Optional[str] = None, ttl_seconds: int = 90) -> Optional[DistributedLock]:
    """
    Convenience function to create and acquire a distributed lock.
    
    Args:
        client_id: Redis client identifier
        lock_key: Key for the lock in Redis
        lock_value: Value to store in the lock (defaults to current PID)
        ttl_seconds: Time-to-live for the lock in seconds
        
    Returns:
        DistributedLock instance if acquired, None if failed
    """
    lock = create_distributed_lock(client_id, lock_key, ttl_seconds)
    
    if await lock.acquire(lock_value):
        return lock
    else:
        return None


def acquire_distributed_lock_sync(client_id: str, lock_key: str, 
                                lock_value: Optional[str] = None, ttl_seconds: int = 90) -> Optional[DistributedLock]:
    """
    Synchronous convenience function to create and acquire a distributed lock.
    """
    lock = create_distributed_lock(client_id, lock_key, ttl_seconds)
    
    if lock.acquire_sync(lock_value):
        return lock
    else:
        return None


# Export the main functions
__all__ = [
    'UnifiedRedisManager',
    'RedisConfig', 
    'get_redis_manager',
    'get_redis_client',
    'get_pubsub_redis_client',
    'get_sync_redis_client',
    'create_redis_client_for_service',
    'redis_health_check',
    'close_redis_client',
    'close_all_redis_connections',
    'get_redis_stats',
    # Standardized data access patterns
    'store_structured_data',
    'retrieve_structured_data', 
    'store_structured_data_sync',
    'retrieve_structured_data_sync',
    # Distributed lock management
    'DistributedLock',
    'create_distributed_lock',
    'acquire_distributed_lock',
    'acquire_distributed_lock_sync'
]
