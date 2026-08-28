"""
Motet - State Registry

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    State Registry for the Motet distributed framework.

Dependencies:
    - typing: Type hints and annotations
    - Base interfaces and implementations

Usage:
    from motet.core.distributed.state_registry import StateRegistry

Notes:
    - Provides core functionality
    - Integrates with distributed architecture
"""


from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Awaitable
from uuid import uuid4
from pydantic import BaseModel, Field

from .redis_manager import get_sync_redis_client


class StateStatus(Enum):
    """Status of worker state."""
    ACTIVE = "active"
    WARMING = "warming"
    EXPIRED = "expired"
    FAILED = "failed"


class StateTypeDefinition(BaseModel):
    """Definition of a type of warm state that workers can maintain."""
    
    name: str
    default_ttl_seconds: int
    reproduction_cost_ms: int  # Estimated cost to recreate this state
    routing_weight: float  # 0.0-1.0, higher = prefer workers with this state
    
    # Optional callbacks for state management (excluded from serialization)
    detector: Optional[Callable[[Dict[str, Any]], bool]] = Field(default=None, exclude=True)
    reproducer: Optional[Callable[[Dict[str, Any]], Awaitable[bool]]] = Field(default=None, exclude=True)
    validator: Optional[Callable[[Dict[str, Any]], Awaitable[bool]]] = Field(default=None, exclude=True)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return self.model_dump()


class WorkerState(BaseModel):
    """Represents the state of a specific worker process."""
    
    worker_id: str
    worker_pid: int
    state_type: str
    status: StateStatus = StateStatus.ACTIVE
    
    # Timing
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None
    last_used_at: datetime = Field(default_factory=datetime.utcnow)
    
    # Metadata
    metadata: Dict[str, Any] = Field(default_factory=dict)
    reproduction_cost_ms: int = 0
    usage_count: int = 0
    
    @property
    def is_expired(self) -> bool:
        """Check if this state has expired."""
        if not self.expires_at:
            return False
        return datetime.utcnow() > self.expires_at
    
    @property
    def age_seconds(self) -> float:
        """Get age of this state in seconds."""
        return (datetime.utcnow() - self.created_at).total_seconds()
    
    @property
    def time_until_expiry_seconds(self) -> Optional[float]:
        """Get time until expiry in seconds."""
        if not self.expires_at:
            return None
        delta = self.expires_at - datetime.utcnow()
        return max(0, delta.total_seconds())
    
    def touch(self):
        """Update last used timestamp."""
        self.last_used_at = datetime.utcnow()
        self.usage_count += 1
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for Redis storage (JSON-serializable)."""
        return self.model_dump(mode="json")
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> WorkerState:
        """Create WorkerState from dictionary."""
        return cls.model_validate(data)


class WarmStateTypeRegistry:
    """Registry for different types of warm state."""
    
    def __init__(self):
        self.state_types: Dict[str, StateTypeDefinition] = {}
        self._register_builtin_types()
    
    def register_state_type(self, state_def: StateTypeDefinition):
        """Register a new state type."""
        self.state_types[state_def.name] = state_def
    
    def get_state_type(self, name: str) -> Optional[StateTypeDefinition]:
        """Get state type definition by name."""
        return self.state_types.get(name)
    
    def list_state_types(self) -> List[StateTypeDefinition]:
        """List all registered state types."""
        return list(self.state_types.values())
    
    def _register_builtin_types(self):
        """Register built-in state types."""
        
        # MCP Connection State
        self.register_state_type(StateTypeDefinition(
            name="mcp_connection",
            default_ttl_seconds=300,  # 5 minutes
            reproduction_cost_ms=150,
            routing_weight=0.8
        ))
        
        # Model Cache State  
        self.register_state_type(StateTypeDefinition(
            name="model_cache",
            default_ttl_seconds=1800,  # 30 minutes
            reproduction_cost_ms=2000,
            routing_weight=0.9
        ))
        
        # Database Pool State
        self.register_state_type(StateTypeDefinition(
            name="database_pool",
            default_ttl_seconds=600,  # 10 minutes
            reproduction_cost_ms=50,
            routing_weight=0.6
        ))
        
        # WebSocket Connection State
        self.register_state_type(StateTypeDefinition(
            name="websocket_connection",
            default_ttl_seconds=900,  # 15 minutes
            reproduction_cost_ms=100,
            routing_weight=0.7
        ))


class EphemeralStateRegistry:
    """Redis-based registry for tracking ephemeral worker state."""
    
    def __init__(self, redis_client, key_prefix: str = "worker:state"):
        self.redis = redis_client
        self.key_prefix = key_prefix
        self.state_type_registry = WarmStateTypeRegistry()
    
    def _make_key(self, worker_id: str, state_type: str) -> str:
        """Generate Redis key for worker state."""
        return f"{self.key_prefix}:{worker_id}:{state_type}"
    
    def _make_index_key(self, state_type: str) -> str:
        """Generate Redis key for state type index."""
        return f"{self.key_prefix}:index:{state_type}"
    
    def register_worker_state(self, worker_id: str, worker_pid: int, 
                                    state_type: str, ttl_seconds: Optional[int] = None,
                                    metadata: Optional[Dict[str, Any]] = None) -> WorkerState:
        """Register that a worker has acquired a specific type of state."""
        
        # Get state type definition
        state_def = self.state_type_registry.get_state_type(state_type)
        if not state_def:
            raise ValueError(f"Unknown state type: {state_type}")
        
        # Create worker state
        ttl = ttl_seconds or state_def.default_ttl_seconds
        worker_state = WorkerState(
            worker_id=worker_id,
            worker_pid=worker_pid,
            state_type=state_type,
            expires_at=datetime.utcnow() + timedelta(seconds=ttl),
            metadata=metadata or {},
            reproduction_cost_ms=state_def.reproduction_cost_ms
        )
        
        # Store in Redis
        key = self._make_key(worker_id, state_type)
        self.redis.setex(
            key,
            ttl,
            json.dumps(worker_state.to_dict())
        )
        
        # Add to state type index
        index_key = self._make_index_key(state_type)
        self.redis.sadd(index_key, f"{worker_id}:{worker_pid}")
        self.redis.expire(index_key, ttl + 60)  # Index expires slightly later
        
        return worker_state
    
    def get_worker_state(self, worker_id: str, state_type: str) -> Optional[WorkerState]:
        """Get worker state if it exists and is not expired."""
        key = self._make_key(worker_id, state_type)
        data = self.redis.get(key)
        
        if not data:
            return None
        
        try:
            worker_state = WorkerState.from_dict(json.loads(data))
            
            # Check if expired
            if worker_state.is_expired:
                self.remove_worker_state(worker_id, state_type)
                return None
            
            return worker_state
            
        except (json.JSONDecodeError, KeyError, ValueError):
            # Corrupted data, remove it
            self.remove_worker_state(worker_id, state_type)
            return None
    
    def touch_worker_state(self, worker_id: str, state_type: str) -> bool:
        """Update last used timestamp for worker state."""
        worker_state = self.get_worker_state(worker_id, state_type)
        if not worker_state:
            return False
        
        worker_state.touch()
        
        # Update in Redis
        key = self._make_key(worker_id, state_type)
        ttl = self.redis.ttl(key)
        if ttl > 0:
            self.redis.setex(
                key,
                ttl,
                json.dumps(worker_state.to_dict())
            )
            return True
        
        return False
    
    def remove_worker_state(self, worker_id: str, state_type: str):
        """Remove worker state from registry."""
        key = self._make_key(worker_id, state_type)
        self.redis.delete(key)
        
        # Remove from index
        index_key = self._make_index_key(state_type)
        worker_state = self.get_worker_state(worker_id, state_type)
        if worker_state:
            self.redis.srem(index_key, f"{worker_id}:{worker_state.worker_pid}")
    
    def find_workers_with_state(self, state_type: str, 
                                      limit: Optional[int] = None) -> List[WorkerState]:
        """Find all workers that have a specific type of state."""
        index_key = self._make_index_key(state_type)
        worker_refs = self.redis.smembers(index_key)
        
        worker_states = []
        for worker_ref in worker_refs:
            try:
                worker_id, worker_pid = worker_ref.decode().split(":", 1)
                worker_state = self.get_worker_state(worker_id, state_type)
                if worker_state:
                    worker_states.append(worker_state)
            except (ValueError, AttributeError):
                # Invalid worker reference, remove from index
                self.redis.srem(index_key, worker_ref)
        
        # Sort by last used (most recent first) and apply limit
        worker_states.sort(key=lambda ws: ws.last_used_at, reverse=True)
        
        if limit:
            worker_states = worker_states[:limit]
        
        return worker_states
    
    def get_worker_states(self, worker_id: str) -> List[WorkerState]:
        """Get all states for a specific worker."""
        states = []
        
        for state_type in self.state_type_registry.state_types.keys():
            worker_state = self.get_worker_state(worker_id, state_type)
            if worker_state:
                states.append(worker_state)
        
        return states
    
    def cleanup_expired_states(self) -> int:
        """Clean up expired worker states. Returns number of states cleaned up."""
        cleaned_count = 0
        
        # Check all state types
        for state_type in self.state_type_registry.state_types.keys():
            index_key = self._make_index_key(state_type)
            worker_refs = self.redis.smembers(index_key)
            
            for worker_ref in worker_refs:
                try:
                    worker_id, worker_pid = worker_ref.decode().split(":", 1)
                    worker_state = self.get_worker_state(worker_id, state_type)
                    
                    if not worker_state or worker_state.is_expired:
                        self.remove_worker_state(worker_id, state_type)
                        cleaned_count += 1
                        
                except (ValueError, AttributeError):
                    # Invalid worker reference, remove from index
                    self.redis.srem(index_key, worker_ref)
                    cleaned_count += 1
        
        return cleaned_count
    
    def get_registry_stats(self) -> Dict[str, Any]:
        """Get statistics about the state registry."""
        stats = {
            "total_workers": 0,
            "total_states": 0,
            "state_types": {},
            "registry_health": "healthy"
        }
        
        worker_ids = set()
        
        for state_type in self.state_type_registry.state_types.keys():
            workers_with_state = self.find_workers_with_state(state_type)
            
            stats["state_types"][state_type] = {
                "active_workers": len(workers_with_state),
                "total_usage": sum(ws.usage_count for ws in workers_with_state),
                "avg_age_seconds": sum(ws.age_seconds for ws in workers_with_state) / len(workers_with_state) if workers_with_state else 0
            }
            
            stats["total_states"] += len(workers_with_state)
            worker_ids.update(ws.worker_id for ws in workers_with_state)
        
        stats["total_workers"] = len(worker_ids)
        
        return stats


# Global state registry instance (will be initialized with Redis client)
global_state_registry: Optional[EphemeralStateRegistry] = None


def initialize_state_registry(redis_client=None, key_prefix: str = "worker:state"):
    """Initialize the global state registry."""
    global global_state_registry
    
    if global_state_registry is None:
        # Use unified Redis manager if no client provided
        if redis_client is None:
            redis_client = get_sync_redis_client("state_registry")
        
        global_state_registry = EphemeralStateRegistry(redis_client, key_prefix)


def get_state_registry() -> Optional[EphemeralStateRegistry]:
    """Get the global state registry instance."""
    return global_state_registry


def register_worker_state(worker_id: str, worker_pid: int, state_type: str,
                                ttl_seconds: Optional[int] = None,
                                metadata: Optional[Dict[str, Any]] = None) -> Optional[WorkerState]:
    """Convenience function to register worker state."""
    if global_state_registry:
        return global_state_registry.register_worker_state(
            worker_id, worker_pid, state_type, ttl_seconds, metadata
        )
    return None


def find_workers_with_state(state_type: str, limit: Optional[int] = None) -> List[WorkerState]:
    """Convenience function to find workers with specific state."""
    if global_state_registry:
        return global_state_registry.find_workers_with_state(state_type, limit)
    return []


def touch_worker_state(worker_id: str, state_type: str) -> bool:
    """Convenience function to touch worker state."""
    if global_state_registry:
        return global_state_registry.touch_worker_state(worker_id, state_type)
    return False
