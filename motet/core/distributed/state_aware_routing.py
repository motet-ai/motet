"""
Motet - State Aware Routing

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Routing state aware routing for the Motet distributed framework.

Dependencies:
    - os: Environment guardrails for lifecycle routing
    - typing: Type hints and annotations
    - Base interfaces and implementations

Usage:
    from motet.core.distributed.state_aware_routing import StateAwareRouting

Notes:
    - Provides core functionality
    - Integrates with distributed architecture
"""


from __future__ import annotations

import asyncio
import os
import time
from pydantic import BaseModel
from typing import Any, Dict, List, Optional, Set, Tuple
from enum import Enum

import structlog

from motet.core.workers.worker_utils import get_lifecycle_worker_id
from .state_registry import (
    EphemeralStateRegistry, WorkerState, StateTypeDefinition,
    find_workers_with_state, touch_worker_state
)
from motet.core.commands.distributed import DistributedCommand

logger = structlog.get_logger(__name__)


class RoutingStrategy(Enum):
    """Different routing strategies for command execution."""
    LOAD_BALANCED = "load_balanced"  # Traditional load balancing
    STATE_AWARE = "state_aware"      # Prefer workers with warm state
    HYBRID = "hybrid"                # Combine state awareness with load balancing
    ROUND_ROBIN = "round_robin"      # Simple round robin


class WorkerCandidate(BaseModel):
    """A candidate worker for command execution."""
    
    worker_id: str
    worker_pid: int
    current_load: float
    capabilities: Set[str]
    
    # State information
    warm_states: List[WorkerState]
    state_affinity_score: float = 0.0
    
    # Routing metrics
    total_score: float = 0.0
    selection_reason: str = ""
    
    def has_capability(self, capability) -> bool:
        """Check if worker has a specific capability."""
        # Handle both enum and string capabilities
        capability_str = capability.value if hasattr(capability, 'value') else str(capability)
        return capability_str in self.capabilities
    
    def has_state_type(self, state_type: str) -> bool:
        """Check if worker has a specific type of warm state."""
        return any(ws.state_type == state_type for ws in self.warm_states)
    
    def get_state_freshness(self, state_type: str) -> float:
        """Get freshness score (0-1) for a state type. Higher = fresher."""
        for ws in self.warm_states:
            if ws.state_type == state_type:
                if ws.time_until_expiry_seconds is None:
                    return 1.0
                # Convert time until expiry to freshness score
                max_ttl = 1800  # 30 minutes max
                time_left = ws.time_until_expiry_seconds
                return min(1.0, time_left / max_ttl)
        return 0.0


class StateAwareRouter:
    """Routes distributed commands based on worker state and capabilities."""
    
    def __init__(self, state_registry: Optional[EphemeralStateRegistry] = None,
                 default_strategy: RoutingStrategy = RoutingStrategy.HYBRID):
        self.state_registry = state_registry
        self.default_strategy = default_strategy
        
        # Routing configuration
        self.state_weight = 0.7  # How much to weight state affinity (0-1)
        self.load_weight = 0.3   # How much to weight current load (0-1)
        self.freshness_threshold = 0.2  # Minimum freshness to consider state useful
        
        # Performance tracking
        self.routing_stats = {
            "total_routes": 0,
            "state_aware_routes": 0,
            "load_balanced_routes": 0,
            "fallback_routes": 0,
            "avg_routing_time_ms": 0.0
        }
    
    def select_optimal_worker(self, 
                                    command: DistributedCommand,
                                    available_workers: List[Dict[str, Any]],
                                    strategy: Optional[RoutingStrategy] = None) -> Optional[WorkerCandidate]:
        """Select the optimal worker for command execution."""
        
        start_time = time.time()
        
        try:
            if not available_workers:
                return None
            
            # Use specified strategy or default
            routing_strategy = strategy or self.default_strategy
            
            # Convert to worker candidates with state information
            candidates = self._build_worker_candidates(available_workers)
            
            # Filter by required capabilities
            required_caps = command.get_required_capabilities()
            if required_caps:
                candidates = [c for c in candidates if all(c.has_capability(cap) for cap in required_caps)]
            
            # Exclude lifecycle worker unless explicitly required
            candidates = self._filter_lifecycle_candidates(candidates, required_caps)
            
            if not candidates:
                return None
            
            # Apply routing strategy
            if routing_strategy == RoutingStrategy.STATE_AWARE:
                selected = self._select_state_aware(command, candidates)
            elif routing_strategy == RoutingStrategy.LOAD_BALANCED:
                selected = self._select_load_balanced(candidates)
            elif routing_strategy == RoutingStrategy.HYBRID:
                selected = self._select_hybrid(command, candidates)
            else:  # ROUND_ROBIN
                selected = self._select_round_robin(candidates)
            
            # Update routing stats
            routing_time_ms = (time.time() - start_time) * 1000
            self._update_routing_stats(routing_strategy, routing_time_ms)
            
            # Touch the worker's state if we're using it
            if selected and self.state_registry:
                self._touch_worker_states(selected)
            
            return selected
            
        except Exception as e:
            logger.warning(
                "state_aware_routing_failed_fallback_load_balanced",
                error=str(e),
                exc_info=True,
            )
            # Fallback to simple load balancing
            if available_workers:
                worker = min(available_workers, key=lambda w: w.get('current_load', 1.0))
                if not self._is_lifecycle_worker_allowed(worker, command.get_required_capabilities()):
                    return None
                return WorkerCandidate(
                    worker_id=worker['worker_id'],
                    worker_pid=worker.get('worker_pid', 0),
                    current_load=worker.get('current_load', 1.0),
                    capabilities=set(worker.get('capabilities', [])),
                    warm_states=[],
                    selection_reason="fallback_after_error"
                )
            return None
    
    def _build_worker_candidates(self, available_workers: List[Dict[str, Any]]) -> List[WorkerCandidate]:
        """Build worker candidates with state information."""
        candidates = []
        
        for worker in available_workers:
            worker_id = worker['worker_id']
            worker_pid = worker.get('worker_pid', 0)
            
            # Get warm states for this worker
            warm_states = []
            if self.state_registry:
                try:
                    warm_states = self.state_registry.get_worker_states(worker_id)
                except Exception:
                    # State registry error, continue without state info
                    pass
            
            candidate = WorkerCandidate(
                worker_id=worker_id,
                worker_pid=worker_pid,
                current_load=worker.get('current_load', 1.0),
                capabilities=set(worker.get('capabilities', [])),
                warm_states=warm_states
            )
            
            candidates.append(candidate)
        
        return candidates

    def _is_lifecycle_worker_allowed(
        self,
        worker: Dict[str, Any],
        required_caps: Optional[Set[Any]],
    ) -> bool:
        lifecycle_worker_id = get_lifecycle_worker_id()
        worker_id = worker.get("worker_id")
        if worker_id != lifecycle_worker_id:
            return True

        required_values = {
            cap.value if hasattr(cap, "value") else str(cap)
            for cap in (required_caps or set())
        }
        return "worker_lifecycle_management" in required_values

    def _filter_lifecycle_candidates(
        self,
        candidates: List[WorkerCandidate],
        required_caps: Optional[Set[Any]],
    ) -> List[WorkerCandidate]:
        required_values = {
            cap.value if hasattr(cap, "value") else str(cap)
            for cap in (required_caps or set())
        }
        if "worker_lifecycle_management" in required_values:
            return candidates

        lifecycle_worker_id = get_lifecycle_worker_id()
        return [
            candidate
            for candidate in candidates
            if candidate.worker_id != lifecycle_worker_id
            and "worker_lifecycle_management" not in candidate.capabilities
        ]
    
    def _select_state_aware(self, command: DistributedCommand, 
                                  candidates: List[WorkerCandidate]) -> Optional[WorkerCandidate]:
        """Select worker based primarily on state affinity."""
        
        # Determine what state types this command might benefit from
        beneficial_states = self._identify_beneficial_states(command)
        
        if not beneficial_states:
            # No beneficial state identified, fall back to load balancing
            return self._select_load_balanced(candidates)
        
        # Score candidates based on state affinity
        for candidate in candidates:
            candidate.state_affinity_score = self._calculate_state_affinity(
                candidate, beneficial_states
            )
        
        # Select candidate with highest state affinity
        # If multiple have same affinity, prefer lower load
        best_candidates = sorted(
            candidates,
            key=lambda c: (-c.state_affinity_score, c.current_load)
        )
        
        selected = best_candidates[0]
        selected.selection_reason = f"state_aware (affinity: {selected.state_affinity_score:.2f})"
        
        return selected
    
    def _select_load_balanced(self, candidates: List[WorkerCandidate]) -> WorkerCandidate:
        """Select worker based on current load."""
        selected = min(candidates, key=lambda c: c.current_load)
        selected.selection_reason = f"load_balanced (load: {selected.current_load:.2f})"
        return selected
    
    def _select_hybrid(self, command: DistributedCommand, 
                             candidates: List[WorkerCandidate]) -> Optional[WorkerCandidate]:
        """Select worker using hybrid state + load strategy."""
        
        # Get beneficial states
        beneficial_states = self._identify_beneficial_states(command)
        
        # Calculate composite scores
        for candidate in candidates:
            # State affinity score (0-1)
            state_score = 0.0
            if beneficial_states:
                state_score = self._calculate_state_affinity(candidate, beneficial_states)
            
            # Load score (inverted, so lower load = higher score)
            load_score = max(0.0, 1.0 - candidate.current_load)
            
            # Composite score
            candidate.total_score = (
                self.state_weight * state_score +
                self.load_weight * load_score
            )
            candidate.state_affinity_score = state_score
        
        # Select highest scoring candidate
        selected = max(candidates, key=lambda c: c.total_score)
        selected.selection_reason = (
            f"hybrid (total: {selected.total_score:.2f}, "
            f"state: {selected.state_affinity_score:.2f}, "
            f"load: {selected.current_load:.2f})"
        )
        
        return selected
    
    def _select_round_robin(self, candidates: List[WorkerCandidate]) -> WorkerCandidate:
        """Select worker using round robin (for testing/comparison)."""
        # Simple implementation - in practice would track last selected
        selected = candidates[0]
        selected.selection_reason = "round_robin"
        return selected
    
    def _identify_beneficial_states(self, command: DistributedCommand) -> List[str]:
        """Identify what state types would benefit this command."""
        beneficial_states = []
        
        command_type = command.get_command_type()
        
        # Map command types to beneficial state types
        if "tool" in command_type or "mcp" in command_type:
            beneficial_states.append("mcp_connection")
        
        if "model" in command_type or "inference" in command_type:
            beneficial_states.append("model_cache")
        
        if "memory" in command_type or "database" in command_type:
            beneficial_states.append("database_pool")
        
        # Check for specific tool requirements in command parameters
        if hasattr(command, 'tool_name'):
            tool_name = getattr(command, 'tool_name', '')
            if 'weather' in tool_name.lower():
                beneficial_states.append("mcp_connection")
        
        return beneficial_states
    
    def _calculate_state_affinity(self, candidate: WorkerCandidate, 
                                  beneficial_states: List[str]) -> float:
        """Calculate state affinity score for a candidate."""
        if not beneficial_states:
            return 0.0
        
        total_score = 0.0
        
        for state_type in beneficial_states:
            if candidate.has_state_type(state_type):
                # Base score for having the state
                base_score = 1.0
                
                # Bonus for freshness
                freshness = candidate.get_state_freshness(state_type)
                if freshness >= self.freshness_threshold:
                    freshness_bonus = freshness * 0.5  # Up to 0.5 bonus
                    total_score += base_score + freshness_bonus
                else:
                    # State is too stale, don't count it
                    pass
        
        # Normalize by number of beneficial states
        return total_score / len(beneficial_states)
    
    def _touch_worker_states(self, selected: WorkerCandidate):
        """Update last used timestamp for worker's states."""
        if not self.state_registry:
            return
        
        for state in selected.warm_states:
            try:
                touch_worker_state(selected.worker_id, state.state_type)
            except Exception:
                # Ignore touch errors
                pass
    
    def _update_routing_stats(self, strategy: RoutingStrategy, routing_time_ms: float):
        """Update routing performance statistics."""
        self.routing_stats["total_routes"] += 1
        
        # Update strategy-specific counters
        if strategy == RoutingStrategy.STATE_AWARE:
            self.routing_stats["state_aware_routes"] += 1
        elif strategy == RoutingStrategy.LOAD_BALANCED:
            self.routing_stats["load_balanced_routes"] += 1
        else:
            self.routing_stats["fallback_routes"] += 1
        
        # Update average routing time
        total_routes = self.routing_stats["total_routes"]
        current_avg = self.routing_stats["avg_routing_time_ms"]
        new_avg = ((current_avg * (total_routes - 1)) + routing_time_ms) / total_routes
        self.routing_stats["avg_routing_time_ms"] = new_avg
    
    def get_routing_stats(self) -> Dict[str, Any]:
        """Get routing performance statistics."""
        stats = self.routing_stats.copy()
        
        if stats["total_routes"] > 0:
            stats["state_aware_percentage"] = (
                stats["state_aware_routes"] / stats["total_routes"] * 100
            )
            stats["load_balanced_percentage"] = (
                stats["load_balanced_routes"] / stats["total_routes"] * 100
            )
        else:
            stats["state_aware_percentage"] = 0.0
            stats["load_balanced_percentage"] = 0.0
        
        return stats


# Global state-aware router instance
global_state_aware_router: Optional[StateAwareRouter] = None


def initialize_state_aware_router(state_registry: Optional[EphemeralStateRegistry] = None,
                                  default_strategy: RoutingStrategy = RoutingStrategy.HYBRID):
    """Initialize the global state-aware router."""
    global global_state_aware_router
    global_state_aware_router = StateAwareRouter(state_registry, default_strategy)


def get_state_aware_router() -> Optional[StateAwareRouter]:
    """Get the global state-aware router instance."""
    return global_state_aware_router


def select_optimal_worker_with_state(command: DistributedCommand,
                                           available_workers: List[Dict[str, Any]],
                                           strategy: Optional[RoutingStrategy] = None) -> Optional[WorkerCandidate]:
    """Convenience function for state-aware worker selection."""
    if global_state_aware_router:
        return global_state_aware_router.select_optimal_worker(
            command, available_workers, strategy
        )
    
    # Fallback to simple load balancing
    if available_workers:
        worker = min(available_workers, key=lambda w: w.get('current_load', 1.0))
        return WorkerCandidate(
            worker_id=worker['worker_id'],
            worker_pid=worker.get('worker_pid', 0),
            current_load=worker.get('current_load', 1.0),
            capabilities=set(worker.get('capabilities', [])),
            warm_states=[],
            selection_reason="fallback_no_router"
        )
    
    return None
