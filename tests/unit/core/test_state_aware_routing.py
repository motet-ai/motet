"""
Tests for State-Aware Routing System

Tests the intelligent routing logic that selects optimal workers
based on warm state availability and current load.
"""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta

import sys
from pathlib import Path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from motet.core.distributed.state_aware_routing import (
    StateAwareRouter,
    RoutingStrategy,
    WorkerCandidate,
    select_optimal_worker_with_state,
    initialize_state_aware_router,
    get_state_aware_router
)
from motet.core.distributed.state_registry import WorkerState, StateStatus
from motet.core.commands.distributed import DistributedCommand
from motet.core.commands.capabilities import WorkerCapability


@pytest.fixture
def mock_state_registry():
    """Mock state registry for testing. State registry methods are sync in production."""
    registry = MagicMock()
    registry.get_worker_states = MagicMock(return_value=[])
    registry.find_workers_with_state = MagicMock(return_value=[])
    return registry


@pytest.fixture
def state_aware_router(mock_state_registry):
    """Create StateAwareRouter with mocked dependencies."""
    return StateAwareRouter(
        state_registry=mock_state_registry,
        default_strategy=RoutingStrategy.HYBRID
    )


@pytest.fixture
def sample_workers():
    """Sample worker data for testing."""
    return [
        {
            "worker_id": "worker_1",
            "worker_pid": 123,
            "current_load": 0.3,
            "capabilities": ["model_inference", "tool_execution"]
        },
        {
            "worker_id": "worker_2", 
            "worker_pid": 124,
            "current_load": 0.6,
            "capabilities": ["memory_operations", "tool_execution"]
        },
        {
            "worker_id": "worker_3",
            "worker_pid": 125,
            "current_load": 0.2,
            "capabilities": ["model_inference", "reasoning", "tool_execution"]
        }
    ]


@pytest.fixture
def sample_worker_states():
    """Sample worker states for testing. Use expiry > 6 min so freshness (time_left/1800) exceeds threshold 0.2."""
    return [
        WorkerState(
            worker_id="worker_1",
            worker_pid=123,
            state_type="mcp_connection",
            status=StateStatus.ACTIVE,
            expires_at=datetime.utcnow() + timedelta(minutes=15),
            usage_count=5
        ),
        WorkerState(
            worker_id="worker_2",
            worker_pid=124,
            state_type="model_cache",
            status=StateStatus.ACTIVE,
            expires_at=datetime.utcnow() + timedelta(minutes=15),
            usage_count=2
        )
    ]


@pytest.fixture
def mock_distributed_command():
    """Mock distributed command for testing."""
    command = MagicMock(spec=DistributedCommand)
    command.get_command_type.return_value = "core.tool_execution"
    command.get_required_capabilities.return_value = [WorkerCapability.TOOL_EXECUTION]
    command.tool_name = "weather"
    return command


class TestWorkerCandidate:
    """Test WorkerCandidate functionality."""
    
    def test_worker_candidate_creation(self, sample_worker_states):
        """Test creating a WorkerCandidate."""
        candidate = WorkerCandidate(
            worker_id="worker_1",
            worker_pid=123,
            current_load=0.3,
            capabilities={"tool_execution", "model_inference"},
            warm_states=sample_worker_states[:1]
        )
        
        assert candidate.worker_id == "worker_1"
        assert candidate.worker_pid == 123
        assert candidate.current_load == 0.3
        assert len(candidate.warm_states) == 1
    
    def test_has_capability(self):
        """Test capability checking."""
        candidate = WorkerCandidate(
            worker_id="worker_1",
            worker_pid=123,
            current_load=0.3,
            capabilities={"tool_execution", "model_inference"},
            warm_states=[]
        )
        
        assert candidate.has_capability(WorkerCapability.TOOL_EXECUTION)
        assert candidate.has_capability(WorkerCapability.MODEL_INFERENCE)
        assert not candidate.has_capability(WorkerCapability.MEMORY_OPERATIONS)
    
    def test_has_state_type(self, sample_worker_states):
        """Test state type checking."""
        candidate = WorkerCandidate(
            worker_id="worker_1",
            worker_pid=123,
            current_load=0.3,
            capabilities=set(),
            warm_states=sample_worker_states
        )
        
        assert candidate.has_state_type("mcp_connection")
        assert candidate.has_state_type("model_cache")
        assert not candidate.has_state_type("database_pool")
    
    def test_get_state_freshness(self, sample_worker_states):
        """Test state freshness calculation."""
        candidate = WorkerCandidate(
            worker_id="worker_1",
            worker_pid=123,
            current_load=0.3,
            capabilities=set(),
            warm_states=sample_worker_states
        )
        
        # Should return freshness score for existing state
        freshness = candidate.get_state_freshness("mcp_connection")
        assert 0.0 <= freshness <= 1.0
        
        # Should return 0 for non-existent state
        freshness = candidate.get_state_freshness("nonexistent_state")
        assert freshness == 0.0


class TestStateAwareRouter:
    """Test StateAwareRouter functionality."""
    
    def test_router_initialization(self, mock_state_registry):
        """Test router initialization."""
        router = StateAwareRouter(
            state_registry=mock_state_registry,
            default_strategy=RoutingStrategy.STATE_AWARE
        )
        
        assert router.state_registry == mock_state_registry
        assert router.default_strategy == RoutingStrategy.STATE_AWARE
        assert router.state_weight == 0.7
        assert router.load_weight == 0.3
    
    def test_build_worker_candidates(self, state_aware_router, sample_workers, sample_worker_states):
        """Test building worker candidates with state information."""
        # Mock state registry responses (sync methods)
        state_aware_router.state_registry.get_worker_states.side_effect = [
            [sample_worker_states[0]],  # worker_1 has mcp_connection
            [sample_worker_states[1]],  # worker_2 has model_cache
            []  # worker_3 has no state
        ]
        
        candidates = state_aware_router._build_worker_candidates(sample_workers)
        
        assert len(candidates) == 3
        assert candidates[0].worker_id == "worker_1"
        assert len(candidates[0].warm_states) == 1
        assert candidates[0].warm_states[0].state_type == "mcp_connection"
        
        assert candidates[1].worker_id == "worker_2"
        assert len(candidates[1].warm_states) == 1
        assert candidates[1].warm_states[0].state_type == "model_cache"
        
        assert candidates[2].worker_id == "worker_3"
        assert len(candidates[2].warm_states) == 0
    
    def test_identify_beneficial_states(self, state_aware_router, mock_distributed_command):
        """Test identification of beneficial state types for commands."""
        # Test tool execution command
        mock_distributed_command.get_command_type.return_value = "core.tool_execution"
        beneficial_states = state_aware_router._identify_beneficial_states(mock_distributed_command)
        assert "mcp_connection" in beneficial_states
        
        # Test model inference command
        mock_distributed_command.get_command_type.return_value = "core.model_inference"
        beneficial_states = state_aware_router._identify_beneficial_states(mock_distributed_command)
        assert "model_cache" in beneficial_states
        
        # Test memory command
        mock_distributed_command.get_command_type.return_value = "core.memory_store"
        beneficial_states = state_aware_router._identify_beneficial_states(mock_distributed_command)
        assert "database_pool" in beneficial_states
    
    def test_calculate_state_affinity(self, state_aware_router, sample_worker_states):
        """Test state affinity score calculation."""
        candidate = WorkerCandidate(
            worker_id="worker_1",
            worker_pid=123,
            current_load=0.3,
            capabilities=set(),
            warm_states=sample_worker_states[:1]  # Has mcp_connection
        )
        
        # Test with matching beneficial state (score can exceed 1.0 due to freshness bonus)
        beneficial_states = ["mcp_connection"]
        score = state_aware_router._calculate_state_affinity(candidate, beneficial_states)
        assert score > 0.0
        
        # Test with non-matching beneficial state
        beneficial_states = ["database_pool"]
        score = state_aware_router._calculate_state_affinity(candidate, beneficial_states)
        assert score == 0.0
    
    def test_select_load_balanced(self, state_aware_router, sample_workers):
        """Test load-balanced worker selection."""
        candidates = [
            WorkerCandidate(
                worker_id=w["worker_id"],
                worker_pid=w["worker_pid"],
                current_load=w["current_load"],
                capabilities=set(w["capabilities"]),
                warm_states=[]
            )
            for w in sample_workers
        ]
        
        selected = state_aware_router._select_load_balanced(candidates)
        
        # Should select worker with lowest load (worker_3 with 0.2)
        assert selected.worker_id == "worker_3"
        assert selected.current_load == 0.2
        assert "load_balanced" in selected.selection_reason
    
    def test_select_state_aware(self, state_aware_router, sample_workers, sample_worker_states, mock_distributed_command):
        """Test state-aware worker selection."""
        # Build candidates with state
        candidates = []
        for i, worker in enumerate(sample_workers):
            warm_states = [sample_worker_states[i]] if i < len(sample_worker_states) else []
            candidate = WorkerCandidate(
                worker_id=worker["worker_id"],
                worker_pid=worker["worker_pid"],
                current_load=worker["current_load"],
                capabilities=set(worker["capabilities"]),
                warm_states=warm_states
            )
            candidates.append(candidate)
        
        # Mock beneficial states identification
        with patch.object(state_aware_router, '_identify_beneficial_states', return_value=["mcp_connection"]):
            selected = state_aware_router._select_state_aware(mock_distributed_command, candidates)
        
        # Should select worker_1 which has mcp_connection state
        assert selected.worker_id == "worker_1"
        assert "state_aware" in selected.selection_reason
    
    def test_select_hybrid(self, state_aware_router, sample_workers, sample_worker_states, mock_distributed_command):
        """Test hybrid worker selection strategy."""
        # Build candidates with state
        candidates = []
        for i, worker in enumerate(sample_workers):
            warm_states = [sample_worker_states[i]] if i < len(sample_worker_states) else []
            candidate = WorkerCandidate(
                worker_id=worker["worker_id"],
                worker_pid=worker["worker_pid"],
                current_load=worker["current_load"],
                capabilities=set(worker["capabilities"]),
                warm_states=warm_states
            )
            candidates.append(candidate)
        
        # Mock beneficial states identification
        with patch.object(state_aware_router, '_identify_beneficial_states', return_value=["mcp_connection"]):
            selected = state_aware_router._select_hybrid(mock_distributed_command, candidates)
        
        assert selected is not None
        assert hasattr(selected, 'total_score')
        assert "hybrid" in selected.selection_reason
    
    def test_select_optimal_worker_no_workers(self, state_aware_router, mock_distributed_command):
        """Test worker selection with no available workers."""
        result = state_aware_router.select_optimal_worker(
            mock_distributed_command, [], RoutingStrategy.HYBRID
        )
        
        assert result is None
    
    def test_select_optimal_worker_capability_filtering(self, state_aware_router, sample_workers, mock_distributed_command):
        """Test worker selection with capability filtering."""
        # Mock command that requires memory operations
        mock_distributed_command.get_required_capabilities.return_value = [WorkerCapability.MEMORY_OPERATIONS]
        
        # Mock state registry
        state_aware_router.state_registry.get_worker_states.return_value = []
        
        selected = state_aware_router.select_optimal_worker(
            mock_distributed_command, sample_workers, RoutingStrategy.LOAD_BALANCED
        )
        
        # Should select worker_2 which has memory_operations capability
        assert selected.worker_id == "worker_2"
    
    def test_routing_stats_update(self, state_aware_router):
        """Test routing statistics tracking."""
        initial_stats = state_aware_router.get_routing_stats()
        assert initial_stats["total_routes"] == 0
        
        # Simulate routing
        state_aware_router._update_routing_stats(RoutingStrategy.STATE_AWARE, 10.5)
        
        updated_stats = state_aware_router.get_routing_stats()
        assert updated_stats["total_routes"] == 1
        assert updated_stats["state_aware_routes"] == 1
        assert updated_stats["avg_routing_time_ms"] == 10.5
    
    def test_touch_worker_states(self, state_aware_router, sample_worker_states):
        """Test touching worker states after selection."""
        candidate = WorkerCandidate(
            worker_id="worker_1",
            worker_pid=123,
            current_load=0.3,
            capabilities=set(),
            warm_states=sample_worker_states[:1]
        )
        
        # Mock touch_worker_state function
        with patch('motet.core.distributed.state_aware_routing.touch_worker_state', return_value=True) as mock_touch:
            state_aware_router._touch_worker_states(candidate)
            
            # Should have called touch for the worker's state
            mock_touch.assert_called_once_with("worker_1", "mcp_connection")


class TestRoutingStrategies:
    """Test different routing strategies."""
    
    def test_round_robin_strategy(self, state_aware_router, sample_workers):
        """Test round robin routing strategy."""
        candidates = [
            WorkerCandidate(
                worker_id=w["worker_id"],
                worker_pid=w["worker_pid"],
                current_load=w["current_load"],
                capabilities=set(w["capabilities"]),
                warm_states=[]
            )
            for w in sample_workers
        ]
        
        selected = state_aware_router._select_round_robin(candidates)
        
        # Should select first candidate (simple implementation)
        assert selected.worker_id == "worker_1"
        assert "round_robin" in selected.selection_reason


class TestErrorHandling:
    """Test error handling in routing."""
    
    def test_state_registry_error_handling(self, state_aware_router, sample_workers, mock_distributed_command):
        """Test handling of state registry errors."""
        # Mock state registry to raise exception
        state_aware_router.state_registry.get_worker_states.side_effect = Exception("Redis error")
        
        # Should still work with fallback
        selected = state_aware_router.select_optimal_worker(
            mock_distributed_command, sample_workers, RoutingStrategy.HYBRID
        )
        
        assert selected is not None
        # Should fall back to load balancing
        assert selected.worker_id == "worker_3"  # Lowest load
    
    def test_routing_with_no_beneficial_states(self, state_aware_router, sample_workers, mock_distributed_command):
        """Test routing when no beneficial states are identified."""
        # Mock empty beneficial states
        with patch.object(state_aware_router, '_identify_beneficial_states', return_value=[]):
            # Mock state registry
            state_aware_router.state_registry.get_worker_states.return_value = []
            
            selected = state_aware_router.select_optimal_worker(
                mock_distributed_command, sample_workers, RoutingStrategy.STATE_AWARE
            )
        
        # Should fall back to load balancing
        assert selected is not None
        assert selected.worker_id == "worker_3"  # Lowest load


class TestGlobalFunctions:
    """Test global convenience functions."""
    
    def test_initialize_state_aware_router(self, mock_state_registry):
        """Test initializing global state-aware router."""
        initialize_state_aware_router(mock_state_registry, RoutingStrategy.STATE_AWARE)
        
        router = get_state_aware_router()
        assert router is not None
        assert router.state_registry == mock_state_registry
        assert router.default_strategy == RoutingStrategy.STATE_AWARE
    
    def test_select_optimal_worker_with_state_convenience(self, mock_state_registry, sample_workers, mock_distributed_command):
        """Test convenience function for worker selection."""
        # Initialize global router
        initialize_state_aware_router(mock_state_registry, RoutingStrategy.LOAD_BALANCED)
        
        # Mock state registry
        mock_state_registry.get_worker_states.return_value = []
        
        result = select_optimal_worker_with_state(
            mock_distributed_command, sample_workers, RoutingStrategy.LOAD_BALANCED
        )
        
        assert result is not None
        assert result.worker_id == "worker_3"  # Lowest load
    
    def test_select_optimal_worker_fallback(self, sample_workers, mock_distributed_command):
        """Test fallback behavior when no global router is available."""
        # Clear global router
        with patch('motet.core.distributed.state_aware_routing.global_state_aware_router', None):
            result = select_optimal_worker_with_state(
                mock_distributed_command, sample_workers
            )
        
        # Should fall back to simple load balancing
        assert result is not None
        assert result.worker_id == "worker_3"  # Lowest load
        assert "fallback_no_router" in result.selection_reason


class TestPerformanceOptimization:
    """Test performance aspects of routing."""
    
    def test_routing_performance_tracking(self, state_aware_router, sample_workers, mock_distributed_command):
        """Test that routing performance is tracked."""
        # Mock state registry
        state_aware_router.state_registry.get_worker_states.return_value = []
        
        # Execute routing
        state_aware_router.select_optimal_worker(
            mock_distributed_command, sample_workers, RoutingStrategy.LOAD_BALANCED
        )
        
        stats = state_aware_router.get_routing_stats()
        assert stats["total_routes"] == 1
        assert stats["avg_routing_time_ms"] > 0
    
    def test_routing_stats_calculation(self, state_aware_router):
        """Test routing statistics calculation."""
        # Simulate multiple routes
        state_aware_router.routing_stats["total_routes"] = 10
        state_aware_router.routing_stats["state_aware_routes"] = 7
        state_aware_router.routing_stats["load_balanced_routes"] = 3
        
        stats = state_aware_router.get_routing_stats()
        
        assert stats["state_aware_percentage"] == 70.0
        assert stats["load_balanced_percentage"] == 30.0


if __name__ == "__main__":
    pytest.main([__file__])
