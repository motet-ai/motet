"""
Motet - Base Routing Strategy Interface

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Base routing strategy interface for the Motet distributed framework.
    Defines core interfaces for all routing strategies in the system.

Dependencies:
    - abc: Abstract base classes
    - pydantic: Data validation and serialization
    - enum: Enumeration types
    - typing: Type hints and annotations

Usage:
    from motet.core.workers.routing.strategies.base import RoutingStrategy, RoutingContext
    
    # Create custom strategy
    class MyStrategy(RoutingStrategy):
        async def route(self, context: RoutingContext) -> List[WorkerScore]:
            # Implementation
            pass

Notes:
    - Provides abstract base classes for routing strategies
    - Includes routing context and worker scoring
    - Supports pluggable routing algorithms
    - Integrates with distributed architecture
"""

from abc import ABC, abstractmethod
from pydantic import BaseModel, Field
from typing import Dict, List, Optional, Any, Set
from enum import Enum


class RoutingPriority(Enum):
    """Priority levels for routing decisions"""
    LOW = 1
    NORMAL = 5
    HIGH = 8
    CRITICAL = 10


class WorkerScore(BaseModel):
    """Score assigned to a worker by a routing strategy"""
    worker_id: str
    score: float  # Higher is better
    reasoning: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RoutingContext(BaseModel):
    """Context information for routing decisions"""
    command_type: str
    required_capabilities: Set[str]
    priority: RoutingPriority
    timeout_seconds: int
    tenant_id: Optional[str] = None
    principal_id: Optional[str] = None
    session_id: Optional[str] = None
    preferred_region: Optional[str] = None
    max_cost: Optional[float] = None
    require_specific_worker: bool = False
    target_worker_id: Optional[str] = None
    
    # Worker targeting fields (ADR-0025)
    preferred_worker_ids: List[str] = Field(default_factory=list)  # Preferred workers in order
    worker_affinity: Optional[str] = None  # Affinity key for consistent worker selection
    avoid_worker_ids: List[str] = Field(default_factory=list)  # Workers to avoid
    
    # Pool type preference (ADR-0033)
    preferred_pool_type: Optional[str] = None  # "high_concurrency", "process", or None
    
    @classmethod
    def from_command(cls, command: Any) -> 'RoutingContext':
        """Create routing context from a distributed command"""
        return cls(
            command_type=command.get_command_type(),
            required_capabilities=getattr(command.distributed_context, 'required_capabilities', set()),
            priority=RoutingPriority(getattr(command.distributed_context, 'priority', 5)),
            timeout_seconds=getattr(command.distributed_context, 'timeout_seconds', 60),
            tenant_id=getattr(command, 'tenant_id', None) or getattr(
                getattr(command, 'distributed_context', None), 'tenant_id', None
            ),
            principal_id=getattr(
                getattr(command, 'distributed_context', None), 'principal_id', None
            ),
            session_id=getattr(command, 'session_id', None),
            preferred_region=getattr(command, 'preferred_region', None),
            max_cost=getattr(command, 'max_cost', None),
            require_specific_worker=getattr(command, 'require_specific_worker', False),
            target_worker_id=getattr(command.distributed_context, 'target_worker_id', None),
            # Worker targeting fields (ADR-0025)
            preferred_worker_ids=getattr(command.distributed_context, 'preferred_worker_ids', []),
            worker_affinity=getattr(command.distributed_context, 'worker_affinity', None),
            avoid_worker_ids=getattr(command.distributed_context, 'avoid_worker_ids', []),
            # Pool type preference (ADR-0033)
            preferred_pool_type=getattr(command.distributed_context, 'preferred_pool_type', None),
        )


class RoutingStrategy(ABC):
    """
    Base interface for all routing strategies.
    
    All routing algorithms must implement this interface to be pluggable
    into the WorkerRouter system.
    """
    
    @abstractmethod
    def select_worker(self, 
                     workers: List[Dict[str, Any]], 
                     context: RoutingContext) -> Optional[Dict[str, Any]]:
        """
        Select the optimal worker from available workers.
        
        Args:
            workers: List of available workers with their metadata
            context: Routing context with command requirements
            
        Returns:
            Selected worker dict or None if no suitable worker found
        """
        pass
    
    @abstractmethod
    def get_strategy_name(self) -> str:
        """Get human-readable name for this strategy"""
        pass
    
    def score_workers(self, 
                     workers: List[Dict[str, Any]], 
                     context: RoutingContext) -> List[WorkerScore]:
        """
        Score all workers for the given context.
        
        Default implementation calls select_worker and returns binary scores.
        Strategies can override this for more sophisticated scoring.
        
        Args:
            workers: List of available workers
            context: Routing context
            
        Returns:
            List of WorkerScore objects
        """
        scores = []
        selected_worker = self.select_worker(workers, context)
        
        for worker in workers:
            if selected_worker and worker.get('worker_id') == selected_worker.get('worker_id'):
                score = WorkerScore(
                    worker_id=worker.get('worker_id', 'unknown'),
                    score=1.0,
                    reasoning=f"Selected by {self.get_strategy_name()}",
                    metadata={'selected': True}
                )
            else:
                score = WorkerScore(
                    worker_id=worker.get('worker_id', 'unknown'),
                    score=0.0,
                    reasoning=f"Not selected by {self.get_strategy_name()}",
                    metadata={'selected': False}
                )
            scores.append(score)
        
        return scores
    
    def supports_context(self, context: RoutingContext) -> bool:
        """
        Check if this strategy can handle the given routing context.
        
        Default implementation returns True (all strategies support all contexts).
        Specialized strategies can override this.
        
        Args:
            context: Routing context to check
            
        Returns:
            True if strategy can handle this context
        """
        return True
    
    def get_strategy_metadata(self) -> Dict[str, Any]:
        """
        Get metadata about this strategy.
        
        Returns:
            Dictionary with strategy metadata
        """
        return {
            'name': self.get_strategy_name(),
            'type': self.__class__.__name__,
            'supports_scoring': hasattr(self, 'score_workers'),
            'supports_context_filtering': hasattr(self, 'supports_context'),
        }


class CompositeStrategy(RoutingStrategy):
    """
    Base class for strategies that combine multiple sub-strategies.
    
    Useful for creating complex routing logic by combining simpler strategies.
    """
    
    def __init__(self, strategies: List[RoutingStrategy], weights: Optional[List[float]] = None):
        self.strategies = strategies
        self.weights = weights or [1.0] * len(strategies)
        
        if len(self.strategies) != len(self.weights):
            raise ValueError("Number of strategies must match number of weights")
    
    def score_workers(self, 
                     workers: List[Dict[str, Any]], 
                     context: RoutingContext) -> List[WorkerScore]:
        """
        Combine scores from all sub-strategies using weighted average.
        """
        if not workers:
            return []
        
        # Get scores from all strategies
        all_scores = {}
        for strategy, weight in zip(self.strategies, self.weights):
            strategy_scores = strategy.score_workers(workers, context)
            for score in strategy_scores:
                if score.worker_id not in all_scores:
                    all_scores[score.worker_id] = []
                all_scores[score.worker_id].append((score.score * weight, score.reasoning))
        
        # Combine scores
        combined_scores = []
        for worker_id, score_list in all_scores.items():
            total_score = sum(score for score, _ in score_list)
            reasoning_parts = [reason for _, reason in score_list]
            
            combined_score = WorkerScore(
                worker_id=worker_id,
                score=total_score / len(self.strategies),  # Average
                reasoning=f"Composite: {'; '.join(reasoning_parts)}",
                metadata={
                    'component_scores': score_list,
                    'strategy_count': len(self.strategies)
                }
            )
            combined_scores.append(combined_score)
        
        return combined_scores
    
    def select_worker(self, 
                     workers: List[Dict[str, Any]], 
                     context: RoutingContext) -> Optional[Dict[str, Any]]:
        """Select worker with highest combined score"""
        scores = self.score_workers(workers, context)
        if not scores:
            return None
        
        # Sort by score (descending)
        scores.sort(key=lambda s: s.score, reverse=True)
        best_score = scores[0]
        
        # Find the worker with the best score
        for worker in workers:
            if worker.get('worker_id') == best_score.worker_id:
                # Add selection reasoning to worker metadata
                worker = worker.copy()
                worker['selection_reason'] = best_score.reasoning
                worker['selection_score'] = best_score.score
                return worker
        
        return None
    
    def get_strategy_name(self) -> str:
        strategy_names = [s.get_strategy_name() for s in self.strategies]
        return f"Composite({', '.join(strategy_names)})"


class FallbackStrategy(RoutingStrategy):
    """
    Strategy that tries multiple strategies in order until one succeeds.
    
    Useful for implementing fallback logic (e.g., try specific worker, 
    then fall back to least loaded).
    """
    
    def __init__(self, strategies: List[RoutingStrategy]):
        self.strategies = strategies
    
    def select_worker(self, 
                     workers: List[Dict[str, Any]], 
                     context: RoutingContext) -> Optional[Dict[str, Any]]:
        """Try strategies in order until one returns a worker"""
        for i, strategy in enumerate(self.strategies):
            try:
                selected_worker = strategy.select_worker(workers, context)
                if selected_worker:
                    # Add fallback information
                    selected_worker = selected_worker.copy()
                    selected_worker['selection_reason'] = f"Fallback level {i+1}: {strategy.get_strategy_name()}"
                    selected_worker['fallback_used'] = i > 0
                    return selected_worker
            except Exception as e:
                # Log error and try next strategy
                print(f"⚠️ Strategy {strategy.get_strategy_name()} failed: {e}")
                continue
        
        return None
    
    def get_strategy_name(self) -> str:
        strategy_names = [s.get_strategy_name() for s in self.strategies]
        return f"Fallback({' → '.join(strategy_names)})"


# Utility functions for common routing operations

def filter_workers_by_capabilities(workers: List[Dict[str, Any]], 
                                 required_capabilities: Set[str]) -> List[Dict[str, Any]]:
    """Filter workers that have all required capabilities"""
    if not required_capabilities:
        return workers
    
    filtered_workers = []
    for worker in workers:
        worker_capabilities = set(worker.get('capabilities', []))
        if required_capabilities.issubset(worker_capabilities):
            filtered_workers.append(worker)
    
    return filtered_workers


def calculate_load_score(worker: Dict[str, Any]) -> float:
    """Calculate load-based score for a worker (higher is better)"""
    current_load = worker.get('current_load', 1.0)
    return max(0.0, 1.0 - current_load)  # Invert load (lower load = higher score)


def calculate_capability_match_score(worker: Dict[str, Any], 
                                   required_capabilities: Set[str]) -> float:
    """Calculate capability match score (higher is better)"""
    if not required_capabilities:
        return 1.0
    
    worker_capabilities = set(worker.get('capabilities', []))
    if not required_capabilities.issubset(worker_capabilities):
        return 0.0  # Missing required capabilities
    
    # Score based on how many extra capabilities the worker has
    extra_capabilities = len(worker_capabilities) - len(required_capabilities)
    return 1.0 + (extra_capabilities * 0.1)  # Bonus for extra capabilities
