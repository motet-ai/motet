"""
Motet - Capability-Based Routing Strategies

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Capability-based routing strategies for the Motet distributed framework.
    Routes commands based on worker capabilities and specialization.

Dependencies:
    - typing: Type hints and annotations
    - Base routing strategy interface
    - Capability matching algorithms

Usage:
    from motet.core.workers.routing.strategies.capability import CapabilityOptimizedStrategy
    
    # Create capability strategy
    strategy = CapabilityOptimizedStrategy()
    
    # Route based on capabilities
    worker = strategy.select_worker(workers, context)

Notes:
    - Provides capability-based routing algorithms
    - Includes worker specialization support
    - Supports capability matching and scoring
    - Integrates with distributed architecture
"""

from typing import Dict, List, Optional, Any, Set
from .base import RoutingStrategy, RoutingContext, WorkerScore, calculate_capability_match_score


class CapabilityOptimizedStrategy(RoutingStrategy):
    """Route to workers with optimal capability matches"""
    
    def select_worker(self, workers: List[Dict[str, Any]], context: RoutingContext) -> Optional[Dict[str, Any]]:
        if not workers:
            return None
        
        # Score workers by capability match
        best_worker = None
        best_score = -1
        
        for worker in workers:
            worker_capabilities = set(worker.get('capabilities', []))
            score = calculate_capability_match_score(worker, context.required_capabilities)
            
            if score > best_score:
                best_score = score
                best_worker = worker
        
        if best_worker:
            selected = best_worker.copy()
            selected['selection_reason'] = f"Capability optimized (score: {best_score:.2f})"
            return selected
        
        return None
    
    def get_strategy_name(self) -> str:
        return "Capability Optimized"


class SpecializedWorkerStrategy(RoutingStrategy):
    """Route to workers specialized for specific command types"""
    
    def __init__(self, specializations: Optional[Dict[str, List[str]]] = None):
        self.specializations = specializations or {}
    
    def select_worker(self, workers: List[Dict[str, Any]], context: RoutingContext) -> Optional[Dict[str, Any]]:
        if not workers:
            return None
        
        # Check for specialized workers
        specialized_workers = self.specializations.get(context.command_type, [])
        
        for worker_id in specialized_workers:
            for worker in workers:
                if worker.get('worker_id') == worker_id:
                    selected = worker.copy()
                    selected['selection_reason'] = f"Specialized for {context.command_type}"
                    return selected
        
        # Fall back to least loaded
        return min(workers, key=lambda w: w.get('current_load', 1.0))
    
    def get_strategy_name(self) -> str:
        return "Specialized Worker"


class MultiCapabilityStrategy(RoutingStrategy):
    """Route considering multiple capability requirements"""
    
    def select_worker(self, workers: List[Dict[str, Any]], context: RoutingContext) -> Optional[Dict[str, Any]]:
        if not workers:
            return None
        
        # Filter workers with all required capabilities
        capable_workers = []
        for worker in workers:
            worker_capabilities = set(worker.get('capabilities', []))
            if context.required_capabilities.issubset(worker_capabilities):
                capable_workers.append(worker)
        
        if not capable_workers:
            return None
        
        # Select least loaded among capable workers
        selected = min(capable_workers, key=lambda w: w.get('current_load', 1.0))
        selected = selected.copy()
        selected['selection_reason'] = "Multi-capability match with least load"
        
        return selected
    
    def get_strategy_name(self) -> str:
        return "Multi-Capability"
