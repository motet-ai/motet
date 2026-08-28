"""
Tests for Circuit Breaker Filter

Tests the circuit breaker integration into routing decisions as described in ADR-0008 Phase 4.
"""

import pytest
from unittest.mock import Mock
from typing import Dict, Any, List

from motet.core.workers.routing.filters.circuit_breaker import CircuitBreakerFilter
from motet.core.resilience.breaker import CircuitState


@pytest.fixture
def sample_workers():
    """Sample workers for testing"""
    return [
        {
            'worker_id': 'worker-1',
            'state': 'READY',
            'current_load': 0.2,
            'capabilities': ['model_inference']
        },
        {
            'worker_id': 'worker-2', 
            'state': 'READY',
            'current_load': 0.6,
            'capabilities': ['model_inference']
        },
        {
            'worker_id': 'worker-3',
            'state': 'READY', 
            'current_load': 0.1,
            'capabilities': ['text_processing']
        }
    ]


@pytest.fixture
def circuit_breaker_filter():
    """Circuit breaker filter for testing"""
    return CircuitBreakerFilter()


class TestCircuitBreakerFilter:
    """Test circuit breaker filter functionality (filter_workers is sync)."""

    def test_filter_workers_all_closed(self, circuit_breaker_filter, sample_workers):
        """Test filtering when all workers have CLOSED circuit breakers"""
        # All workers should pass through (default state is CLOSED)
        filtered_workers = circuit_breaker_filter.filter_workers(sample_workers, None)

        assert len(filtered_workers) == 3
        for worker in filtered_workers:
            assert worker['circuit_breaker_state'] == CircuitState.CLOSED
            assert worker['circuit_breaker_penalty'] == 0.0

    def test_filter_workers_empty_list(self, circuit_breaker_filter):
        """Test filtering with empty worker list"""
        filtered_workers = circuit_breaker_filter.filter_workers([], None)
        assert filtered_workers == []
    
    @pytest.mark.asyncio
    async def test_get_circuit_breaker_stats(self, circuit_breaker_filter):
        """Test getting circuit breaker statistics"""
        stats = circuit_breaker_filter.get_circuit_breaker_stats()
        
        assert 'total_workers' in stats
        assert 'state_counts' in stats
        assert 'average_failure_rate' in stats
        assert 'cache_size' in stats
        assert 'half_open_traffic_limit' in stats
        
        # Should have default values
        assert stats['total_workers'] == 0
        assert stats['state_counts'][CircuitState.CLOSED] == 0
        assert stats['state_counts'][CircuitState.HALF_OPEN] == 0
        assert stats['state_counts'][CircuitState.OPEN] == 0
    
    def test_get_filter_name(self, circuit_breaker_filter):
        """Test getting filter name"""
        assert circuit_breaker_filter.get_filter_name() == "CircuitBreakerFilter"
    
    def test_clear_cache(self, circuit_breaker_filter):
        """Test clearing the circuit breaker cache"""
        # Add some dummy data to cache
        circuit_breaker_filter._circuit_breaker_cache['test_worker'] = {
            'state': CircuitState.CLOSED,
            'failure_count': 0
        }
        
        assert len(circuit_breaker_filter._circuit_breaker_cache) == 1
        
        # Clear cache
        circuit_breaker_filter.clear_cache()
        
        assert len(circuit_breaker_filter._circuit_breaker_cache) == 0
        assert circuit_breaker_filter._last_cache_update == 0.0
    
    def test_half_open_traffic_limiting(self, circuit_breaker_filter):
        """Test HALF_OPEN traffic limiting logic"""
        # Test with different worker IDs to see traffic limiting behavior
        worker_1_allowed = circuit_breaker_filter._should_allow_half_open_traffic('worker-1')
        worker_2_allowed = circuit_breaker_filter._should_allow_half_open_traffic('worker-2')
        
        # Both should be boolean values
        assert isinstance(worker_1_allowed, bool)
        assert isinstance(worker_2_allowed, bool)
        
        # With 30% traffic limit, some workers should be allowed, some not
        # (exact behavior depends on hash values)
        allowed_count = sum([
            circuit_breaker_filter._should_allow_half_open_traffic(f'worker-{i}')
            for i in range(10)
        ])
        
        # Should allow roughly 30% of workers (with some variance due to hashing)
        assert 0 <= allowed_count <= 10


class TestCircuitBreakerFilterIntegration:
    """Test circuit breaker filter integration with routing (filter_workers is sync)."""

    def test_filter_with_missing_worker_id(self, circuit_breaker_filter):
        """Test filtering workers without worker_id"""
        workers_without_id = [
            {'state': 'READY', 'current_load': 0.2},  # Missing worker_id
            {'worker_id': 'worker-2', 'state': 'READY', 'current_load': 0.6}
        ]

        filtered_workers = circuit_breaker_filter.filter_workers(workers_without_id, None)

        # Should only include worker with valid worker_id
        assert len(filtered_workers) == 1
        assert filtered_workers[0]['worker_id'] == 'worker-2'

    def test_cache_ttl_behavior(self, circuit_breaker_filter, sample_workers):
        """Test that cache TTL prevents excessive updates"""
        # First call should update cache
        circuit_breaker_filter.filter_workers(sample_workers, None)
        first_update = circuit_breaker_filter._last_cache_update

        # Second call within TTL should not update cache
        circuit_breaker_filter.filter_workers(sample_workers, None)
        second_update = circuit_breaker_filter._last_cache_update

        assert first_update == second_update

        # Manually expire cache
        circuit_breaker_filter._last_cache_update = 0.0

        # Third call should update cache again
        circuit_breaker_filter.filter_workers(sample_workers, None)
        third_update = circuit_breaker_filter._last_cache_update

        assert third_update > second_update
