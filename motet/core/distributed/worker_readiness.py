"""
Motet - Worker Readiness Service

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Comprehensive worker readiness service for the Motet distributed framework.
    Manages worker lifecycle states, health monitoring, capability tracking, and
    readiness queries for intelligent command routing. Includes worker registration,
    heartbeat management, and automatic cleanup of stale workers. Fleet listing
    uses the ``worker:registered`` membership set plus per-id hashes (not a
    keyspace scan). Each registration stamps the worker process Motet product
    version so operators can detect mixed-version fleets.

Dependencies:
    - pydantic: Data validation and model definitions
    - enum: Worker state enumeration
    - time: Timestamp and duration management
    - json: Data serialization
    - Redis manager for distributed state storage

Usage:
    from motet.core.distributed.worker_readiness import WorkerReadinessService, WorkerState
    
    service = WorkerReadinessService()
    service.register_worker("worker_id", capabilities=["reasoning"])
    ready_workers = service.get_ready_workers()

Notes:
    - Supports multiple worker states (starting, warming, ready, accepting, busy, unhealthy)
    - Includes capability-based worker filtering and routing
    - Provides health monitoring with memory and CPU usage tracking
    - Supports worker concurrency models (eventlet, gevent, threads, fork)
    - Includes automatic cleanup of stale and unhealthy workers
    - Integrates with Redis for distributed state management
    - Lists the fleet with ``SMEMBERS worker:registered`` then a pipelined ``HGETALL`` per id
    - ``worker:ready`` remains the ready-only set; do not use it as the full roster
    - Active-command counters update only when the registration hash already exists
    - Supports tool discovery and MCP tool integration
"""


import os
import time
import json
from enum import Enum
from typing import Dict, List, Optional, Any
from pydantic import BaseModel

import structlog

from ..._version import get_version
from .redis_manager import get_sync_redis_client
from ..workers.concurrency_primitives import worker_sleep

logger = structlog.get_logger(__name__)
DEBUG_MODE = os.getenv("MOTET_DEBUG_MODE", "false").lower() == "true"


class WorkerState(Enum):
    """Worker lifecycle states"""
    STARTING = "starting"
    WARMING = "warming"
    READY = "ready"
    ACCEPTING = "accepting"
    BUSY = "busy"
    UNHEALTHY = "unhealthy"
    # Lifecycle-managed states (worker stays in registry for UI visibility)
    STOPPED = "stopped"
    TERMINATING = "terminating"
    RESTARTING = "restarting"


class WorkerInfo(BaseModel):
    """Worker information stored in registry"""
    worker_id: str
    state: WorkerState
    capabilities: List[str]
    last_heartbeat: float
    warmup_completed: bool
    active_commands: int = 0
    max_concurrency: int = 6
    tool_count: int = 0
    mcp_tool_count: int = 0
    tools: List[Dict[str, Any]] = []  # List of available tools with details
    instance_managers: Dict[str, Any] = {}  # Manager telemetry keyed by manager type
    startup_time: float = 0
    warmup_duration_ms: int = 0
    
    # Worker concurrency model (ADR-0033)
    pool_type: Optional[str] = None  # "eventlet", "gevent", "threads", or "fork"

    # Local worker identity (ADR-0095) — populated from env vars on local workers
    owner_principal_id: Optional[str] = None
    owner_tenant_id: Optional[str] = None
    command_scope: Optional[str] = None  # "principal" or "tenant"; None for cloud workers

    # ADR-0105: Sibling MCPInstanceManager binding. Populated by the worker's
    # MCP startup once it has resolved which manager it routes to (via
    # MOTET_MCP_MANAGER_ID). The /managers/status endpoint inverts this map to
    # compute served_workers per manager — the manager itself does not know
    # which workers send to its (anonymous) Redis Streams.
    mcp_manager_id: Optional[str] = None

    # ADR-0105 (LocalInferenceManager hoist): the sibling LocalInferenceManager this
    # worker routes to, via MOTET_LOCAL_INFERENCE_MANAGER_ID. Populated when the worker
    # initializes its LocalInferenceClient. Inverted by /managers/status to compute
    # served_workers for local_inference managers, exactly like mcp_manager_id above.
    local_inference_manager_id: Optional[str] = None

    # Motet product version of the process that last registered this worker.
    # Missing on entries written before this field existed.
    motet_version: Optional[str] = None
    
    # Health metrics
    memory_usage_mb: float = 0.0
    cpu_usage_percent: float = 0.0
    uptime_seconds: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for Redis storage using Pydantic's serialization"""
        # Use Pydantic's model_dump with mode='json' for proper serialization
        data = self.model_dump(mode='json')
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'WorkerInfo':
        """Create from dictionary loaded from Redis using Pydantic's validation"""
        # Handle legacy data where tools might be stored as JSON string
        if 'tools' in data and isinstance(data['tools'], str):
            try:
                data['tools'] = json.loads(data['tools'])
            except (json.JSONDecodeError, TypeError):
                data['tools'] = []
        if 'instance_managers' in data and isinstance(data['instance_managers'], str):
            try:
                data['instance_managers'] = json.loads(data['instance_managers'])
            except (json.JSONDecodeError, TypeError):
                data['instance_managers'] = {}
        
        # Use Pydantic's model_validate for proper deserialization
        # This handles all type conversions automatically
        return cls.model_validate(data)


class WorkerReadinessService:
    """
    Service for managing worker readiness state and routing decisions.
    
    This service provides:
    - Worker registration and state management
    - Readiness tracking and queries
    - Health monitoring and cleanup
    - Load balancing support
    """
    
    def __init__(self):
        # Use unified Redis manager (synchronous) with consistent client ID
        # Typed as Any: redis-py's ResponseT generic causes basedpyright to infer
        # Awaitable return types for sync methods (smembers, hgetall, etc.)
        # when the client type param is unresolved. Any suppresses those false positives.
        self.redis_client: Any = get_sync_redis_client("worker_readiness")
        self._validation_lock_key = "lock:worker:readiness"
        
        # Redis keys (shared control plane; unprefixed)
        self.WORKERS_KEY_PREFIX = "worker:registration:"
        self.REGISTERED_WORKERS_SET = "worker:registered"
        self.READY_WORKERS_SET = "worker:ready"
        self.WORKER_HEARTBEAT_TTL = 120  # 2 minutes
    
    def shutdown(self):
        """Shutdown the service and cleanup resources"""
        if self.redis_client:
            self.redis_client.close()
    
    def register_worker(self, 
                            worker_id: str, 
                            capabilities: List[str],
                            max_concurrency: int = 6,
                            pool_type: Optional[str] = None) -> None:
        """Register a new worker in STARTING state with stale entry cleanup (ADR-0033: added pool_type)

        Preserves out-of-band binding fields (e.g. ``mcp_manager_id`` written
        by the MCP watcher per ADR-0105 §R3) when an entry already exists, so
        that a later re-registration in the same process boot doesn't clobber
        them. This matters because parent-coordination re-registers the
        worker AFTER ``ensure_mcp_watcher_started`` has already published the
        manager binding.
        """
        self._cleanup_stale_worker_entries()

        owner_principal_id: Optional[str] = None
        owner_tenant_id: Optional[str] = None
        command_scope: Optional[str] = None
        if worker_id.startswith("edge_"):
            owner_principal_id = os.getenv("MOTET_EDGE_PRINCIPAL_ID") or None
            owner_tenant_id = os.getenv("MOTET_EDGE_TENANT_ID") or None
            command_scope = os.getenv("MOTET_EDGE_COMMAND_SCOPE", "principal") or None

        existing = self.get_worker_info(worker_id)
        preserved_mcp_manager_id: Optional[str] = None
        if existing and existing.mcp_manager_id and existing.mcp_manager_id != "None":
            preserved_mcp_manager_id = existing.mcp_manager_id
        preserved_local_inference_manager_id: Optional[str] = None
        if (
            existing
            and existing.local_inference_manager_id
            and existing.local_inference_manager_id != "None"
        ):
            preserved_local_inference_manager_id = existing.local_inference_manager_id

        worker_info = WorkerInfo(
            worker_id=worker_id,
            state=WorkerState.STARTING,
            capabilities=capabilities,
            last_heartbeat=time.time(),
            warmup_completed=False,
            max_concurrency=max_concurrency,
            pool_type=pool_type,  # ADR-0033: Track worker concurrency model
            startup_time=time.time(),
            owner_principal_id=owner_principal_id,
            owner_tenant_id=owner_tenant_id,
            command_scope=command_scope,
            mcp_manager_id=preserved_mcp_manager_id,
            local_inference_manager_id=preserved_local_inference_manager_id,
            motet_version=get_version(),
        )
        
        self._store_worker_info(worker_info)
        pool_info = f" (pool: {pool_type})" if pool_type else ""
        logger.info(
            "worker_registered",
            worker_id=worker_id,
            state="starting",
            pool_type=pool_type,
            max_concurrency=max_concurrency,
        )
    
    def update_worker_state(self, 
                                worker_id: str, 
                                state: WorkerState,
                                **kwargs) -> None:
        """Update worker state and optional additional fields
        
        NOTE: Uses synchronous Redis client to ensure immediate consistency.
        This method is called from both sync (parent process) and async contexts.
        """
        from .redis_manager import get_sync_redis_client
        
        worker_info = self.get_worker_info(worker_id)
        if not worker_info:
            logger.warning("worker_state_update_unknown_worker", worker_id=worker_id)
            return
        
        # Update state and any additional fields
        worker_info.state = state
        worker_info.last_heartbeat = time.time()
        
        for key, value in kwargs.items():
            if hasattr(worker_info, key):
                setattr(worker_info, key, value)
        
        self._store_worker_info(worker_info)
        
        # Update ready workers set using SYNCHRONOUS Redis client
        # This ensures the set is updated immediately in all contexts
        sync_redis = get_sync_redis_client("worker_readiness")
        if state == WorkerState.READY:
            sync_redis.sadd(self.READY_WORKERS_SET, worker_id)
            logger.info("worker_marked_ready", worker_id=worker_id)
        else:
            sync_redis.srem(self.READY_WORKERS_SET, worker_id)
            if state != WorkerState.ACCEPTING:  # ACCEPTING is still ready, just busy
                logger.debug("worker_state_changed", worker_id=worker_id, state=state.value)
    
    def update_worker_tools(self, 
                           worker_id: str,
                           tools: List[Dict[str, Any]],
                           tool_count: Optional[int] = None,
                           mcp_tool_count: Optional[int] = None) -> None:
        """Update worker tool information with detailed tool list"""
        worker_info = self.get_worker_info(worker_id)
        if not worker_info:
            logger.warning("worker_tools_update_unknown_worker", worker_id=worker_id)
            return
        
        # Update tool information
        worker_info.tools = tools
        if tool_count is not None:
            worker_info.tool_count = tool_count
        if mcp_tool_count is not None:
            worker_info.mcp_tool_count = mcp_tool_count
        
        # Update last heartbeat to indicate worker is active
        worker_info.last_heartbeat = time.time()
        
        self._store_worker_info(worker_info)
        logger.debug("worker_tools_updated", worker_id=worker_id, tools_count=len(tools))
    
    def mark_worker_ready(self, 
                              worker_id: str,
                              tool_count: int = 0,
                              mcp_tool_count: int = 0,
                              warmup_duration_ms: int = 0) -> None:
        """Mark worker as ready after successful warmup"""
        self.update_worker_state(
            worker_id, 
            WorkerState.READY,
            warmup_completed=True,
            tool_count=tool_count,
            mcp_tool_count=mcp_tool_count,
            warmup_duration_ms=warmup_duration_ms
        )
    
    def update_worker_mcp_manager_binding(
        self,
        worker_id: str,
        mcp_manager_id: Optional[str],
    ) -> bool:
        """
        Record which MCPInstanceManager this worker routes to (ADR-0105 §R3).

        Called by ``ensure_mcp_watcher_started`` once ``MOTET_MCP_MANAGER_ID``
        has been resolved. The /managers/status API inverts this binding to
        compute each manager's ``served_workers`` set — the sibling manager
        itself cannot derive this (workers post anonymously to Redis Streams).
        """
        worker_info = self.get_worker_info(worker_id)
        if not worker_info:
            logger.warning(
                "worker_mcp_manager_binding_unknown_worker",
                worker_id=worker_id,
                mcp_manager_id=mcp_manager_id,
            )
            return False

        if worker_info.mcp_manager_id == mcp_manager_id:
            return True  # No-op; avoid spurious Redis writes on every reconnect.

        worker_info.mcp_manager_id = mcp_manager_id
        worker_info.last_heartbeat = time.time()
        self._store_worker_info(worker_info)
        logger.info(
            "worker_mcp_manager_binding_updated",
            worker_id=worker_id,
            mcp_manager_id=mcp_manager_id,
        )
        return True

    def update_worker_local_inference_manager_binding(
        self,
        worker_id: str,
        local_inference_manager_id: Optional[str],
    ) -> bool:
        """
        Record which sibling LocalInferenceManager this worker routes to (ADR-0105).

        Called when the worker initializes its ``LocalInferenceClient`` and has
        resolved ``MOTET_LOCAL_INFERENCE_MANAGER_ID``. The /managers/status API
        inverts this binding to compute each local_inference manager's
        ``served_workers`` set — the sibling manager itself cannot derive this
        (workers post anonymously to its Redis Streams). Mirrors
        ``update_worker_mcp_manager_binding``.
        """
        worker_info = self.get_worker_info(worker_id)
        if not worker_info:
            logger.warning(
                "worker_local_inference_manager_binding_unknown_worker",
                worker_id=worker_id,
                local_inference_manager_id=local_inference_manager_id,
            )
            return False

        if worker_info.local_inference_manager_id == local_inference_manager_id:
            return True  # No-op; avoid spurious Redis writes on every reconnect.

        worker_info.local_inference_manager_id = local_inference_manager_id
        worker_info.last_heartbeat = time.time()
        self._store_worker_info(worker_info)
        logger.info(
            "worker_local_inference_manager_binding_updated",
            worker_id=worker_id,
            local_inference_manager_id=local_inference_manager_id,
        )
        return True

    def update_worker_capacity(self, 
                                worker_id: str,
                                max_concurrency: int) -> None:
        """
        Update worker's max_concurrency based on actual discovered processes.
        
        This method bridges the gap between ProcessHealthMonitor's dynamic process
        discovery and WorkerReadinessService's capacity tracking for routing decisions.
        
        Args:
            worker_id: Worker ID to update
            max_concurrency: Actual number of worker processes discovered
        """
        worker_info = self.get_worker_info(worker_id)
        if not worker_info:
            logger.warning("worker_capacity_update_unknown_worker", worker_id=worker_id)
            return
        
        # Only update if capacity has changed
        old_capacity = worker_info.max_concurrency
        if old_capacity != max_concurrency:
            worker_info.max_concurrency = max_concurrency
            worker_info.last_heartbeat = time.time()
            
            self._store_worker_info(worker_info)
            
            logger.info(
                "worker_capacity_updated",
                worker_id=worker_id,
                old_capacity=old_capacity,
                max_concurrency=max_concurrency,
            )
        # If capacity hasn't changed, no need to update or log
    
    def worker_heartbeat(self, 
                             worker_id: str,
                             active_commands: int = 0,
                             current_state: Optional[WorkerState] = None,
                             re_register_on_missing: bool = False) -> bool:
        """
        Update worker heartbeat and current status (REAL-TIME updates using UnifiedRedisManager)
        
        Enhanced with:
        - Connection health validation
        - Automatic re-registration if worker key is missing
        - Extended TTL for sleep scenarios
        - Better error handling
        
        Args:
            worker_id: Worker identifier
            active_commands: Current number of active commands
            current_state: Optional explicit state override
            re_register_on_missing: If True, attempt to re-register worker if key is missing
            
        Returns:
            True if heartbeat succeeded, False otherwise
        """
        from .redis_manager import (
            retrieve_structured_data_sync, 
            store_structured_data_sync, 
            get_sync_redis_client,
            get_redis_manager
        )
        
        try:
            # Check Redis connection health first
            redis_manager = get_redis_manager()
            if not redis_manager.health_check_sync("worker_readiness"):
                logger.warning("worker_heartbeat_redis_unhealthy", worker_id=worker_id)
                return False
            
            # Typed as Any for the same reason as self.redis_client above.
            sync_redis: Any = get_sync_redis_client("worker_readiness")
            
            # Get existing worker info using standardized Redis manager
            worker_key = f"{self.WORKERS_KEY_PREFIX}{worker_id}"
            worker_data = retrieve_structured_data_sync("worker_readiness", worker_key, format_type="hash")
            
            if worker_data:
                # Update heartbeat and status
                worker_data['last_heartbeat'] = str(time.time())
                worker_data['active_commands'] = str(active_commands)
                
                # Update state if provided
                if current_state:
                    worker_data['state'] = current_state.value
                else:
                    # Auto-determine state based on load
                    current_state_value = worker_data.get('state', WorkerState.READY.value)
                    max_concurrency = int(worker_data.get('max_concurrency', 6))
                    
                    if current_state_value in [WorkerState.READY.value, WorkerState.ACCEPTING.value, WorkerState.BUSY.value]:
                        if active_commands >= max_concurrency:
                            worker_data['state'] = WorkerState.BUSY.value
                        elif active_commands > 0:
                            worker_data['state'] = WorkerState.ACCEPTING.value
                        else:
                            worker_data['state'] = WorkerState.READY.value
                
                # Store updated worker info using standardized Redis manager (REAL-TIME)
                store_structured_data_sync("worker_readiness", worker_key, worker_data, format_type="hash")
                
                # Set extended TTL (5x heartbeat interval for sleep resilience) - using synchronous Redis client
                # This gives us 10 minutes of TTL, enough to handle most sleep scenarios
                extended_ttl = self.WORKER_HEARTBEAT_TTL * 5  # 120 * 5 = 600 seconds (10 minutes)
                sync_redis.expire(worker_key, extended_ttl)
                self._mark_registered(sync_redis, worker_id)
                
                # Real-time logging for immediate updates
                if active_commands > 0:
                    if DEBUG_MODE:
                        logger.debug(
                            "worker_heartbeat_state_updated",
                            worker_id=worker_id,
                            state=worker_data.get("state"),
                            active_commands=active_commands,
                        )
                
                # Get the current state for ready workers set management
                state = WorkerState(worker_data['state'])
            else:
                # Worker not registered - handle based on re_register_on_missing flag
                if re_register_on_missing:
                    logger.warning(
                        "worker_heartbeat_unregistered_reregistration_requested",
                        worker_id=worker_id,
                    )
                    # Re-registration should be handled by the caller (parent_coordinator)
                    return False
                else:
                    logger.warning("worker_heartbeat_unregistered_skipping", worker_id=worker_id)
                    return False
            
            # Update ready workers set based on state with TTL - using synchronous Redis client
            if state in [WorkerState.READY, WorkerState.ACCEPTING]:
                sync_redis.sadd(self.READY_WORKERS_SET, worker_id)
                # Extended TTL on ready workers set
                sync_redis.expire(self.READY_WORKERS_SET, self.WORKER_HEARTBEAT_TTL * 4)
            else:
                sync_redis.srem(self.READY_WORKERS_SET, worker_id)
            
            return True
                
        except Exception as e:
            logger.error(
                "worker_heartbeat_failed",
                worker_id=worker_id,
                error=str(e),
                exc_info=True,
            )
            return False
    
    def get_ready_workers(self, 
                              required_capabilities: Optional[List[str]] = None) -> List[str]:
        """Get list of ready workers, optionally filtered by capabilities"""
        ready_worker_ids = self._decode_worker_ids(
            self.redis_client.smembers(self.READY_WORKERS_SET)
        )
        
        if not required_capabilities:
            return ready_worker_ids
        
        capable_workers = []
        for worker_id in ready_worker_ids:
            worker_info = self.get_worker_info(worker_id)
            
            if worker_info and self._has_capabilities(worker_info.capabilities, required_capabilities):
                capable_workers.append(worker_id)
        
        return capable_workers
    
    def get_worker_info(self, worker_id: str) -> Optional[WorkerInfo]:
        """Get detailed information about a specific worker using UnifiedRedisManager"""
        from .redis_manager import retrieve_structured_data_sync
        
        key = f"{self.WORKERS_KEY_PREFIX}{worker_id}"
        data = retrieve_structured_data_sync("worker_readiness", key, format_type="hash")
        
        if not data:
            return None
        
        # Check if we have the minimum required fields for a valid WorkerInfo
        # If data is corrupted/partial (e.g., only has last_heartbeat), treat as missing
        required_fields = ['worker_id', 'state', 'capabilities']
        if not all(field in data for field in required_fields):
            logger.warning(
                "worker_info_corrupted_or_partial",
                worker_id=worker_id,
                available_fields=list(data.keys())[:50],
            )
            self._delete_corrupt_registration(worker_id, key)
            return None
        
        try:
            # The standardized Redis manager handles type conversion automatically
            # We just need to handle the WorkerState enum conversion
            if 'state' in data and isinstance(data['state'], str):
                data['state'] = WorkerState(data['state'])
            
            return WorkerInfo.from_dict(data)
        except Exception as e:
            # If validation fails, treat as corrupted and delete the key
            logger.warning(
                "worker_info_validation_failed",
                worker_id=worker_id,
                error=str(e),
                available_fields=list(data.keys())[:50] if isinstance(data, dict) else None,
                exc_info=True,
            )
            self._delete_corrupt_registration(worker_id, key)
            return None
    
    def get_worker_active_commands(self, worker_id: str) -> int:
        """Get the current number of active commands for a specific worker (REAL-TIME)"""
        worker_info = self.get_worker_info(worker_id)
        if worker_info:
            return worker_info.active_commands
        return 0  # Default to 0 if worker not found
    
    def increment_active_commands(self, worker_id: str) -> None:
        """Increment active command count for a registered worker."""
        self._adjust_active_commands(worker_id, 1)
    
    def decrement_active_commands(self, worker_id: str) -> None:
        """Decrement active command count for a registered worker."""
        self._adjust_active_commands(worker_id, -1)

    def _adjust_active_commands(self, worker_id: str, delta: int) -> None:
        """Atomically adjust active_commands only when the registration hash exists.

        ``HINCRBY`` / ``HSET`` on a missing key would invent an orphan hash that
        ``SMEMBERS worker:registered`` cannot see.
        """
        key = f"{self.WORKERS_KEY_PREFIX}{worker_id}"
        if not self.redis_client.exists(key):
            logger.warning(
                "worker_active_commands_unregistered",
                worker_id=worker_id,
                delta=delta,
            )
            return

        if delta < 0:
            current_active = int(self.redis_client.hget(key, "active_commands") or 0)
            if current_active <= 0:
                self.redis_client.hset(key, "last_heartbeat", str(time.time()))
                return
        new_active_count = self.redis_client.hincrby(key, "active_commands", delta)
        self.redis_client.hset(key, "last_heartbeat", str(time.time()))
        self._mark_registered(self.redis_client, worker_id)

        if DEBUG_MODE:
            logger.debug(
                "worker_active_commands_adjusted",
                worker_id=worker_id,
                delta=delta,
                active_commands=new_active_count,
            )
    
    
    def get_all_workers(self) -> Dict[str, WorkerInfo]:
        """Get information about all registered workers.

        Lists members of ``worker:registered`` then ``HGETALL`` each
        ``worker:registration:{id}`` in one pipeline. Empty hashes are
        skipped and dropped from the membership set. Does not scan the
        keyspace.
        """
        worker_ids = self._decode_worker_ids(
            self.redis_client.smembers(self.REGISTERED_WORKERS_SET)
        )
        if DEBUG_MODE:
            logger.debug(
                "worker_readiness_get_all_workers_members",
                members_count=len(worker_ids),
                members_preview=worker_ids[:50],
            )

        keys = [f"{self.WORKERS_KEY_PREFIX}{worker_id}" for worker_id in worker_ids]
        hashes = self._hgetall_many(keys)

        workers: Dict[str, WorkerInfo] = {}
        stale_ids: List[str] = []
        for worker_id, key, data in zip(worker_ids, keys, hashes):
            try:
                if not data:
                    stale_ids.append(worker_id)
                    continue

                decoded_data = dict(data)
                self._convert_worker_data_types(decoded_data)
                workers[worker_id] = WorkerInfo.from_dict(decoded_data)
            except Exception as e:
                logger.warning(
                    "worker_readiness_load_worker_failed",
                    redis_key=key,
                    error=str(e),
                    exc_info=True,
                )
                continue

        if stale_ids:
            self._forget_worker_membership(*stale_ids)

        return workers
    
    def _convert_worker_data_types(self, data: Dict[str, Any]) -> None:
        """Convert string values from Redis to appropriate types for WorkerInfo"""
        # Convert numeric fields
        numeric_fields = [
            'active_commands', 'max_concurrency', 'tool_count', 'mcp_tool_count',
            'startup_time', 'last_heartbeat', 'warmup_duration_ms'
        ]
        
        for field in numeric_fields:
            if field in data and isinstance(data[field], str):
                try:
                    if field in ['startup_time', 'last_heartbeat']:
                        data[field] = float(data[field])
                    else:
                        data[field] = int(data[field])
                except (ValueError, TypeError):
                    # Keep as string if conversion fails
                    pass
        
        # Convert boolean fields
        boolean_fields = ['warmup_completed']
        for field in boolean_fields:
            if field in data and isinstance(data[field], str):
                data[field] = data[field].lower() in ('true', '1', 'yes')
        
        # Convert JSON fields
        json_fields = ['capabilities']
        for field in json_fields:
            if field in data and isinstance(data[field], str):
                try:
                    data[field] = json.loads(data[field])
                except (json.JSONDecodeError, TypeError):
                    pass
    
    def remove_worker(self, worker_id: str) -> None:
        """Remove worker from registry"""
        key = f"{self.WORKERS_KEY_PREFIX}{worker_id}"
        self.redis_client.delete(key)
        self._forget_worker_membership(worker_id)
        logger.info("worker_removed", worker_id=worker_id)
    
    
    def get_readiness_stats(self) -> Dict[str, Any]:
        """Get overall system readiness statistics with validation"""
        # IMPROVEMENT 2: Validate data consistency before returning stats
        validation_stats = self._validate_ready_workers_set()
        
        all_workers = self.get_all_workers()
        ready_workers = self.get_ready_workers()
        
        state_counts = {}
        for worker_info in all_workers.values():
            state = worker_info.state.value
            state_counts[state] = state_counts.get(state, 0) + 1
        
        total_capacity = sum(w.max_concurrency for w in all_workers.values())
        active_commands = sum(w.active_commands for w in all_workers.values())
        
        stats = {
            'total_workers': len(all_workers),
            'ready_workers': len(ready_workers),
            'state_distribution': state_counts,
            'total_capacity': total_capacity,
            'active_commands': active_commands,
            'utilization_percent': (active_commands / total_capacity * 100) if total_capacity > 0 else 0,
            'average_tools_per_worker': sum(w.tool_count for w in all_workers.values()) / len(all_workers) if all_workers else 0
        }
        
        # Include validation info if there were fixes applied
        if validation_stats.get("validation_fixed"):
            stats['_validation_applied'] = True
            stats['_validation_stats'] = validation_stats
        
        return stats
    
    def wait_for_ready_workers(self, 
                                   required_capabilities: Optional[List[str]] = None,
                                   min_workers: int = 1,
                                   timeout_seconds: int = 30) -> bool:
        """Wait for minimum number of ready workers with required capabilities"""
        start_time = time.time()
        
        while time.time() - start_time < timeout_seconds:
            ready_workers = self.get_ready_workers(required_capabilities)
            
            if len(ready_workers) >= min_workers:
                return True
            
            worker_sleep(1)  # Check every second
        
        return False
    
    # Private methods
    
    def _store_worker_info(self, worker_info: WorkerInfo) -> None:
        """Store worker information in Redis with enhanced TTL using UnifiedRedisManager"""
        from .redis_manager import store_structured_data_sync
        
        key = f"{self.WORKERS_KEY_PREFIX}{worker_info.worker_id}"
        data = worker_info.to_dict()
        if DEBUG_MODE:
            logger.debug(
                "worker_info_store_start",
                redis_key=key,
                worker_id=worker_info.worker_id,
                fields=list(data.keys())[:50],
            )
        
        try:
            # Use standardized Redis manager method for consistent data storage
            store_structured_data_sync("worker_readiness", key, data, format_type="hash")
            if DEBUG_MODE:
                logger.debug("worker_info_store_success", redis_key=key, worker_id=worker_info.worker_id)
            
            # IMPROVEMENT 3: Enhanced TTL - longer for worker info keys
            # Use 5x heartbeat interval to allow for sleep scenarios (10 minutes)
            # This provides resilience against laptop sleep/wake cycles
            enhanced_ttl = self.WORKER_HEARTBEAT_TTL * 5  # 120 * 5 = 600 seconds (10 minutes)
            self.redis_client.expire(key, enhanced_ttl)
            self._mark_registered(self.redis_client, worker_info.worker_id)
            if DEBUG_MODE:
                logger.debug(
                    "worker_info_store_ttl_set",
                    redis_key=key,
                    worker_id=worker_info.worker_id,
                    ttl_seconds=enhanced_ttl,
                )
            
        except Exception as e:
            logger.error(
                "worker_info_store_failed",
                redis_key=key,
                worker_id=worker_info.worker_id,
                error=str(e),
                exc_info=True,
            )
            raise
    
    def _has_capabilities(self, worker_caps: List[str], required_caps: List[str]) -> bool:
        """Check if worker has all required capabilities"""
        return all(cap in worker_caps for cap in required_caps)

    def _decode_worker_id(self, worker_id: Any) -> str:
        """Normalize a Redis set member to a worker id string."""
        if isinstance(worker_id, bytes):
            return worker_id.decode("utf-8")
        return str(worker_id)

    def _decode_worker_ids(self, raw_ids: Any) -> List[str]:
        """Normalize a Redis set (or empty) to worker id strings."""
        return [self._decode_worker_id(member) for member in (raw_ids or [])]

    def _mark_registered(self, redis_client: Any, worker_id: str) -> None:
        """Add a worker id to the registered membership set."""
        redis_client.sadd(self.REGISTERED_WORKERS_SET, worker_id)

    def _delete_corrupt_registration(self, worker_id: str, key: str) -> None:
        """Delete a bad registration hash and drop set membership."""
        self.redis_client.delete(key)
        self._forget_worker_membership(worker_id)

    def _hgetall_many(self, keys: List[str]) -> List[Any]:
        """Fetch many hashes in one non-transactional pipeline."""
        if not keys:
            return []
        pipe = self.redis_client.pipeline(transaction=False)
        for key in keys:
            pipe.hgetall(key)
        return list(pipe.execute())

    def _forget_worker_membership(self, *worker_ids: str) -> None:
        """Drop worker ids from ready and registered sets."""
        if not worker_ids:
            return
        self.redis_client.srem(self.READY_WORKERS_SET, *worker_ids)
        self.redis_client.srem(self.REGISTERED_WORKERS_SET, *worker_ids)

    def _prune_set_missing_hashes(self, set_key: str) -> None:
        """Remove set members whose registration hash no longer exists."""
        raw_ids = self.redis_client.smembers(set_key)
        if not raw_ids:
            return

        stale_workers = []
        for decoded_id in self._decode_worker_ids(raw_ids):
            worker_key = f"{self.WORKERS_KEY_PREFIX}{decoded_id}"
            if not self.redis_client.exists(worker_key):
                stale_workers.append(decoded_id)

        if stale_workers:
            self.redis_client.srem(set_key, *stale_workers)
            if DEBUG_MODE:
                logger.debug(
                    "worker_readiness_set_stale_entries_removed",
                    set_key=set_key,
                    stale_workers_count=len(stale_workers),
                    stale_workers=stale_workers[:50],
                )
    
    def _cleanup_stale_worker_entries(self) -> None:
        """
        Remove worker IDs from ready and registered sets that have no
        corresponding registration hash.
        """
        try:
            self._prune_set_missing_hashes(self.READY_WORKERS_SET)
            self._prune_set_missing_hashes(self.REGISTERED_WORKERS_SET)
        except Exception as e:
            logger.warning("worker_readiness_stale_worker_cleanup_failed", error=str(e), exc_info=True)
    
    def _validate_ready_workers_set(self) -> Dict[str, Any]:
        """
        IMPROVEMENT 2: Validate set membership against actual worker data
        Returns validation statistics and fixes inconsistencies
        Uses distributed lock only for write operations (optimized)
        """
        try:
            # Read operations - no lock needed (Redis operations are atomic)
            ready_set_workers = set(
                self._decode_worker_ids(self.redis_client.smembers(self.READY_WORKERS_SET))
            )
            all_workers = self.get_all_workers()
            
            # Find workers that should be in ready set but aren't
            should_be_ready = set()
            for worker_id, worker_info in all_workers.items():
                if worker_info.state in [WorkerState.READY, WorkerState.ACCEPTING]:
                    should_be_ready.add(worker_id)
            
            # Calculate discrepancies
            missing_from_set = should_be_ready - ready_set_workers
            stale_in_set = ready_set_workers - should_be_ready
            
            # Only acquire lock if we need to make changes
            if missing_from_set or stale_in_set:
                write_success = self._apply_ready_set_fixes(missing_from_set, stale_in_set)
                if not write_success:
                    return {
                        "error": "Could not acquire validation lock for write operations",
                        "validation_skipped": True,
                        "missing_from_set": len(missing_from_set),
                        "stale_in_set": len(stale_in_set)
                    }
            
            return {
                "total_workers": len(all_workers),
                "ready_set_size": len(ready_set_workers),
                "should_be_ready": len(should_be_ready),
                "missing_from_set": len(missing_from_set),
                "stale_in_set": len(stale_in_set),
                "validation_fixed": len(missing_from_set) + len(stale_in_set) > 0
            }
            
        except Exception as e:
            logger.warning("worker_readiness_ready_workers_validation_failed", error=str(e), exc_info=True)
            return {"error": str(e)}
    
    def _apply_ready_set_fixes(self, missing_from_set: set, stale_in_set: set) -> bool:
        """
        Apply fixes to ready workers set with distributed lock protection.
        Returns True if fixes were applied, False if lock could not be acquired.
        """
        from .redis_manager import acquire_distributed_lock_sync
        
        # Acquire distributed lock only for write operations
        lock = acquire_distributed_lock_sync(
            "worker_readiness", 
            self._validation_lock_key, 
            ttl_seconds=15  # Shorter TTL since we're only doing writes
        )
        
        if not lock:
            return False
        
        try:
            # Apply fixes under lock protection
            if missing_from_set:
                self.redis_client.sadd(self.READY_WORKERS_SET, *missing_from_set)
                logger.info(
                    "worker_readiness_ready_set_fixed_added_missing",
                    missing_count=len(missing_from_set),
                )
            
            if stale_in_set:
                self.redis_client.srem(self.READY_WORKERS_SET, *stale_in_set)
                logger.info(
                    "worker_readiness_ready_set_fixed_removed_stale",
                    stale_count=len(stale_in_set),
                )
            
            return True
            
        except Exception as e:
            logger.warning("worker_readiness_ready_set_fix_failed", error=str(e), exc_info=True)
            return False
        finally:
            # Always release the distributed lock
            lock.release_sync()

    def _cleanup_stale_workers(self):
        """Remove workers that haven't sent heartbeats recently.

        NOTE: Uses extended TTL (5x heartbeat interval) to match Redis key TTL.
        This prevents premature cleanup during laptop sleep scenarios.

        Lifecycle-managed states (STOPPED, STARTING, RESTARTING, TERMINATING) are
        excluded so we do not remove workers that are intentionally not heartbeating
        (e.g. stopped container, or container starting up). This prevents "all
        workers disappear" when starting a worker triggers a cleanup run that
        would otherwise remove workers based on stale heartbeat.
        """
        extended_ttl = self.WORKER_HEARTBEAT_TTL * 5  # 600 seconds (10 minutes)
        cutoff_time = time.time() - extended_ttl
        all_workers = self.get_all_workers()

        lifecycle_managed_states = {
            WorkerState.STOPPED,
            WorkerState.STARTING,
            WorkerState.RESTARTING,
            WorkerState.TERMINATING,
        }

        stale_workers = [
            worker_id for worker_id, worker_info in all_workers.items()
            if worker_info.state not in lifecycle_managed_states
            and worker_info.last_heartbeat > 0  # Skip uninitialized/corrupt (0)
            and worker_info.last_heartbeat < cutoff_time
        ]

        for worker_id in stale_workers:
            self.remove_worker(worker_id)

        if stale_workers:
            logger.info("worker_readiness_stale_workers_cleaned", stale_workers_count=len(stale_workers))


# Global service instance
_readiness_service: Optional[WorkerReadinessService] = None


def get_readiness_service() -> WorkerReadinessService:
    """Get or create the global worker readiness service (synchronous)"""
    global _readiness_service
    
    if _readiness_service is None:
        # Use unified Redis manager - completely synchronous initialization!
        _readiness_service = WorkerReadinessService()
    
    return _readiness_service
