"""
Tests for Routing Strategies

Tests all routing strategies including tenant-based, performance-based,
and specialized routing algorithms.
"""

import pytest
from unittest.mock import Mock
from typing import Dict, Any, List, Set

from motet.core.workers.routing.strategies.base import RoutingContext, RoutingPriority
from motet.core.workers.routing.strategies.tenant import (
    TenantAffinityStrategy, TenantIsolationStrategy, MultiTenantStrategy
)
from motet.core.workers.routing.strategies.load_based import (
    LeastLoadedStrategy, RoundRobinStrategy, WeightedRoundRobinStrategy
)
from motet.core.workers.routing.strategies.performance import (
    FastestResponseStrategy, StateAwareStrategy, AdaptiveStrategy
)
from motet.core.workers.routing.strategies.specific import (
    SpecificWorkerStrategy, SessionAffinityStrategy, AffinityBasedStrategy
)


@pytest.fixture
def sample_workers():
    """Sample workers for testing"""
    return [
        {
            'worker_id': 'worker-1',
            'state': 'READY',
            'current_load': 0.2,
            'capabilities': ['model_inference', 'text_processing'],
            'active_commands': 1,
            'max_concurrency': 5,
            'region': 'us-east-1',
            'cost_per_hour': 1.0
        },
        {
            'worker_id': 'worker-2',
            'state': 'READY',
            'current_load': 0.6,
            'capabilities': ['model_inference', 'data_processing'],
            'active_commands': 3,
            'max_concurrency': 5,
            'region': 'us-west-1',
            'cost_per_hour': 1.5
        },
        {
            'worker_id': 'worker-3',
            'state': 'READY',
            'current_load': 0.1,
            'capabilities': ['specialized_task'],
            'active_commands': 0,
            'max_concurrency': 3,
            'region': 'eu-west-1',
            'cost_per_hour': 2.0
        }
    ]


@pytest.fixture
def basic_context():
    """Basic routing context for testing"""
    return RoutingContext(
        command_type="TestCommand",
        required_capabilities=set(),
        priority=RoutingPriority.NORMAL,
        timeout_seconds=60
    )


class TestTenantStrategies:
    """Test tenant-based routing strategies"""
    
    def test_tenant_affinity_no_history(self, sample_workers, basic_context):
        """Test tenant affinity with no existing history"""
        strategy = TenantAffinityStrategy()
        basic_context.tenant_id = "new-tenant"
        
        selected = strategy.select_worker(sample_workers, basic_context)
        
        assert selected is not None
        assert selected['worker_id'] in ['worker-1', 'worker-2', 'worker-3']
        assert 'new_session_mapping' not in selected  # This is session affinity feature
    
    def test_tenant_affinity_with_mapping(self, sample_workers, basic_context):
        """Test tenant affinity with pre-configured mapping"""
        tenant_mapping = {"test-tenant": ["worker-2"]}
        strategy = TenantAffinityStrategy(tenant_worker_map=tenant_mapping)
        basic_context.tenant_id = "test-tenant"
        
        selected = strategy.select_worker(sample_workers, basic_context)
        
        assert selected is not None
        assert selected['worker_id'] == 'worker-2'
        assert 'Tenant affinity mapping' in selected['selection_reason']
    
    def test_tenant_isolation_success(self, sample_workers, basic_context):
        """Test tenant isolation with valid assignment"""
        tenant_assignments = {"tenant-1": ["worker-1", "worker-2"]}
        strategy = TenantIsolationStrategy(tenant_assignments)
        basic_context.tenant_id = "tenant-1"
        
        selected = strategy.select_worker(sample_workers, basic_context)
        
        assert selected is not None
        assert selected['worker_id'] in ['worker-1', 'worker-2']
        assert selected['tenant_isolation'] is True
    
    def test_tenant_isolation_no_assignment(self, sample_workers, basic_context):
        """Test tenant isolation with no worker assignment"""
        strategy = TenantIsolationStrategy({})
        basic_context.tenant_id = "unassigned-tenant"
        
        selected = strategy.select_worker(sample_workers, basic_context)
        
        assert selected is None  # No workers assigned to tenant
    
    def test_multi_tenant_strategy(self, sample_workers, basic_context):
        """Test multi-tenant strategy"""
        tenant_priorities = {"high-priority-tenant": 9, "low-priority-tenant": 3}
        strategy = MultiTenantStrategy(tenant_priorities=tenant_priorities)
        basic_context.tenant_id = "high-priority-tenant"
        
        selected = strategy.select_worker(sample_workers, basic_context)
        
        assert selected is not None
        assert 'Multi-tenant optimization' in selected['selection_reason']
        assert 'tenant_score' in selected


class TestLoadBasedStrategies:
    """Test load-based routing strategies"""
    
    def test_least_loaded_strategy(self, sample_workers, basic_context):
        """Test least loaded strategy"""
        strategy = LeastLoadedStrategy()
        
        selected = strategy.select_worker(sample_workers, basic_context)
        
        assert selected is not None
        # worker-3 has lowest load (0.1)
        assert selected['worker_id'] == 'worker-3'
        assert 'Least loaded' in selected['selection_reason']
    
    def test_round_robin_strategy(self, sample_workers, basic_context):
        """Test round robin strategy"""
        strategy = RoundRobinStrategy()
        
        # Make multiple selections to test round-robin behavior
        selections = []
        for _ in range(6):  # More than number of workers
            selected = strategy.select_worker(sample_workers, basic_context)
            selections.append(selected['worker_id'])
        
        # Should cycle through workers
        assert len(set(selections)) == 3  # All workers selected
        # Should repeat after cycling through all workers
        assert selections[0] == selections[3]  # First and fourth should be same
    
    def test_weighted_round_robin_strategy(self, sample_workers, basic_context):
        """Test weighted round robin strategy"""
        strategy = WeightedRoundRobinStrategy()
        
        selected = strategy.select_worker(sample_workers, basic_context)
        
        assert selected is not None
        assert 'assigned_weight' in selected
        assert selected['assigned_weight'] > 0


class TestPerformanceStrategies:
    """Test performance-based routing strategies"""
    
    def test_fastest_response_no_history(self, sample_workers, basic_context):
        """Test fastest response strategy with no history"""
        strategy = FastestResponseStrategy()
        
        selected = strategy.select_worker(sample_workers, basic_context)
        
        assert selected is not None
        assert 'Fastest response (estimated, no history)' in selected['selection_reason']
    
    def test_fastest_response_with_history(self, sample_workers, basic_context):
        """Test fastest response strategy with performance history"""
        strategy = FastestResponseStrategy()
        
        # Record some performance history
        strategy.record_execution('worker-1', 2000.0)  # 2 seconds
        strategy.record_execution('worker-2', 1000.0)  # 1 second
        strategy.record_execution('worker-3', 3000.0)  # 3 seconds
        
        selected = strategy.select_worker(sample_workers, basic_context)
        
        assert selected is not None
        # worker-2 should be selected (fastest average)
        assert selected['worker_id'] == 'worker-2'
        assert 'avg: 1000ms' in selected['selection_reason']
    
    def test_state_aware_strategy(self, sample_workers, basic_context):
        """Test state-aware strategy"""
        strategy = StateAwareStrategy()
        
        selected = strategy.select_worker(sample_workers, basic_context)
        
        assert selected is not None
        assert 'State-aware selection' in selected['selection_reason']
        assert 'state_score' in selected
    
    def test_adaptive_strategy(self, sample_workers, basic_context):
        """Test adaptive strategy"""
        strategy = AdaptiveStrategy()
        
        selected = strategy.select_worker(sample_workers, basic_context)
        
        assert selected is not None
        assert 'Adaptive strategy using' in selected['selection_reason']
        assert 'adaptive_strategy' in selected
        assert 'adaptation_confidence' in selected


class TestSpecificWorkerStrategies:
    """Test specific worker routing strategies"""
    
    def test_specific_worker_success(self, sample_workers, basic_context):
        """Test specific worker strategy success"""
        strategy = SpecificWorkerStrategy('worker-2')
        
        selected = strategy.select_worker(sample_workers, basic_context)
        
        assert selected is not None
        assert selected['worker_id'] == 'worker-2'
        assert 'Specific worker requested: worker-2' in selected['selection_reason']
        assert selected['specific_worker_match'] is True
    
    def test_specific_worker_not_found(self, sample_workers, basic_context):
        """Test specific worker strategy when worker not found"""
        strategy = SpecificWorkerStrategy('nonexistent-worker')
        
        selected = strategy.select_worker(sample_workers, basic_context)
        
        assert selected is None  # No fallback configured
    
    def test_specific_worker_with_fallback(self, sample_workers, basic_context):
        """Test specific worker strategy with fallback"""
        fallback_strategy = LeastLoadedStrategy()
        strategy = SpecificWorkerStrategy(
            'nonexistent-worker',
            allow_fallback=True,
            fallback_strategy=fallback_strategy
        )
        
        selected = strategy.select_worker(sample_workers, basic_context)
        
        assert selected is not None
        assert selected['specific_worker_fallback'] is True
        assert selected['original_target'] == 'nonexistent-worker'
    
    def test_session_affinity_new_session(self, sample_workers, basic_context):
        """Test session affinity with new session"""
        strategy = SessionAffinityStrategy()
        basic_context.session_id = "new-session-123"
        
        selected = strategy.select_worker(sample_workers, basic_context)
        
        assert selected is not None
        assert selected['session_affinity'] is True
        assert selected['session_id'] == "new-session-123"
        assert selected['new_session_mapping'] is True
    
    def test_session_affinity_existing_session(self, sample_workers, basic_context):
        """Test session affinity with existing session mapping"""
        session_mapping = {"existing-session": "worker-2"}
        strategy = SessionAffinityStrategy(session_worker_map=session_mapping)
        basic_context.session_id = "existing-session"
        
        selected = strategy.select_worker(sample_workers, basic_context)
        
        assert selected is not None
        assert selected['worker_id'] == 'worker-2'
        assert selected['session_affinity'] is True
        assert 'new_session_mapping' not in selected
    
    def test_affinity_based_strategy(self, sample_workers, basic_context):
        """Test affinity-based strategy"""
        affinity_rules = {
            'tenant_id': {'test-tenant': 'worker-1'},
            'command_type': {'TestCommand': 'worker-2'}
        }
        strategy = AffinityBasedStrategy(affinity_rules=affinity_rules)
        basic_context.tenant_id = "test-tenant"
        
        selected = strategy.select_worker(sample_workers, basic_context)
        
        assert selected is not None
        assert 'affinity_score' in selected
        assert 'Affinity-based selection' in selected['selection_reason']


class TestStrategyScoring:
    """Test strategy scoring mechanisms"""
    
    def test_least_loaded_scoring(self, sample_workers, basic_context):
        """Test least loaded strategy scoring"""
        strategy = LeastLoadedStrategy()
        
        scores = strategy.score_workers(sample_workers, basic_context)
        
        assert len(scores) == 3
        # Scores should be inversely related to load
        worker_scores = {score.worker_id: score.score for score in scores}
        
        # worker-3 (load 0.1) should have highest score
        # worker-2 (load 0.6) should have lowest score
        assert worker_scores['worker-3'] > worker_scores['worker-1']
        assert worker_scores['worker-1'] > worker_scores['worker-2']
    
    def test_strategy_metadata(self, sample_workers, basic_context):
        """Test strategy metadata"""
        strategy = LeastLoadedStrategy()
        
        metadata = strategy.get_strategy_metadata()
        
        assert 'name' in metadata
        assert 'type' in metadata
        assert metadata['name'] == 'Least Loaded'
        assert metadata['type'] == 'LeastLoadedStrategy'


class TestCapabilityFiltering:
    """Test capability-based filtering in strategies"""
    
    def test_capability_requirements(self, sample_workers, basic_context):
        """Test strategies with capability requirements"""
        basic_context.required_capabilities = {'specialized_task'}
        strategy = LeastLoadedStrategy()
        
        # First filter workers by capabilities
        capable_workers = []
        for worker in sample_workers:
            worker_capabilities = set(worker.get('capabilities', []))
            if basic_context.required_capabilities.issubset(worker_capabilities):
                capable_workers.append(worker)
        
        selected = strategy.select_worker(capable_workers, basic_context)
        
        assert selected is not None
        assert selected['worker_id'] == 'worker-3'  # Only worker with specialized_task
    
    def test_no_capable_workers(self, sample_workers, basic_context):
        """Test when no workers have required capabilities"""
        basic_context.required_capabilities = {'nonexistent_capability'}
        strategy = LeastLoadedStrategy()
        
        # Filter workers (should result in empty list)
        capable_workers = []
        for worker in sample_workers:
            worker_capabilities = set(worker.get('capabilities', []))
            if basic_context.required_capabilities.issubset(worker_capabilities):
                capable_workers.append(worker)
        
        selected = strategy.select_worker(capable_workers, basic_context)
        
        assert selected is None  # No capable workers


class TestStrategyEdgeCases:
    """Test edge cases and error conditions"""
    
    def test_empty_worker_list(self, basic_context):
        """Test strategies with empty worker list"""
        strategy = LeastLoadedStrategy()
        
        selected = strategy.select_worker([], basic_context)
        
        assert selected is None
    
    def test_single_worker(self, basic_context):
        """Test strategies with single worker"""
        single_worker = [{
            'worker_id': 'only-worker',
            'state': 'READY',
            'current_load': 0.5,
            'capabilities': ['general']
        }]
        
        strategy = LeastLoadedStrategy()
        selected = strategy.select_worker(single_worker, basic_context)
        
        assert selected is not None
        assert selected['worker_id'] == 'only-worker'
    
    def test_all_workers_overloaded(self, basic_context):
        """Test strategies when all workers are overloaded"""
        overloaded_workers = [
            {
                'worker_id': f'worker-{i}',
                'state': 'READY',
                'current_load': 1.0,  # 100% load
                'capabilities': ['general']
            }
            for i in range(3)
        ]
        
        strategy = LeastLoadedStrategy()
        selected = strategy.select_worker(overloaded_workers, basic_context)
        
        # Should still select one (least loaded among overloaded)
        assert selected is not None


class TestStrategyIntegration:
    """Test strategy integration and composition"""
    
    def test_strategy_supports_context(self, basic_context):
        """Test strategy context support"""
        strategy = SpecificWorkerStrategy('test-worker')
        
        # Should support contexts with specific worker requirements
        basic_context.require_specific_worker = True
        assert strategy.supports_context(basic_context) is True
        
        # Should not support contexts without specific worker requirements
        basic_context.require_specific_worker = False
        basic_context.target_worker_id = None
        assert strategy.supports_context(basic_context) is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
