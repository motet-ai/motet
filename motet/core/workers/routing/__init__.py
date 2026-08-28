"""
Motet - Command Routing

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Command routing system for the Motet distributed framework.
    Provides intelligent worker routing with pluggable strategies and filters.

Dependencies:
    - Worker router and communication
    - Routing strategies and filters
    - Worker capability management
    - Load balancing and optimization

Usage:
    from motet.core.workers.routing import WorkerRouter, ReadinessFilter
    
    # Create router
    router = WorkerRouter()
    
    # Add filters
    router.add_filter(ReadinessFilter())

Notes:
    - Supports multiple routing strategies
    - Includes comprehensive filtering
    - Provides worker communication
    - Integrates with distributed architecture
"""

from .worker_router import WorkerRouter
from .filters import ReadinessFilter, CapabilityFilter
from .filters.circuit_breaker import CircuitBreakerFilter
from .worker_communicator import WorkerCommunicator
from .filter_trace import FilterTrace, FilterStep

__all__ = [
    'WorkerRouter',
    'ReadinessFilter', 
    'CapabilityFilter',
    'CircuitBreakerFilter',
    'WorkerCommunicator',
    'FilterTrace',
    'FilterStep'
]
