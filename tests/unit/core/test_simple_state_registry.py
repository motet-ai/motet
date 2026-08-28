"""
Simple tests for state registry without circular imports.

These tests verify the core functionality of the state management system
without importing the full AI stack. Updated to reflect current architecture
where execution tracking is optional and commands use _execute_impl.
"""

import pytest
import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock
import json

# Import only the specific modules we need
import sys
from pathlib import Path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from motet.core.distributed.state_registry import (
    StateTypeDefinition,
    WorkerState,
    StateStatus,
    WarmStateTypeRegistry,
    EphemeralStateRegistry
)


class TestStateTypeDefinition:
    """Test StateTypeDefinition without external dependencies."""
    
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
    
    def test_worker_state_creation(self):
        """Test creating a WorkerState."""
        state = WorkerState(
            worker_id="test_worker_1",
            worker_pid=12345,
            state_type="mcp_connection",
            status=StateStatus.ACTIVE,
            expires_at=datetime.utcnow() + timedelta(minutes=5),
            metadata={"server_id": "weather", "tools_count": 3},
            reproduction_cost_ms=150
        )
        
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
    
    def test_worker_state_serialization(self):
        """Test WorkerState serialization and deserialization."""
        state = WorkerState(
            worker_id="test_worker_1",
            worker_pid=12345,
            state_type="mcp_connection",
            status=StateStatus.ACTIVE,
            expires_at=datetime.utcnow() + timedelta(minutes=5),
            metadata={"server_id": "weather", "tools_count": 3},
            reproduction_cost_ms=150
        )
        
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


class TestEphemeralStateRegistry:
    """Test EphemeralStateRegistry core functionality."""
    
    @pytest.fixture
    def mock_redis(self):
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
    def state_registry(self, mock_redis):
        """Create EphemeralStateRegistry with mocked Redis."""
        return EphemeralStateRegistry(mock_redis, "test:worker_state")

    def test_register_worker_state(self, state_registry, mock_redis):
        """Test registering worker state."""
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

    def test_get_worker_state(self, state_registry, mock_redis):
        """Test retrieving worker state."""
        # Create sample state
        sample_state = WorkerState(
            worker_id="test_worker_1",
            worker_pid=12345,
            state_type="mcp_connection",
            status=StateStatus.ACTIVE,
            expires_at=datetime.utcnow() + timedelta(minutes=5),
            metadata={"server_id": "weather", "tools_count": 3},
            reproduction_cost_ms=150
        )

        # Mock Redis response
        state_data = json.dumps(sample_state.to_dict())
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
