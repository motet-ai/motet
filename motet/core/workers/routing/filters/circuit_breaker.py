"""
Motet - Circuit Breaker

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-28

Description:
    Routing circuit breaker for the Motet distributed framework.

Dependencies:
    - typing: Type hints and annotations
    - Base interfaces and implementations

Usage:
    from motet.core.workers.routing.filters.circuit_breaker import CircuitBreaker

Notes:
    - Provides core functionality
    - Integrates with distributed architecture
"""


import time
from typing import Dict, List, Any, Optional
import structlog

from ....resilience.breaker import get_breaker_configured, CircuitState
from .base import WorkerFilter

# Prometheus metrics
try:
    import prometheus_client  # presence only; constructors imported in _init_prometheus_metrics

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

logger = structlog.get_logger(__name__)


class CircuitBreakerFilter(WorkerFilter):
    """
    Filter that excludes workers with open circuit breakers from routing.
    
    This filter implements the circuit breaker integration described in ADR-0008:
    - Checks worker-level circuit breaker states
    - Excludes workers with OPEN circuit breakers
    - Allows limited traffic to workers with HALF_OPEN circuit breakers
    - Provides circuit breaker state information for routing decisions
    """
    
    def __init__(self, 
                 default_failure_threshold: int = 3,
                 default_reset_timeout_seconds: float = 120.0,
                 half_open_traffic_limit: float = 0.3):
        """
        Initialize circuit breaker filter.
        
        Args:
            default_failure_threshold: Default failure threshold for worker circuit breakers
            default_reset_timeout_seconds: Default reset timeout for worker circuit breakers
            half_open_traffic_limit: Fraction of traffic to allow for HALF_OPEN workers (0.0-1.0)
        """
        self.default_failure_threshold = default_failure_threshold
        self.default_reset_timeout_seconds = default_reset_timeout_seconds
        self.half_open_traffic_limit = half_open_traffic_limit
        
        # Track circuit breaker states for routing decisions
        self._circuit_breaker_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_ttl_seconds = 10  # Cache circuit breaker states for 10 seconds
        self._last_cache_update = 0.0
        
        # Initialize Prometheus metrics
        self._init_prometheus_metrics()
        
        logger.info("CircuitBreakerFilter initialized",
                   default_failure_threshold=default_failure_threshold,
                   default_reset_timeout=default_reset_timeout_seconds,
                   half_open_traffic_limit=half_open_traffic_limit)
    
    def _init_prometheus_metrics(self):
        """Initialize Prometheus metrics for circuit breaker monitoring"""
        if not PROMETHEUS_AVAILABLE:
            return
        from prometheus_client import Counter, Gauge, Histogram

        # Use class-level metrics to avoid duplicate registration
        if not hasattr(CircuitBreakerFilter, '_metrics_initialized'):
            try:
                # Circuit breaker state metrics
                self._circuit_breaker_states = Gauge(
                    'motet_circuit_breaker_workers_by_state',
                    'Number of workers by circuit breaker state',
                    ['state']
                )
                
                # Circuit breaker filtering metrics
                self._filtered_workers_total = Counter(
                    'motet_circuit_breaker_filtered_workers_total',
                    'Total number of workers filtered by circuit breaker state',
                    ['state', 'action']
                )
                
                # Circuit breaker cache metrics
                self._cache_size = Gauge(
                    'motet_circuit_breaker_cache_size',
                    'Number of workers in circuit breaker cache'
                )
                
                # Circuit breaker filtering duration
                self._filter_duration = Histogram(
                    'motet_circuit_breaker_filter_duration_seconds',
                    'Time spent filtering workers by circuit breaker state'
                )
                
                CircuitBreakerFilter._metrics_initialized = True
            except ValueError:
                # Metrics already registered, use existing ones
                pass
        else:
            # Metrics already initialized, use class-level references
            self._circuit_breaker_states = getattr(CircuitBreakerFilter, '_circuit_breaker_states', None)
            self._filtered_workers_total = getattr(CircuitBreakerFilter, '_filtered_workers_total', None)
            self._cache_size = getattr(CircuitBreakerFilter, '_cache_size', None)
            self._filter_duration = getattr(CircuitBreakerFilter, '_filter_duration', None)
    
    def filter_workers(self, 
                      workers: List[Dict[str, Any]], 
                      context: Any) -> List[Dict[str, Any]]:
        """
        Filter workers based on circuit breaker states.
        
        Args:
            workers: List of workers to filter
            context: Routing context (unused in this filter)
            
        Returns:
            List of workers that passed circuit breaker filtering
        """
        if not workers:
            return workers
        
        # Record filtering duration
        start_time = time.time()
        
        # Update circuit breaker cache if needed
        self._update_circuit_breaker_cache(workers)
        
        filtered_workers = []
        excluded_count = 0
        state_counts = {CircuitState.CLOSED: 0, CircuitState.HALF_OPEN: 0, CircuitState.OPEN: 0}
        
        for worker in workers:
            worker_id = worker.get('worker_id')
            if not worker_id:
                continue
            
            # Get circuit breaker state for this worker
            circuit_state = self._get_worker_circuit_breaker_state(worker_id)
            
            if circuit_state == CircuitState.OPEN:
                # Completely exclude workers with OPEN circuit breakers
                excluded_count += 1
                state_counts[CircuitState.OPEN] += 1
                self._record_filter_metric(circuit_state, "excluded")
                logger.warning("Excluding worker due to OPEN circuit breaker",
                             operation="circuit_breaker_filter",
                             worker_id=worker_id,
                             circuit_state=circuit_state,
                             filter_name=self.get_filter_name())
                continue
            
            elif circuit_state == CircuitState.HALF_OPEN:
                # Allow limited traffic to HALF_OPEN workers
                if self._should_allow_half_open_traffic(worker_id):
                    # Add circuit breaker metadata to worker
                    worker = worker.copy()
                    worker['circuit_breaker_state'] = circuit_state
                    worker['circuit_breaker_penalty'] = 0.7  # 30% penalty for HALF_OPEN
                    filtered_workers.append(worker)
                    state_counts[CircuitState.HALF_OPEN] += 1
                    self._record_filter_metric(circuit_state, "allowed")
                    logger.info("Allowing limited traffic to HALF_OPEN worker",
                              operation="circuit_breaker_filter",
                              worker_id=worker_id,
                              circuit_state=circuit_state,
                              filter_name=self.get_filter_name())
                else:
                    excluded_count += 1
                    state_counts[CircuitState.HALF_OPEN] += 1
                    self._record_filter_metric(circuit_state, "excluded")
                    logger.debug("Excluding HALF_OPEN worker due to traffic limit",
                               worker_id=worker_id,
                               circuit_state=circuit_state)
                continue
            
            else:  # CLOSED
                # Full traffic allowed for CLOSED circuit breakers
                worker = worker.copy()
                worker['circuit_breaker_state'] = circuit_state
                worker['circuit_breaker_penalty'] = 0.0  # No penalty for CLOSED
                filtered_workers.append(worker)
                state_counts[CircuitState.CLOSED] += 1
                self._record_filter_metric(circuit_state, "allowed")
        
        # Record final metrics
        self._record_final_metrics(state_counts, len(filtered_workers), start_time)
        
        logger.info("Circuit breaker filtering completed",
                   operation="circuit_breaker_filter",
                   total_workers=len(workers),
                   filtered_workers=len(filtered_workers),
                   excluded_workers=excluded_count,
                   filter_name=self.get_filter_name())
        
        return filtered_workers
    
    def _get_worker_circuit_breaker_state(self, worker_id: str) -> str:
        """
        Get circuit breaker state for a worker.
        
        Args:
            worker_id: Worker identifier
            
        Returns:
            Circuit breaker state (CLOSED, HALF_OPEN, OPEN)
        """
        cache_key = f"worker_{worker_id}"
        cached_state = self._circuit_breaker_cache.get(cache_key)
        
        if cached_state:
            return cached_state.get('state', CircuitState.CLOSED)
        
        # If not cached, assume CLOSED (default state)
        return CircuitState.CLOSED
    
    def _update_circuit_breaker_cache(self, workers: List[Dict[str, Any]]):
        """
        Update the circuit breaker state cache for all workers.
        
        Args:
            workers: List of workers to check circuit breaker states for
        """
        current_time = time.time()
        
        # Only update cache if TTL has expired
        if current_time - self._last_cache_update < self._cache_ttl_seconds:
            return
        
        logger.debug("Updating circuit breaker cache", worker_count=len(workers))
        
        for worker in workers:
            worker_id = worker.get('worker_id')
            if not worker_id:
                continue
            
            try:
                # Get or create circuit breaker for this worker
                breaker_name = f"worker_{worker_id}"
                breaker = get_breaker_configured(
                    breaker_name,
                    default_failure_threshold=self.default_failure_threshold,
                    default_reset_timeout_seconds=self.default_reset_timeout_seconds
                )
                
                # Get circuit breaker state and stats
                breaker_stats = breaker.get_stats()
                
                # Cache the state
                cache_key = f"worker_{worker_id}"
                self._circuit_breaker_cache[cache_key] = {
                    'state': breaker_stats['state'],
                    'failure_count': breaker_stats['failure_count'],
                    'success_count': breaker_stats['success_count'],
                    'recent_failure_rate': breaker_stats['recent_failure_rate'],
                    'time_in_current_state': breaker_stats['time_in_current_state_seconds'],
                    'last_updated': current_time
                }
                
            except Exception as e:
                logger.warning("Failed to get circuit breaker state for worker",
                             worker_id=worker_id,
                             error=str(e))
                # Default to CLOSED state on error
                cache_key = f"worker_{worker_id}"
                self._circuit_breaker_cache[cache_key] = {
                    'state': CircuitState.CLOSED,
                    'failure_count': 0,
                    'success_count': 0,
                    'recent_failure_rate': 0.0,
                    'time_in_current_state': 0.0,
                    'last_updated': current_time
                }
        
        self._last_cache_update = current_time
        
        logger.debug("Circuit breaker cache updated",
                   cached_workers=len(self._circuit_breaker_cache))
    
    def _should_allow_half_open_traffic(self, worker_id: str) -> bool:
        """
        Determine if HALF_OPEN worker should receive traffic based on traffic limiting.
        
        This implements a simple traffic limiting mechanism for HALF_OPEN workers
        to prevent overwhelming them during recovery testing.
        
        Args:
            worker_id: Worker identifier
            
        Returns:
            True if traffic should be allowed, False otherwise
        """
        # Simple hash-based traffic limiting for HALF_OPEN workers
        # This ensures consistent behavior across multiple routing decisions
        worker_hash = hash(worker_id) % 100
        traffic_threshold = int(self.half_open_traffic_limit * 100)
        
        return worker_hash < traffic_threshold
    
    def _record_filter_metric(self, circuit_state: str, action: str):
        """Record a filtering metric"""
        if PROMETHEUS_AVAILABLE and hasattr(self, '_filtered_workers_total') and self._filtered_workers_total is not None:
            self._filtered_workers_total.labels(state=circuit_state, action=action).inc()
    
    def _record_final_metrics(self, state_counts: Dict[str, int], filtered_count: int, start_time: float):
        """Record final metrics after filtering"""
        if not PROMETHEUS_AVAILABLE:
            return
        
        # Record circuit breaker state counts
        if hasattr(self, '_circuit_breaker_states') and self._circuit_breaker_states is not None:
            for state, count in state_counts.items():
                self._circuit_breaker_states.labels(state=state).set(count)
        
        # Record cache size
        if hasattr(self, '_cache_size') and self._cache_size is not None:
            self._cache_size.set(len(self._circuit_breaker_cache))
        
        # Record filtering duration
        if hasattr(self, '_filter_duration') and self._filter_duration is not None:
            duration = time.time() - start_time
            self._filter_duration.observe(duration)
    
    def get_circuit_breaker_stats(self) -> Dict[str, Any]:
        """
        Get circuit breaker statistics for monitoring and debugging.
        
        Returns:
            Dictionary with circuit breaker statistics
        """
        current_time = time.time()
        
        # Count workers by circuit breaker state
        state_counts = {
            CircuitState.CLOSED: 0,
            CircuitState.HALF_OPEN: 0,
            CircuitState.OPEN: 0
        }
        
        total_failure_rate = 0.0
        total_workers = 0
        
        for cache_key, cached_state in self._circuit_breaker_cache.items():
            state = cached_state.get('state', CircuitState.CLOSED)
            state_counts[state] += 1
            
            failure_rate = cached_state.get('recent_failure_rate', 0.0)
            total_failure_rate += failure_rate
            total_workers += 1
        
        avg_failure_rate = total_failure_rate / total_workers if total_workers > 0 else 0.0
        
        return {
            'total_workers': total_workers,
            'state_counts': state_counts,
            'average_failure_rate': avg_failure_rate,
            'cache_size': len(self._circuit_breaker_cache),
            'cache_ttl_seconds': self._cache_ttl_seconds,
            'last_cache_update': self._last_cache_update,
            'half_open_traffic_limit': self.half_open_traffic_limit
        }
    
    def clear_cache(self):
        """Clear the circuit breaker state cache."""
        self._circuit_breaker_cache.clear()
        self._last_cache_update = 0.0
        logger.info("Circuit breaker cache cleared")
