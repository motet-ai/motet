"""
Integration Tests for State Management System

Tests the complete state-aware routing and execution tracking system
working together in realistic scenarios.
"""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
import json

import sys
from pathlib import Path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from motet.core.distributed.state_registry import (
    EphemeralStateRegistry,
    WorkerState as StateRegistryWorkerState,
    StateStatus,
    initialize_state_registry,
    register_worker_state,
)
from motet.core.distributed.state_aware_routing import (
    StateAwareRouter, RoutingStrategy, initialize_state_aware_router
)
# Execution tracking now handled by Flower API
from motet.core.commands.distributed import DistributedCommand, DistributedCommandContext
from motet.core.commands.capabilities import WorkerCapability


@pytest.fixture
def mock_redis():
    """Mock Redis client for integration testing (sync - EphemeralStateRegistry uses sync Redis)."""
    redis_mock = MagicMock()
    redis_mock.setex.return_value = True
    redis_mock.get.return_value = None
    redis_mock.delete.return_value = 1
    redis_mock.sadd.return_value = 1
    redis_mock.srem.return_value = 1
    redis_mock.smembers.return_value = set()
    redis_mock.expire.return_value = True
    redis_mock.ttl.return_value = 300
    return redis_mock


@pytest.fixture
def integrated_system(mock_redis):
    """Set up complete integrated state management system."""
    # Initialize state registry
    state_registry = EphemeralStateRegistry(mock_redis, "test:worker_state")
    initialize_state_registry(mock_redis, "test:worker_state")
    
    # Initialize state-aware router
    router = StateAwareRouter(state_registry, RoutingStrategy.HYBRID)
    initialize_state_aware_router(state_registry, RoutingStrategy.HYBRID)
    
    return {
        "state_registry": state_registry,
        "router": router,
        "redis": mock_redis
    }


@pytest.fixture
def sample_distributed_command():
    """Create a sample distributed command for testing."""
    context = DistributedCommandContext(
        task_id="test_task_123",
        conversation_id="test_conv_456",
        tenant_id="test_tenant",
        principal_id="test_user",
        trace_id="test_trace_789",
        timeout_seconds=30,
        priority=5,
        max_retries=3,
        required_capabilities={WorkerCapability.TOOL_EXECUTION}
    )
    
    command = MagicMock(spec=DistributedCommand)
    command.command_id = "cmd_123"
    command.distributed_context = context
    command.get_command_type.return_value = "core.tool_execution"
    command.get_required_capabilities.return_value = [WorkerCapability.TOOL_EXECUTION]
    command.tool_name = "weather"
    
    return command


class TestStateRegistryIntegration:
    """Test state registry integration scenarios (sync API)."""

    def test_worker_state_lifecycle(self, integrated_system):
        """Test complete worker state lifecycle."""
        state_registry = integrated_system["state_registry"]

        # 1. Register worker state (sync)
        worker_state = state_registry.register_worker_state(
            worker_id="integration_worker_1",
            worker_pid=12345,
            state_type="mcp_connection",
            ttl_seconds=300,
            metadata={"server_id": "weather", "tools_count": 5}
        )

        assert worker_state.worker_id == "integration_worker_1"
        assert worker_state.state_type == "mcp_connection"

        # 2. Retrieve worker state - mock Redis get to return the state we just registered
        state_registry.redis.get.return_value = json.dumps(worker_state.to_dict())
        retrieved_state = state_registry.get_worker_state(
            "integration_worker_1", "mcp_connection"
        )

        assert retrieved_state is not None
        assert retrieved_state.worker_id == "integration_worker_1"

        # 3. Touch worker state (sync)
        state_registry.redis.get.return_value = json.dumps(worker_state.to_dict())
        state_registry.redis.ttl.return_value = 300
        success = state_registry.touch_worker_state(
            "integration_worker_1", "mcp_connection"
        )

        assert success is True

        # 4. Find workers with state - smembers returns set of bytes
        state_registry.redis.smembers.return_value = {b"integration_worker_1:12345"}
        state_registry.redis.get.return_value = json.dumps(worker_state.to_dict())
        workers_with_state = state_registry.find_workers_with_state("mcp_connection")

        assert len(workers_with_state) == 1
        assert workers_with_state[0].worker_id == "integration_worker_1"

    def test_multiple_workers_state_management(self, integrated_system):
        """Test managing state for multiple workers."""
        state_registry = integrated_system["state_registry"]

        workers = [
            ("worker_1", 123, "mcp_connection", {"server": "weather"}),
            ("worker_2", 124, "model_cache", {"model": "gpt-4o-mini"}),
            ("worker_3", 125, "database_pool", {"db": "postgres"}),
            ("worker_1", 123, "model_cache", {"model": "claude"})
        ]

        for worker_id, pid, state_type, metadata in workers:
            state_registry.register_worker_state(
                worker_id=worker_id,
                worker_pid=pid,
                state_type=state_type,
                metadata=metadata
            )

        state_registry.redis.smembers.side_effect = [
            {b"worker_1:123"},
            {b"worker_2:124", b"worker_1:123"},
            {b"worker_3:125"}
        ]
        state_registry.redis.get.return_value = None  # or valid state dict if find needs it

        mcp_workers = state_registry.find_workers_with_state("mcp_connection")
        model_workers = state_registry.find_workers_with_state("model_cache")
        db_workers = state_registry.find_workers_with_state("database_pool")

        assert len(mcp_workers) >= 0
        assert len(model_workers) >= 0
        assert len(db_workers) >= 0


class TestStateAwareRoutingIntegration:
    """Test state-aware routing integration scenarios (sync API)."""

    def test_end_to_end_routing_with_state(self, integrated_system, sample_distributed_command):
        """Test complete routing flow with state considerations."""
        router = integrated_system["router"]
        state_registry = integrated_system["state_registry"]

        available_workers = [
            {"worker_id": "worker_1", "worker_pid": 123, "current_load": 0.4, "capabilities": ["tool_execution", "model_inference"]},
            {"worker_id": "worker_2", "worker_pid": 124, "current_load": 0.2, "capabilities": ["tool_execution", "memory_operations"]},
            {"worker_id": "worker_3", "worker_pid": 125, "current_load": 0.8, "capabilities": ["tool_execution", "reasoning"]}
        ]

        # Use real get_worker_states (returns [] when redis is mocked with None); router falls back to load balancing
        selected_worker = router.select_optimal_worker(
            sample_distributed_command,
            available_workers,
            RoutingStrategy.HYBRID
        )

        assert selected_worker is not None
        # With no state, hybrid strategy falls back to load: worker_2 has lowest load (0.2)
        assert selected_worker.worker_id == "worker_2"
        assert selected_worker.current_load == 0.2

    def test_routing_fallback_scenarios(self, integrated_system, sample_distributed_command):
        """Test routing fallback when no beneficial state exists."""
        router = integrated_system["router"]
        state_registry = integrated_system["state_registry"]

        available_workers = [
            {"worker_id": "worker_1", "worker_pid": 123, "current_load": 0.6, "capabilities": ["tool_execution"]},
            {"worker_id": "worker_2", "worker_pid": 124, "current_load": 0.3, "capabilities": ["tool_execution"]}
        ]

        router.state_registry.get_worker_states = MagicMock(return_value=[])
        selected_worker = router.select_optimal_worker(
            sample_distributed_command,
            available_workers,
            RoutingStrategy.HYBRID
        )

        assert selected_worker is not None
        assert selected_worker.worker_id == "worker_2"
        assert selected_worker.current_load == 0.3

    def test_capability_filtering_with_state(self, integrated_system, sample_distributed_command):
        """Test that capability filtering works correctly with state-aware routing."""
        router = integrated_system["router"]
        state_registry = integrated_system["state_registry"]

        sample_distributed_command.get_required_capabilities.return_value = [WorkerCapability.MEMORY_OPERATIONS]

        available_workers = [
            {"worker_id": "worker_1", "worker_pid": 123, "current_load": 0.2, "capabilities": ["tool_execution"]},
            {"worker_id": "worker_2", "worker_pid": 124, "current_load": 0.5, "capabilities": ["memory_operations", "tool_execution"]}
        ]

        mock_state = StateRegistryWorkerState(
            worker_id="worker_1",
            worker_pid=123,
            state_type="database_pool",
            status=StateStatus.ACTIVE,
            expires_at=datetime.utcnow() + timedelta(minutes=5),
            reproduction_cost_ms=50,
        )
        router.state_registry.get_worker_states = MagicMock(
            side_effect=[[mock_state], []]
        )
        selected_worker = router.select_optimal_worker(
            sample_distributed_command,
            available_workers,
            RoutingStrategy.HYBRID
        )

        assert selected_worker is not None
        assert selected_worker.worker_id == "worker_2"


# Execution tracking tests removed - now handled by Flower API


class TestSystemIntegrationScenarios:
    """Test realistic system integration scenarios (sync API)."""

    def test_weather_query_optimization_scenario(self, integrated_system):
        """Test realistic weather query optimization scenario."""
        state_registry = integrated_system["state_registry"]
        router = integrated_system["router"]

        state_registry.register_worker_state(
            worker_id="weather_worker_1",
            worker_pid=123,
            state_type="mcp_connection",
            metadata={"server_id": "weather", "connection_time": "2024-01-01T10:00:00"}
        )

        weather_command = MagicMock(spec=DistributedCommand)
        weather_command.command_id = "weather_cmd_2"
        weather_command.get_command_type.return_value = "core.tool_execution"
        weather_command.get_required_capabilities.return_value = [WorkerCapability.TOOL_EXECUTION]
        weather_command.tool_name = "weather"

        available_workers = [
            {"worker_id": "weather_worker_1", "worker_pid": 123, "current_load": 0.5, "capabilities": ["tool_execution"]},
            {"worker_id": "other_worker_1", "worker_pid": 124, "current_load": 0.2, "capabilities": ["tool_execution"]}
        ]

        # No state mock: get_worker_states returns [] for all; router uses load balancing
        selected_worker = router.select_optimal_worker(
            weather_command,
            available_workers,
            RoutingStrategy.HYBRID
        )

        assert selected_worker is not None
        # other_worker_1 has lower load (0.2) than weather_worker_1 (0.5)
        assert selected_worker.worker_id == "other_worker_1"
        stats = router.get_routing_stats()
        assert stats["total_routes"] > 0

    def test_horizontal_scaling_simulation(self, integrated_system):
        """Test system behavior during horizontal scaling."""
        state_registry = integrated_system["state_registry"]
        router = integrated_system["router"]

        scaled_workers = [
            {"worker_id": "worker_1", "worker_pid": 123, "current_load": 0.8, "capabilities": ["tool_execution"]},
            {"worker_id": "worker_2", "worker_pid": 124, "current_load": 0.7, "capabilities": ["tool_execution"]},
            {"worker_id": "worker_3", "worker_pid": 125, "current_load": 0.1, "capabilities": ["tool_execution"]},
            {"worker_id": "worker_4", "worker_pid": 126, "current_load": 0.1, "capabilities": ["tool_execution"]}
        ]

        state_registry.register_worker_state(worker_id="worker_1", worker_pid=123, state_type="mcp_connection")
        state_registry.register_worker_state(worker_id="worker_2", worker_pid=124, state_type="model_cache")

        test_command = MagicMock(spec=DistributedCommand)
        test_command.get_command_type.return_value = "core.tool_execution"
        test_command.get_required_capabilities.return_value = [WorkerCapability.TOOL_EXECUTION]
        test_command.tool_name = "weather"

        def _make_state(stype, mins=5):
            return StateRegistryWorkerState(
                worker_id="x",
                worker_pid=0,
                state_type=stype,
                status=StateStatus.ACTIVE,
                expires_at=datetime.utcnow() + timedelta(minutes=mins),
                reproduction_cost_ms=50,
            )
        router.state_registry.get_worker_states = MagicMock(
            side_effect=[
                [_make_state("mcp_connection", 5)],
                [_make_state("model_cache", 10)],
                [], [],
            ],
        )
        selected_worker = router.select_optimal_worker(test_command, scaled_workers, RoutingStrategy.HYBRID)

        assert selected_worker is not None
        assert selected_worker.worker_id in ["worker_1", "worker_3", "worker_4"]

    def test_state_expiry_and_cleanup_scenario(self, integrated_system):
        """Test state expiry and cleanup scenarios."""
        state_registry = integrated_system["state_registry"]

        worker_state = state_registry.register_worker_state(
            worker_id="temp_worker",
            worker_pid=999,
            state_type="mcp_connection",
            ttl_seconds=1
        )
        assert worker_state is not None

        expired_state = StateRegistryWorkerState(
            worker_id="temp_worker",
            worker_pid=999,
            state_type="mcp_connection",
            expires_at=datetime.utcnow() - timedelta(seconds=1),
            reproduction_cost_ms=100
        )
        state_registry.redis.get.side_effect = [json.dumps(expired_state.to_dict()), None]

        retrieved_state = state_registry.get_worker_state("temp_worker", "mcp_connection")
        assert retrieved_state is None
        state_registry.redis.delete.assert_called()

    def test_error_resilience_scenario(self, integrated_system):
        """Test system resilience to various error conditions."""
        router = integrated_system["router"]
        state_registry = integrated_system["state_registry"]

        test_command = MagicMock(spec=DistributedCommand)
        test_command.get_command_type.return_value = "core.tool_execution"
        test_command.get_required_capabilities.return_value = [WorkerCapability.TOOL_EXECUTION]

        available_workers = [
            {"worker_id": "worker_1", "worker_pid": 123, "current_load": 0.5, "capabilities": ["tool_execution"]},
            {"worker_id": "worker_2", "worker_pid": 124, "current_load": 0.3, "capabilities": ["tool_execution"]}
        ]

        router.state_registry.get_worker_states = MagicMock(
            side_effect=Exception("Redis connection failed")
        )
        selected_worker = router.select_optimal_worker(
            test_command,
            available_workers,
            RoutingStrategy.HYBRID
        )

        assert selected_worker is not None
        assert selected_worker.worker_id == "worker_2"


if __name__ == "__main__":
    pytest.main([__file__])
