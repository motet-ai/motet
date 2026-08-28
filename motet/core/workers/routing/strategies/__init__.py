"""
Motet - Routing Strategies

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Routing strategies for the Motet distributed framework.
    Provides sophisticated routing algorithms for worker selection and load balancing.

Dependencies:
    - Base routing strategy interface
    - Load-based and performance strategies
    - Capability and geographic strategies
    - Cost and tenant-aware strategies

Usage:
    from motet.core.workers.routing.strategies import LeastLoadedStrategy, get_strategy
    
    # Get strategy
    strategy = get_strategy("least_loaded")
    
    # Create strategy instance
    strategy = LeastLoadedStrategy()

Notes:
    - Supports multiple routing algorithms
    - Includes load-based and performance strategies
    - Provides capability and geographic routing
    - Integrates with filtering system
"""

from .base import RoutingStrategy, WorkerScore
from .load_based import LeastLoadedStrategy, RoundRobinStrategy, WeightedRoundRobinStrategy
from .performance import FastestResponseStrategy, StateAwareStrategy, AdaptiveStrategy
from .capability import CapabilityOptimizedStrategy, SpecializedWorkerStrategy, MultiCapabilityStrategy
from .geographic import GeographicProximityStrategy, DataLocalityStrategy, RegionalStrategy
from .cost import CostOptimizedStrategy, BudgetAwareStrategy, SpotInstanceStrategy
from .tenant import TenantAffinityStrategy, TenantIsolationStrategy, MultiTenantStrategy
from .specific import (
    SpecificWorkerStrategy as SpecificWorkerStrategyImpl, 
    SessionAffinityStrategy, 
    AffinityBasedStrategy
)

# Alias to avoid naming conflict
SpecificWorkerStrategy = SpecificWorkerStrategyImpl

# Strategy registry for easy access
STRATEGY_REGISTRY = {
    # Load-based strategies
    'least_loaded': LeastLoadedStrategy,
    'round_robin': RoundRobinStrategy,
    'weighted_round_robin': WeightedRoundRobinStrategy,
    
    # Performance strategies
    'fastest_response': FastestResponseStrategy,
    'state_aware': StateAwareStrategy,
    'adaptive': AdaptiveStrategy,
    
    # Capability strategies
    'capability_optimized': CapabilityOptimizedStrategy,
    'specialized_worker': SpecializedWorkerStrategy,
    'multi_capability': MultiCapabilityStrategy,
    
    # Geographic strategies
    'geographic_proximity': GeographicProximityStrategy,
    'data_locality': DataLocalityStrategy,
    'regional': RegionalStrategy,
    
    # Cost strategies
    'cost_optimized': CostOptimizedStrategy,
    'budget_aware': BudgetAwareStrategy,
    'spot_instance': SpotInstanceStrategy,
    
    # Tenant strategies
    'tenant_affinity': TenantAffinityStrategy,
    'tenant_isolation': TenantIsolationStrategy,
    'multi_tenant': MultiTenantStrategy,
    
    # Specific worker strategies
    'specific_worker': SpecificWorkerStrategy,
    'session_affinity': SessionAffinityStrategy,
    'affinity_based': AffinityBasedStrategy,
}

def get_strategy(name: str, **kwargs) -> RoutingStrategy:
    """Get a routing strategy instance by name"""
    if name not in STRATEGY_REGISTRY:
        raise ValueError(f"Unknown routing strategy: {name}. Available: {list(STRATEGY_REGISTRY.keys())}")
    
    strategy_class = STRATEGY_REGISTRY[name]
    return strategy_class(**kwargs)

def list_strategies() -> list[str]:
    """Get list of available routing strategy names"""
    return list(STRATEGY_REGISTRY.keys())

__all__ = [
    # Base classes
    'RoutingStrategy',
    'WorkerScore',
    
    # Load-based strategies
    'LeastLoadedStrategy',
    'RoundRobinStrategy', 
    'WeightedRoundRobinStrategy',
    
    # Performance strategies
    'FastestResponseStrategy',
    'StateAwareStrategy',
    'AdaptiveStrategy',
    
    # Capability strategies
    'CapabilityOptimizedStrategy',
    'SpecializedWorkerStrategy',
    'MultiCapabilityStrategy',
    
    # Geographic strategies
    'GeographicProximityStrategy',
    'DataLocalityStrategy',
    'RegionalStrategy',
    
    # Cost strategies
    'CostOptimizedStrategy',
    'BudgetAwareStrategy',
    'SpotInstanceStrategy',
    
    # Tenant strategies
    'TenantAffinityStrategy',
    'TenantIsolationStrategy',
    'MultiTenantStrategy',
    
    # Specific worker strategies
    'SpecificWorkerStrategy',
    'SessionAffinityStrategy',
    'AffinityBasedStrategy',
    
    # Utilities
    'STRATEGY_REGISTRY',
    'get_strategy',
    'list_strategies',
]
