"""
Motet - Load-Based Routing Strategies

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Load-based routing strategies for the Motet distributed framework.
    Routes commands based on worker load metrics for optimal load distribution.

Dependencies:
    - random: Random selection for load balancing
    - typing: Type hints and annotations
    - Base routing strategy interface
    - Load monitoring and metrics

Usage:
    from motet.core.workers.routing.strategies.load_based import LeastLoadedStrategy
    
    # Create load strategy
    strategy = LeastLoadedStrategy()
    
    # Route based on load
    worker = strategy.select_worker(workers, context)

Notes:
    - Provides load-based routing algorithms
    - Includes least-loaded and round-robin strategies
    - Supports weighted load distribution
    - Integrates with distributed architecture
"""

import random
from typing import Dict, List, Optional, Any
from .base import RoutingStrategy, RoutingContext, WorkerScore, calculate_load_score


class LeastLoadedStrategy(RoutingStrategy):
    """
    Route to the worker with the lowest current load.
    
    This is the most common load balancing strategy, ensuring even
    distribution of work across available workers.
    """
    
    def select_worker(self, 
                     workers: List[Dict[str, Any]], 
                     context: RoutingContext) -> Optional[Dict[str, Any]]:
        """Select worker with lowest current load"""
        if not workers:
            return None
        
        # Sort by current load (ascending)
        sorted_workers = sorted(workers, key=lambda w: w.get('current_load', 1.0))
        selected = sorted_workers[0]
        
        # Add selection metadata
        selected = selected.copy()
        selected['selection_reason'] = f"Least loaded (load: {selected.get('current_load', 'unknown')})"
        selected['load_rank'] = 1
        
        return selected
    
    def score_workers(self, 
                     workers: List[Dict[str, Any]], 
                     context: RoutingContext) -> List[WorkerScore]:
        """Score workers based on load (lower load = higher score)"""
        scores = []
        
        for worker in workers:
            load_score = calculate_load_score(worker)
            current_load = worker.get('current_load', 1.0)
            
            score = WorkerScore(
                worker_id=worker.get('worker_id', 'unknown'),
                score=load_score,
                reasoning=f"Load-based score: {load_score:.2f} (current load: {current_load:.2f})",
                metadata={
                    'current_load': current_load,
                    'load_score': load_score,
                    'active_commands': worker.get('active_commands', 0),
                    'max_concurrency': worker.get('max_concurrency', 1)
                }
            )
            scores.append(score)
        
        return scores
    
    def get_strategy_name(self) -> str:
        return "Least Loaded"


class RoundRobinStrategy(RoutingStrategy):
    """
    Route to workers in round-robin fashion.
    
    This strategy ensures even distribution regardless of current load,
    which can be useful for predictable workloads.
    """
    
    def __init__(self):
        self.current_index = 0
        self.worker_order = {}  # Track consistent ordering
    
    def select_worker(self, 
                     workers: List[Dict[str, Any]], 
                     context: RoutingContext) -> Optional[Dict[str, Any]]:
        """Select next worker in round-robin order"""
        if not workers:
            return None
        
        # Create consistent ordering based on worker IDs
        worker_ids = sorted([w.get('worker_id', '') for w in workers])
        
        # Update current index
        if len(worker_ids) > 0:
            self.current_index = (self.current_index + 1) % len(worker_ids)
            target_worker_id = worker_ids[self.current_index]
            
            # Find the worker with this ID
            for worker in workers:
                if worker.get('worker_id') == target_worker_id:
                    selected = worker.copy()
                    selected['selection_reason'] = f"Round-robin selection (index: {self.current_index})"
                    selected['round_robin_index'] = self.current_index
                    return selected
        
        # Fallback to first worker
        selected = workers[0].copy()
        selected['selection_reason'] = "Round-robin fallback"
        return selected
    
    def get_strategy_name(self) -> str:
        return "Round Robin"


class WeightedRoundRobinStrategy(RoutingStrategy):
    """
    Route to workers using weighted round-robin based on capacity.
    
    Workers with higher capacity receive proportionally more requests.
    """
    
    def __init__(self):
        self.worker_weights = {}
        self.current_weights = {}
    
    def select_worker(self, 
                     workers: List[Dict[str, Any]], 
                     context: RoutingContext) -> Optional[Dict[str, Any]]:
        """Select worker using weighted round-robin algorithm"""
        if not workers:
            return None
        
        # Calculate weights based on max_concurrency
        total_weight = 0
        worker_weights = {}
        
        for worker in workers:
            worker_id = worker.get('worker_id', '')
            max_concurrency = worker.get('max_concurrency', 1)
            current_load = worker.get('current_load', 0.0)
            
            # Weight based on available capacity
            available_capacity = max(0, max_concurrency * (1.0 - current_load))
            weight = max(1, int(available_capacity))  # Minimum weight of 1
            
            worker_weights[worker_id] = weight
            total_weight += weight
        
        if total_weight == 0:
            # All workers at capacity, fall back to round-robin
            return RoundRobinStrategy().select_worker(workers, context)
        
        # Update current weights
        for worker_id, weight in worker_weights.items():
            if worker_id not in self.current_weights:
                self.current_weights[worker_id] = 0
            self.current_weights[worker_id] += weight
        
        # Select worker with highest current weight
        selected_worker_id = max(self.current_weights.keys(), 
                               key=lambda wid: self.current_weights[wid])
        
        # Reduce selected worker's current weight
        self.current_weights[selected_worker_id] -= total_weight
        
        # Find and return the selected worker
        for worker in workers:
            if worker.get('worker_id') == selected_worker_id:
                selected = worker.copy()
                selected['selection_reason'] = f"Weighted round-robin (weight: {worker_weights[selected_worker_id]})"
                selected['assigned_weight'] = worker_weights[selected_worker_id]
                return selected
        
        # Fallback
        return workers[0]
    
    def get_strategy_name(self) -> str:
        return "Weighted Round Robin"


class RandomStrategy(RoutingStrategy):
    """
    Route to a random worker.
    
    Simple strategy that can be useful for testing or when
    sophisticated load balancing is not needed.
    """
    
    def select_worker(self, 
                     workers: List[Dict[str, Any]], 
                     context: RoutingContext) -> Optional[Dict[str, Any]]:
        """Select a random worker"""
        if not workers:
            return None
        
        selected = random.choice(workers).copy()
        selected['selection_reason'] = f"Random selection from {len(workers)} workers"
        
        return selected
    
    def get_strategy_name(self) -> str:
        return "Random"


class PowerOfTwoStrategy(RoutingStrategy):
    """
    Power of Two Choices load balancing.
    
    Randomly select two workers and choose the one with lower load.
    This provides good load balancing with minimal overhead.
    """
    
    def select_worker(self, 
                     workers: List[Dict[str, Any]], 
                     context: RoutingContext) -> Optional[Dict[str, Any]]:
        """Select better of two random workers"""
        if not workers:
            return None
        
        if len(workers) == 1:
            selected = workers[0].copy()
            selected['selection_reason'] = "Only worker available"
            return selected
        
        # Randomly select two workers
        candidates = random.sample(workers, min(2, len(workers)))
        
        # Choose the one with lower load
        selected = min(candidates, key=lambda w: w.get('current_load', 1.0))
        selected = selected.copy()
        
        other_load = max(candidates, key=lambda w: w.get('current_load', 1.0)).get('current_load', 1.0)
        selected['selection_reason'] = f"Power of two (load: {selected.get('current_load', 'unknown')} vs {other_load})"
        
        return selected
    
    def get_strategy_name(self) -> str:
        return "Power of Two"


class ConsistentHashStrategy(RoutingStrategy):
    """
    Consistent hashing for session affinity.
    
    Routes commands to workers based on a hash of session/user ID,
    ensuring the same user always goes to the same worker when possible.
    """
    
    def __init__(self, hash_key: str = 'session_id'):
        """
        Initialize consistent hash strategy.
        
        Args:
            hash_key: Key to use for hashing (session_id, user_id, etc.)
        """
        self.hash_key = hash_key
    
    def select_worker(self, 
                     workers: List[Dict[str, Any]], 
                     context: RoutingContext) -> Optional[Dict[str, Any]]:
        """Select worker using consistent hashing"""
        if not workers:
            return None
        
        # Get hash value from context
        hash_value = getattr(context, self.hash_key, None)
        if not hash_value:
            # No hash key available, fall back to least loaded
            return LeastLoadedStrategy().select_worker(workers, context)
        
        # Sort workers by ID for consistent ordering
        sorted_workers = sorted(workers, key=lambda w: w.get('worker_id', ''))
        
        # Simple hash to select worker
        hash_index = hash(str(hash_value)) % len(sorted_workers)
        selected = sorted_workers[hash_index].copy()
        
        selected['selection_reason'] = f"Consistent hash on {self.hash_key}={hash_value} (index: {hash_index})"
        selected['hash_key'] = self.hash_key
        selected['hash_value'] = hash_value
        selected['hash_index'] = hash_index
        
        return selected
    
    def get_strategy_name(self) -> str:
        return f"Consistent Hash ({self.hash_key})"
