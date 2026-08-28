"""
Tests for the Ephemeral Worker State Registry

Tests the Redis-based state tracking system that enables intelligent
routing based on worker warm state.
"""

import itertools
import json
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import sys
from pathlib import Path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from motet.core.distributed.state_registry import (
    EphemeralStateRegistry,
    WarmStateTypeRegistry,
    StateTypeDefinition,
    WorkerState,
    StateStatus,
    initialize_state_registry,
    get_state_registry,
    register_worker_state,
    find_workers_with_state,
    touch_worker_state
)


@pytest.fixture
def mock_redis():
    """Mock Redis client for testing (sync - EphemeralStateRegistry uses sync Redis)."""
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
def state_registry(mock_redis):
    """Create EphemeralStateRegistry with mocked Redis."""
    return EphemeralStateRegistry(mock_redis, "test:worker_state")


@pytest.fixture
def sample_worker_state():
    """Create a sample WorkerState for testing."""
    return WorkerState(
        worker_id="test_worker_1",
        worker_pid=12345,
        state_type="mcp_connection",
        status=StateStatus.ACTIVE,
        expires_at=datetime.utcnow() + timedelta(minutes=5),
        metadata={"server_id": "weather", "tools_count": 3},
        reproduction_cost_ms=150
    )


class TestStateTypeDefinition:
    """Test StateTypeDefinition functionality."""
    
    def test_state_type_creation(self):
        """Test creating a state type definition."""
        state_def = StateTypeDefinition(
            name="test_state",
            default_ttl_seconds=300,
            reproduction_cost_ms=100,
            routing_weight=0.8
        )
        
        assert state_def.name == "test_state"
        assert state_def.default_ttl_seconds == 300
        assert state_def.reproduction_cost_ms == 100
        assert state_def.routing_weight == 0.8
    
    def test_state_type_to_dict(self):
        """Test serialization of state type definition."""
        state_def = StateTypeDefinition(
            name="mcp_connection",
            default_ttl_seconds=300,
            reproduction_cost_ms=150,
            routing_weight=0.8
        )
        
        result = state_def.to_dict()
        expected = {
            "name": "mcp_connection",
            "default_ttl_seconds": 300,
            "reproduction_cost_ms": 150,
            "routing_weight": 0.8
        }
        
        assert result == expected


class TestWorkerState:
    """Test WorkerState functionality."""
    
    def test_worker_state_creation(self, sample_worker_state):
        """Test creating a WorkerState."""
        state = sample_worker_state
        
        assert state.worker_id == "test_worker_1"
        assert state.worker_pid == 12345
        assert state.state_type == "mcp_connection"
        assert state.status == StateStatus.ACTIVE
        assert not state.is_expired
    

    def test_worker_state_expiry(self):
        """Test worker state expiry logic."""
        # Create expired state
        expired_state = WorkerState(
            worker_id="test_worker",
            worker_pid=123,
            state_type="test_state",
            expires_at=datetime.utcnow() - timedelta(minutes=1)
        )
        
        assert expired_state.is_expired
        
        # Create non-expiring state
        no_expiry_state = WorkerState(
            worker_id="test_worker",
            worker_pid=123,
            state_type="test_state"
        )
        
        assert not no_expiry_state.is_expired
    
    def test_worker_state_age(self, sample_worker_state):
        """Test worker state age calculation."""
        state = sample_worker_state
        age = state.age_seconds
        
        assert age >= 0
        assert age < 1  # Should be very recent
    
    def test_worker_state_touch(self, sample_worker_state):
        """Test updating worker state usage."""
        state = sample_worker_state
        initial_usage = state.usage_count
        initial_time = state.last_used_at
        
        # Small delay to ensure timestamp difference
        import time
        time.sleep(0.01)
        
        state.touch()
        
        assert state.usage_count == initial_usage + 1
        assert state.last_used_at > initial_time
    
    def test_worker_state_serialization(self, sample_worker_state):
        """Test WorkerState serialization and deserialization."""
        state = sample_worker_state
        
        # Serialize to dict
        state_dict = state.to_dict()
        
        # Verify required fields
        assert "worker_id" in state_dict
        assert "worker_pid" in state_dict
        assert "state_type" in state_dict
        assert "status" in state_dict
        
        # Deserialize from dict
        restored_state = WorkerState.from_dict(state_dict)
        
        assert restored_state.worker_id == state.worker_id
        assert restored_state.worker_pid == state.worker_pid
        assert restored_state.state_type == state.state_type
        assert restored_state.status == state.status


class TestWarmStateTypeRegistry:
    """Test WarmStateTypeRegistry functionality."""
    
    def test_registry_initialization(self):
        """Test that registry initializes with built-in types."""
        registry = WarmStateTypeRegistry()
        
        # Should have built-in types
        assert "mcp_connection" in registry.state_types
        assert "model_cache" in registry.state_types
        assert "database_pool" in registry.state_types
        assert "websocket_connection" in registry.state_types
    
    def test_register_custom_state_type(self):
        """Test registering a custom state type."""
        registry = WarmStateTypeRegistry()
        
        custom_state = StateTypeDefinition(
            name="custom_cache",
            default_ttl_seconds=600,
            reproduction_cost_ms=200,
            routing_weight=0.7
        )
        
        registry.register_state_type(custom_state)
        
        assert "custom_cache" in registry.state_types
        retrieved = registry.get_state_type("custom_cache")
        assert retrieved == custom_state
    
    def test_list_state_types(self):
        """Test listing all registered state types."""
        registry = WarmStateTypeRegistry()
        
        state_types = registry.list_state_types()
        
        assert len(state_types) >= 4  # At least the built-in types
        assert all(isinstance(st, StateTypeDefinition) for st in state_types)


class TestEphemeralStateRegistry:
    """Test EphemeralStateRegistry functionality (sync API)."""

    def test_register_worker_state(self, state_registry, mock_redis):
        """Test registering worker state."""
        # Mock Redis responses
        mock_redis.setex.return_value = True
        mock_redis.sadd.return_value = 1
        mock_redis.expire.return_value = True

        # Register state (sync)
        worker_state = state_registry.register_worker_state(
            worker_id="test_worker",
            worker_pid=12345,
            state_type="mcp_connection",
            ttl_seconds=300,
            metadata={"server_id": "weather"}
        )

        # Verify state object
        assert worker_state.worker_id == "test_worker"
        assert worker_state.worker_pid == 12345
        assert worker_state.state_type == "mcp_connection"
        assert worker_state.metadata["server_id"] == "weather"

        # Verify Redis calls
        mock_redis.setex.assert_called_once()
        mock_redis.sadd.assert_called_once()
        mock_redis.expire.assert_called_once()

    def test_register_unknown_state_type(self, state_registry):
        """Test registering state with unknown type raises error."""
        with pytest.raises(ValueError, match="Unknown state type"):
            state_registry.register_worker_state(
                worker_id="test_worker",
                worker_pid=12345,
                state_type="unknown_state_type"
            )

    def test_get_worker_state(self, state_registry, mock_redis, sample_worker_state):
        """Test retrieving worker state."""
        # Mock Redis response
        state_data = json.dumps(sample_worker_state.to_dict())
        mock_redis.get.return_value = state_data

        # Get state (sync)
        retrieved_state = state_registry.get_worker_state(
            "test_worker_1", "mcp_connection"
        )

        assert retrieved_state is not None
        assert retrieved_state.worker_id == "test_worker_1"
        assert retrieved_state.state_type == "mcp_connection"

        # Verify Redis call
        mock_redis.get.assert_called_once_with("test:worker_state:test_worker_1:mcp_connection")

    def test_get_nonexistent_worker_state(self, state_registry, mock_redis):
        """Test retrieving non-existent worker state returns None."""
        mock_redis.get.return_value = None

        result = state_registry.get_worker_state("nonexistent", "mcp_connection")

        assert result is None

    def test_get_expired_worker_state(self, state_registry, mock_redis):
        """Test retrieving expired worker state returns None and cleans up."""
        # Create expired state (need reproduction_cost_ms for model)
        expired_state = WorkerState(
            worker_id="test_worker",
            worker_pid=123,
            state_type="mcp_connection",
            expires_at=datetime.utcnow() - timedelta(minutes=1),
            reproduction_cost_ms=100
        )
        # First get returns expired state; remove_worker_state calls get again, return None
        mock_redis.get.side_effect = [json.dumps(expired_state.to_dict()), None]

        result = state_registry.get_worker_state("test_worker", "mcp_connection")

        assert result is None
        # Should have cleaned up expired state
        mock_redis.delete.assert_called_once()

    def test_touch_worker_state(self, state_registry, mock_redis, sample_worker_state):
        """Test updating worker state last used timestamp."""
        # Mock Redis responses
        state_data = json.dumps(sample_worker_state.to_dict())
        mock_redis.get.return_value = state_data
        mock_redis.ttl.return_value = 300  # 5 minutes remaining

        # Touch state (sync)
        result = state_registry.touch_worker_state("test_worker_1", "mcp_connection")

        assert result is True
        mock_redis.setex.assert_called_once()

    def test_find_workers_with_state(self, state_registry, mock_redis, sample_worker_state):
        """Test finding workers with specific state type."""
        # Mock Redis responses - smembers returns set of bytes
        mock_redis.smembers.return_value = {b"test_worker_1:12345", b"test_worker_2:12346"}

        # Mock get_worker_state calls
        state_data = json.dumps(sample_worker_state.to_dict())
        mock_redis.get.return_value = state_data

        # Find workers (sync)
        workers = state_registry.find_workers_with_state("mcp_connection", limit=10)

        assert len(workers) == 2
        assert all(ws.state_type == "mcp_connection" for ws in workers)

    def test_cleanup_expired_states(self, state_registry, mock_redis):
        """Test cleanup of expired worker states."""
        # Only one state type has worker refs so get is called a bounded number of times
        mock_redis.smembers.side_effect = [
            {b"test_worker_1:123", b"test_worker_2:124"},
            set(), set(), set(),
        ]

        expired_state = WorkerState(
            worker_id="test_worker_1",
            worker_pid=123,
            state_type="mcp_connection",
            expires_at=datetime.utcnow() - timedelta(minutes=1),
            reproduction_cost_ms=100
        )
        expired_json = json.dumps(expired_state.to_dict())
        # Cycle: each worker get returns expired then None (from remove_worker_state's get)
        mock_redis.get.side_effect = itertools.cycle([expired_json, None])

        cleaned_count = state_registry.cleanup_expired_states()

        assert cleaned_count > 0

    def test_get_registry_stats(self, state_registry, mock_redis, sample_worker_state):
        """Test getting registry statistics."""
        # Mock Redis responses - smembers returns set
        mock_redis.smembers.return_value = {b"test_worker_1:12345"}
        state_data = json.dumps(sample_worker_state.to_dict())
        mock_redis.get.return_value = state_data

        # Get stats (sync)
        stats = state_registry.get_registry_stats()

        assert "total_workers" in stats
        assert "total_states" in stats
        assert "state_types" in stats
        assert "registry_health" in stats


class TestGlobalFunctions:
    """Test global convenience functions."""
    
    def test_initialize_state_registry(self):
        """Test initializing global state registry."""
        from motet.core.distributed import state_registry as state_registry_module
        mock_redis = MagicMock()
        # Ensure we create a fresh registry with test prefix (global may already be set)
        with patch.object(state_registry_module, "global_state_registry", None):
            initialize_state_registry(mock_redis, "test:prefix")
            registry = get_state_registry()
            assert registry is not None
            assert registry.key_prefix == "test:prefix"
    
    def test_register_worker_state_convenience(self):
        """Test convenience function for registering worker state."""
        mock_registry = MagicMock()

        with patch('motet.core.distributed.state_registry.global_state_registry', mock_registry):
            mock_registry.register_worker_state.return_value = MagicMock()

            result = register_worker_state(
                worker_id="test_worker",
                worker_pid=123,
                state_type="mcp_connection"
            )

            assert result is not None
            mock_registry.register_worker_state.assert_called_once()

    def test_find_workers_with_state_convenience(self):
        """Test convenience function for finding workers with state."""
        mock_registry = MagicMock()

        with patch('motet.core.distributed.state_registry.global_state_registry', mock_registry):
            mock_registry.find_workers_with_state.return_value = []

            result = find_workers_with_state("mcp_connection")

            assert result == []
            mock_registry.find_workers_with_state.assert_called_once_with("mcp_connection", None)

    def test_touch_worker_state_convenience(self):
        """Test convenience function for touching worker state."""
        mock_registry = MagicMock()

        with patch('motet.core.distributed.state_registry.global_state_registry', mock_registry):
            mock_registry.touch_worker_state.return_value = True

            result = touch_worker_state("test_worker", "mcp_connection")

            assert result is True
            mock_registry.touch_worker_state.assert_called_once()


class TestErrorHandling:
    """Test error handling scenarios."""

    def test_corrupted_state_data(self, state_registry, mock_redis):
        """Test handling of corrupted state data in Redis."""
        # First get returns invalid data; remove_worker_state calls get again, return None
        mock_redis.get.side_effect = ["invalid json data", None]

        result = state_registry.get_worker_state("test_worker", "mcp_connection")

        assert result is None
        # Should have cleaned up corrupted data
        mock_redis.delete.assert_called_once()

    def test_redis_connection_failure(self, mock_redis):
        """Test handling of Redis connection failures."""
        mock_redis.get.side_effect = Exception("Redis connection failed")

        registry = EphemeralStateRegistry(mock_redis, "test:prefix")

        # Implementation may return None on Redis errors or let exception propagate
        try:
            result = registry.get_worker_state("test_worker", "mcp_connection")
            assert result is None
        except Exception as e:
            assert "Redis connection failed" in str(e)

    def test_invalid_worker_reference(self, state_registry, mock_redis):
        """Test handling of invalid worker references in index."""
        # Mock invalid worker reference format - smembers returns set of bytes
        mock_redis.smembers.return_value = {b"invalid_format", b"test_worker:123"}
        # Mock get for the valid key
        mock_redis.get.return_value = None

        workers = state_registry.find_workers_with_state("mcp_connection")

        # Should have cleaned up invalid reference or returned partial results
        mock_redis.srem.assert_called()


if __name__ == "__main__":
    pytest.main([__file__])
