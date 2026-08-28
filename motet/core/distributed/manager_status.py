"""
Motet - Manager Status Registry

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Redis-based status registry for instance managers (MCP and Local Inference).
    Provides a consistent pattern for publishing and querying manager health,
    metrics, and lifecycle state. Fleet listing uses the ``manager:registered``
    membership set plus per-key hashes (not a keyspace scan).

Dependencies:
    - pydantic: Data validation and model definitions
    - enum: Manager type enumeration
    - time: Timestamp management
    - json: Data serialization
    - Redis manager for distributed state storage
    - structlog: Structured logs for hash parse and membership prune

Usage:
    from motet.core.distributed.manager_status import ManagerStatusRegistry, ManagerType

    registry = ManagerStatusRegistry()
    registry.publish_status(
        worker_id="cloud_worker1",
        manager_type=ManagerType.MCP,
        status="running",
        metadata={"instances": 5},
    )
    statuses = registry.get_all_statuses()

Notes:
    - Uses Redis hash storage for efficient updates and queries
    - Supports both MCP Instance Manager and Local Inference Manager
    - Status hashes expire after 30s; ``is_stale`` flags rows the API should mark stale
    - Lists managers with ``SMEMBERS manager:registered`` then a pipelined ``HGETALL``
    - Status hashes expire in 30s; empty members are skipped and dropped from the set
    - Thread-safe and works across all Celery pool types
    - Integrates seamlessly with ops dashboard
"""

import time
import json
from enum import Enum
from typing import Any, Dict, List, Optional, cast
from pydantic import BaseModel, Field

import redis
import structlog

from .redis_manager import get_sync_redis_client

logger = structlog.get_logger(__name__)


class ManagerType(Enum):
    """Manager types"""
    MCP = "mcp"
    LOCAL_INFERENCE = "local_inference"


class ManagerStatus(BaseModel):
    """Manager status information stored in Redis.

    Identity decoupling:
    - ``manager_id`` is the canonical identity of the manager process. It is
      injected via ``MOTET_MCP_MANAGER_ID`` once the sibling deployment lands.
      Until then it is synthesized from ``worker_id``.
    - ``served_workers`` enumerates the workers the manager currently serves.
    - ``worker_id`` is the bootstrap-attribution tag (which worker brought the
      manager up). Treat ``manager_id`` as authoritative.
    """

    worker_id: str
    manager_type: ManagerType
    status: str  # "starting", "running", "stopping", "stopped", "error"
    last_update: float
    pid: Optional[int] = None

    # ADR-0105 §R3: canonical manager identity (additive in M1.5; populated
    # by env injection once the sibling deployment lands in M1+M4).
    manager_id: str = ""
    served_workers: List[str] = Field(default_factory=list)

    # Health metrics
    instances_total: int = 0
    instances_healthy: int = 0
    instances_unhealthy: int = 0

    # Stats
    total_requests: int = 0
    active_requests: int = 0
    errors: int = 0
    start_time: Optional[float] = None
    uptime_seconds: float = 0.0

    # Resource usage
    memory_mb: float = 0.0
    cpu_percent: float = 0.0

    # Additional metadata
    metadata: Dict[str, Any] = Field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for Redis storage"""
        data = self.model_dump(mode='json')
        # Convert enum to string for Redis
        data['manager_type'] = self.manager_type.value
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ManagerStatus':
        """Create from dictionary loaded from Redis"""
        # Convert string back to enum
        if 'manager_type' in data and isinstance(data['manager_type'], str):
            data['manager_type'] = ManagerType(data['manager_type'])
        return cls(**data)


class ManagerStatusRegistry:
    """
    Redis-based registry for manager status tracking.
    
    Provides a centralized way to publish and query manager health and metrics
    without requiring HTTP endpoints or port management.
    """
    
    REDIS_KEY_PREFIX = "manager:status"
    REGISTERED_MANAGERS_SET = "manager:registered"
    STATUS_TTL = 30  # seconds - if not updated, considered stale
    
    def __init__(self):
        """Initialize the registry with Redis connection"""
        self.redis = cast(redis.Redis, get_sync_redis_client("manager_status_registry"))
    
    def _get_redis_key(self, manager_id: str, manager_type: ManagerType) -> str:
        """Generate Redis key for a manager.

        ADR-0105 §R3: the disambiguator is ``manager_id`` (canonical manager
        identity), NOT ``worker_id``. Two ``mcp-manager`` deployments on the
        same Redis bus (e.g. dev cloud stack + edge device) would otherwise
        collide on ``manager:status:mcp-manager:mcp`` and overwrite each
        other's heartbeat — visible in the UI as rows flickering on/off.
        """
        return f"{self.REDIS_KEY_PREFIX}:{manager_id}:{manager_type.value}"
    
    def publish_status(
        self,
        worker_id: str,
        manager_type: ManagerType,
        status: str,
        pid: Optional[int] = None,
        instances_total: int = 0,
        instances_healthy: int = 0,
        instances_unhealthy: int = 0,
        total_requests: int = 0,
        active_requests: int = 0,
        errors: int = 0,
        start_time: Optional[float] = None,
        memory_mb: float = 0.0,
        cpu_percent: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
        manager_id: Optional[str] = None,
        served_workers: Optional[List[str]] = None,
    ) -> None:
        """
        Publish manager status to Redis.

        Args:
            worker_id: Worker identifier (bootstrap-attribution; see ADR-0105 §R3).
            manager_type: Type of manager (MCP or Local Inference)
            status: Current status (starting, running, stopping, stopped, error)
            pid: Process ID
            instances_total: Total number of managed instances
            instances_healthy: Number of healthy instances
            instances_unhealthy: Number of unhealthy instances
            total_requests: Total requests processed
            active_requests: Currently active requests
            errors: Total error count
            start_time: Manager start timestamp
            memory_mb: Memory usage in MB
            cpu_percent: CPU usage percentage
            metadata: Additional metadata dictionary
            manager_id: Canonical manager identity per ADR-0105 §R3. When the
                sibling deployment lands (ADR-0105 M1+M4), this is read from
                ``MOTET_MCP_MANAGER_ID`` by the manager process itself. Until
                then, it is synthesized from ``worker_id`` so the new field is
                populated for FE/dashboard consumers without coupling the
                rollout to the still-pending in-worker deletion.
            served_workers: Workers this manager currently serves. Defaults to
                ``[worker_id]`` (today's de-facto 1:1 cardinality); shape A / B
                / C deployments will set this to the actual served list.
        """
        # Calculate uptime
        uptime_seconds = 0.0
        if start_time:
            uptime_seconds = time.time() - start_time

        resolved_manager_id = manager_id or f"{manager_type.value}-{worker_id}"
        resolved_served_workers = served_workers if served_workers is not None else [worker_id]

        manager_status = ManagerStatus(
            worker_id=worker_id,
            manager_type=manager_type,
            status=status,
            last_update=time.time(),
            pid=pid,
            manager_id=resolved_manager_id,
            served_workers=resolved_served_workers,
            instances_total=instances_total,
            instances_healthy=instances_healthy,
            instances_unhealthy=instances_unhealthy,
            total_requests=total_requests,
            active_requests=active_requests,
            errors=errors,
            start_time=start_time,
            uptime_seconds=uptime_seconds,
            memory_mb=memory_mb,
            cpu_percent=cpu_percent,
            metadata=metadata or {}
        )
        
        # Store in Redis with TTL — keyed by manager_id (ADR-0105 §R3) so
        # multiple managers with the same observability worker_id tag
        # (e.g. two ``mcp-manager`` services on different deployments
        # sharing one Valkey) do not stomp on each other.
        key = self._get_redis_key(resolved_manager_id, manager_type)
        self.redis.hset(key, mapping={
            k: json.dumps(v) if isinstance(v, (dict, list)) else str(v)
            for k, v in manager_status.to_dict().items()
        })
        self.redis.expire(key, self.STATUS_TTL)
        self.redis.sadd(self.REGISTERED_MANAGERS_SET, key)
    
    def get_status(
        self,
        manager_id: str,
        manager_type: ManagerType
    ) -> Optional[ManagerStatus]:
        """
        Get status for a specific manager.

        Args:
            manager_id: Canonical manager identity (ADR-0105 §R3). For
                MCP managers this is ``MOTET_MCP_MANAGER_ID``; for the
                in-worker LocalInferenceManager it is the synthesized
                ``local_inference-{worker_id}`` value.
            manager_type: Type of manager

        Returns:
            ManagerStatus object or None if not found
        """
        key = self._get_redis_key(manager_id, manager_type)
        data = cast(Dict[Any, Any], cast(Any, self.redis).hgetall(key))
        return self._parse_status_hash(data)

    def get_all_statuses(self) -> List[ManagerStatus]:
        """Get all manager statuses.

        Lists members of ``manager:registered`` (full ``manager:status:…``
        keys) then ``HGETALL`` each in one pipeline. Empty hashes are
        skipped and dropped from the membership set. Does not scan the
        keyspace.
        """
        keys = [
            self._decode_member(member)
            for member in (cast(Any, self.redis).smembers(self.REGISTERED_MANAGERS_SET) or [])
        ]
        hashes = self._hgetall_many(keys)

        statuses: List[ManagerStatus] = []
        stale_keys: List[str] = []
        for key, data in zip(keys, hashes):
            if not data:
                stale_keys.append(key)
                continue
            try:
                status = self._parse_status_hash(data)
            except Exception as e:
                logger.warning(
                    "manager_status_load_failed",
                    redis_key=key,
                    error=str(e),
                    exc_info=True,
                )
                continue
            if status is not None:
                statuses.append(status)

        if stale_keys:
            self._forget_membership(*stale_keys)

        return statuses
    
    def is_stale(self, status: ManagerStatus) -> bool:
        """
        Check if a manager status is stale (hasn't updated recently).
        
        Args:
            status: ManagerStatus object
            
        Returns:
            True if stale, False otherwise
        """
        age = time.time() - status.last_update
        return age > self.STATUS_TTL

    def _decode_member(self, member: Any) -> str:
        """Normalize a Redis set member to a status-hash key string."""
        if isinstance(member, bytes):
            return member.decode("utf-8")
        return str(member)

    def _hgetall_many(self, keys: List[str]) -> List[Any]:
        """Fetch many hashes in one non-transactional pipeline."""
        if not keys:
            return []
        pipe = cast(Any, self.redis).pipeline(transaction=False)
        for key in keys:
            pipe.hgetall(key)
        return list(pipe.execute())

    def _forget_membership(self, *keys: str) -> None:
        """Drop status-hash keys from the registered membership set."""
        if not keys:
            return
        self.redis.srem(self.REGISTERED_MANAGERS_SET, *keys)

    def _parse_status_hash(self, data: Dict[Any, Any]) -> Optional[ManagerStatus]:
        """Build a ManagerStatus from a Redis hash, or None if empty."""
        if not data:
            return None

        parsed_data: Dict[str, Any] = {}
        for k_str, v_str in data.items():
            if k_str in ["metadata"]:
                try:
                    parsed_data[k_str] = json.loads(v_str)
                except (json.JSONDecodeError, ValueError, TypeError):
                    parsed_data[k_str] = v_str
            elif k_str == "served_workers":
                try:
                    parsed_data[k_str] = json.loads(v_str)
                except (json.JSONDecodeError, ValueError, TypeError):
                    parsed_data[k_str] = []
            elif k_str in [
                "last_update",
                "start_time",
                "uptime_seconds",
                "memory_mb",
                "cpu_percent",
            ]:
                try:
                    parsed_data[k_str] = float(v_str) if v_str != "None" else None
                except (ValueError, TypeError):
                    parsed_data[k_str] = 0.0
            elif k_str in [
                "pid",
                "instances_total",
                "instances_healthy",
                "instances_unhealthy",
                "total_requests",
                "active_requests",
                "errors",
            ]:
                try:
                    parsed_data[k_str] = int(v_str) if v_str != "None" else None
                except (ValueError, TypeError):
                    parsed_data[k_str] = 0
            else:
                parsed_data[k_str] = v_str

        return ManagerStatus.from_dict(parsed_data)

