"""
Motet - Resilience Module

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Resilience system for the Motet distributed framework.
    Provides circuit breakers, retry logic, bulkheads, and fallback strategies.

Dependencies:
    - Circuit breaker implementations
    - Retry logic with exponential backoff
    - Bulkhead pattern for resource isolation
    - Fallback strategies for graceful degradation

Usage:
    from motet.core.resilience import CircuitBreaker, retry
    
    # Circuit breaker
    breaker = CircuitBreaker(failure_threshold=5, timeout=60)
    
    # Retry logic
    result = retry(operation, max_attempts=3)

Notes:
    - Provides fault tolerance and graceful degradation
    - Includes configurable circuit breakers
    - Supports retry logic with backoff
    - Integrates with distributed architecture
"""

from __future__ import annotations

from .breaker import (
    CircuitBreaker, CircuitState, get_breaker, get_breaker_configured,
    get_all_breaker_stats, reset_all_breakers,
    FallbackStrategy, DefaultValueFallback, CachedResponseFallback,
    ResilientServiceCaller, global_resilient_caller
)  # noqa: F401
from .retry import retry, retry_sync, exponential_backoff  # noqa: F401
from .bulkhead import Bulkhead, bulkhead_sync  # noqa: F401

__all__ = [
    # Enhanced circuit breaker
    "CircuitBreaker",
    "CircuitState", 
    "get_breaker",
    "get_breaker_configured",
    "get_all_breaker_stats",
    "reset_all_breakers",
    
    # Fallback strategies
    "FallbackStrategy",
    "DefaultValueFallback",
    "CachedResponseFallback",
    
    # Resilient service caller
    "ResilientServiceCaller",
    "global_resilient_caller",
    
    # Existing components
    "retry",
    "retry_sync", 
    "exponential_backoff",
    "Bulkhead",
    "bulkhead_sync",
]


