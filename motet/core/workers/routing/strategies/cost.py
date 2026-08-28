"""
Motet - Cost-Based Routing Strategies

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Cost-based routing strategies for the Motet distributed framework.
    Optimizes for cost efficiency and budget constraints in worker selection.

Dependencies:
    - typing: Type hints and annotations
    - Base routing strategy interface
    - Cost optimization algorithms

Usage:
    from motet.core.workers.routing.strategies.cost import CostOptimizedStrategy
    
    # Create cost strategy
    strategy = CostOptimizedStrategy()
    
    # Route based on cost
    worker = strategy.select_worker(workers, context)

Notes:
    - Provides cost-optimized routing algorithms
    - Includes budget constraint handling
    - Supports cost-effective worker selection
    - Integrates with distributed architecture
"""

from typing import Dict, List, Optional, Any
from .base import RoutingStrategy, RoutingContext


class CostOptimizedStrategy(RoutingStrategy):
    """Route to most cost-effective workers"""
    
    def select_worker(self, workers: List[Dict[str, Any]], context: RoutingContext) -> Optional[Dict[str, Any]]:
        if not workers:
            return None
        
        # Select worker with lowest cost per unit
        cheapest_worker = min(workers, key=lambda w: w.get('cost_per_hour', 1.0))
        selected = cheapest_worker.copy()
        selected['selection_reason'] = f"Cost optimized (${cheapest_worker.get('cost_per_hour', 'unknown')}/hour)"
        
        return selected
    
    def get_strategy_name(self) -> str:
        return "Cost Optimized"


class BudgetAwareStrategy(RoutingStrategy):
    """Route within budget constraints"""
    
    def select_worker(self, workers: List[Dict[str, Any]], context: RoutingContext) -> Optional[Dict[str, Any]]:
        if not workers:
            return None
        
        max_cost = context.max_cost
        if max_cost is None:
            # No budget constraint, use least loaded
            return min(workers, key=lambda w: w.get('current_load', 1.0))
        
        # Filter workers within budget
        affordable_workers = [w for w in workers if w.get('cost_per_hour', 0) <= max_cost]
        
        if affordable_workers:
            selected = min(affordable_workers, key=lambda w: w.get('current_load', 1.0))
            selected = selected.copy()
            selected['selection_reason'] = f"Budget aware (max: ${max_cost}/hour)"
            return selected
        
        return None  # No workers within budget
    
    def get_strategy_name(self) -> str:
        return "Budget Aware"


class SpotInstanceStrategy(RoutingStrategy):
    """Route to spot/preemptible instances when available"""
    
    def select_worker(self, workers: List[Dict[str, Any]], context: RoutingContext) -> Optional[Dict[str, Any]]:
        if not workers:
            return None
        
        # Prefer spot instances for cost savings
        spot_workers = [w for w in workers if w.get('is_spot_instance', False)]
        
        if spot_workers:
            selected = min(spot_workers, key=lambda w: w.get('current_load', 1.0))
            selected = selected.copy()
            selected['selection_reason'] = "Spot instance for cost savings"
            return selected
        
        # No spot instances, use regular workers
        return min(workers, key=lambda w: w.get('current_load', 1.0))
    
    def get_strategy_name(self) -> str:
        return "Spot Instance"
