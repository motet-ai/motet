"""
Unit tests for distributed infrastructure components.

Tests the core distributed system components including state-aware routing,
command invoker, and worker coordination.
"""
import pytest
import asyncio
from unittest.mock import Mock, MagicMock, AsyncMock, patch
from typing import Dict, Any, Set

from motet.core.distributed.state_aware_routing import (
    StateAwareRouter,
    WorkerCandidate,
    RoutingStrategy
)
from motet.core.distributed.state_registry import (
    EphemeralStateRegistry,
    WorkerState,
    StateStatus,
    StateTypeDefinition,
    WarmStateTypeRegistry
)
from motet.core.commands.distributed import (
    DistributedCommand,
    DistributedCommandContext
)


class TestStateTypeDefinition:
    """Test state type definitions for warm state management."""

    @pytest.mark.unit
    def test_state_type_creation(self):
        """Test creating a state type definition."""
        state_type = StateTypeDefinition(
            name="test_state",
            default_ttl_seconds=300,
            reproduction_cost_ms=100,
            routing_weight=0.8
        )
        
        assert state_type.name == "test_state"
        assert state_type.default_ttl_seconds == 300
        assert state_type.reproduction_cost_ms == 100
        assert state_type.routing_weight == 0.8

    @pytest.mark.unit
    def test_state_type_serialization(self):
        """Test state type serialization."""
        state_type = StateTypeDefinition(
            name="mcp_connection",
            default_ttl_seconds=300,
            reproduction_cost_ms=150,
            routing_weight=0.9
        )
        
        serialized = state_type.to_dict()
        assert serialized["name"] == "mcp_connection"
        assert serialized["default_ttl_seconds"] == 300
        
        # Test deserialization (Pydantic v2)
        deserialized = StateTypeDefinition.model_validate(serialized)
        assert deserialized.name == state_type.name
        assert deserialized.routing_weight == state_type.routing_weight


class TestWorkerState:
    """Test worker state management."""

    @pytest.mark.unit
    def test_worker_state_creation(self):
        """Test creating worker state."""
        from datetime import datetime, timedelta
        state = WorkerState(
            worker_id="worker-1",
            worker_pid=12345,
            state_type="mcp_connection",
            status=StateStatus.ACTIVE,
            expires_at=datetime.utcnow() + timedelta(seconds=300),
            metadata={"server": "weather"}
        )
        
        assert state.worker_id == "worker-1"
        assert state.state_type == "mcp_connection"
        assert state.status == StateStatus.ACTIVE
        assert state.metadata["server"] == "weather"

    @pytest.mark.unit
    def test_worker_state_expiry(self):
        """Test worker state expiry logic."""
        from datetime import datetime, timedelta
        
        # Create expired state
        expired_state = WorkerState(
            worker_id="worker-1",
            worker_pid=1,
            state_type="test",
            status=StateStatus.ACTIVE,
            expires_at=datetime.utcnow() - timedelta(seconds=100)
        )
        
        assert expired_state.is_expired  # property, not method
        
        # Create active state
        active_state = WorkerState(
            worker_id="worker-2",
            worker_pid=2,
            state_type="test",
            status=StateStatus.ACTIVE,
            expires_at=datetime.utcnow() + timedelta(seconds=300)
        )
        
        assert not active_state.is_expired


class TestWarmStateTypeRegistry:
    """Test warm state type registry."""

    @pytest.mark.unit
    def test_built_in_state_types(self):
        """Test that built-in state types are registered."""
        registry = WarmStateTypeRegistry()
        
        type_names = [t.name for t in registry.list_state_types()]
        assert "mcp_connection" in type_names
        assert "model_cache" in type_names
        assert "database_pool" in type_names
        
        mcp_type = registry.get_state_type("mcp_connection")
        assert mcp_type is not None
        assert mcp_type.default_ttl_seconds == 300
        assert mcp_type.reproduction_cost_ms == 150

    @pytest.mark.unit
    def test_custom_state_type_registration(self):
        """Test registering custom state types."""
        registry = WarmStateTypeRegistry()
        
        custom_type = StateTypeDefinition(
            name="custom_cache",
            default_ttl_seconds=600,
            reproduction_cost_ms=50,
            routing_weight=0.7
        )
        
        registry.register_state_type(custom_type)
        
        retrieved = registry.get_state_type("custom_cache")
        assert retrieved is not None
        assert retrieved.name == "custom_cache"
        assert retrieved.default_ttl_seconds == 600


class TestEphemeralStateRegistry:
    """Test ephemeral state registry with Redis."""

    @pytest.fixture
    def mock_redis(self):
        """Mock Redis client (synchronous)."""
        redis_mock = MagicMock()
        redis_mock.setex = MagicMock(return_value=True)
        redis_mock.get = MagicMock(return_value=None)
        redis_mock.hset = MagicMock(return_value=True)
        redis_mock.hgetall = MagicMock(return_value={})
        redis_mock.sadd = MagicMock(return_value=1)
        redis_mock.smembers = MagicMock(return_value=set())
        redis_mock.delete = MagicMock(return_value=1)
        return redis_mock

    @pytest.fixture
    def state_registry(self, mock_redis):
        """Create state registry with mocked Redis."""
        return EphemeralStateRegistry(mock_redis)

    @pytest.mark.unit
    def test_register_worker_state(self, state_registry, mock_redis):
        """Test registering worker state."""
        from datetime import datetime, timedelta
        
        mock_redis.setex.return_value = True
        
        result = state_registry.register_worker_state(
            worker_id="worker-1",
            worker_pid=12345,
            state_type="mcp_connection",
            metadata={"server": "weather"}
        )
        
        assert result is not None
        assert result.worker_id == "worker-1"
        assert result.state_type == "mcp_connection"
        
        # Verify Redis was called correctly
        mock_redis.setex.assert_called_once()

    @pytest.mark.unit
    def test_get_workers_with_state(self, state_registry, mock_redis):
        """Test finding workers with specific state."""
        from datetime import datetime, timedelta
        import json
        
        worker_state_data = {
            "worker_id": "worker-1",
            "worker_pid": 12345,
            "state_type": "mcp_connection",
            "status": "active",
            "expires_at": (datetime.utcnow() + timedelta(seconds=300)).isoformat(),
            "metadata": {"server": "weather"}
        }
        mock_redis.get.return_value = json.dumps(worker_state_data)
        mock_redis.smembers.return_value = {b"worker-1:12345"}
        
        workers = state_registry.find_workers_with_state("mcp_connection")
        
        assert len(workers) == 1
        assert workers[0].worker_id == "worker-1"
        assert workers[0].state_type == "mcp_connection"


class TestWorkerCandidate:
    """Test worker candidate for routing."""

    @pytest.mark.unit
    def test_worker_candidate_creation(self):
        """Test creating worker candidate."""
        from datetime import datetime, timedelta
        warm = WorkerState(
            worker_id="worker-1",
            worker_pid=1,
            state_type="mcp_connection",
            expires_at=datetime.utcnow() + timedelta(seconds=300),
            metadata={"server": "weather"}
        )
        candidate = WorkerCandidate(
            worker_id="worker-1",
            worker_pid=1,
            capabilities={"tool_execution", "mcp_connection"},
            current_load=0.3,
            warm_states=[warm]
        )
        
        assert candidate.worker_id == "worker-1"
        assert "tool_execution" in candidate.capabilities
        assert candidate.current_load == 0.3
        assert candidate.has_state_type("mcp_connection")

    @pytest.mark.unit
    def test_worker_candidate_scoring(self):
        """Test worker candidate state affinity and freshness."""
        from datetime import datetime, timedelta
        warm = WorkerState(
            worker_id="worker-1",
            worker_pid=1,
            state_type="mcp_connection",
            expires_at=datetime.utcnow() + timedelta(seconds=300),
            metadata={"server": "weather"}
        )
        candidate = WorkerCandidate(
            worker_id="worker-1",
            worker_pid=1,
            capabilities={"tool_execution"},
            current_load=0.2,
            warm_states=[warm]
        )
        
        assert candidate.has_state_type("mcp_connection")
        assert candidate.get_state_freshness("mcp_connection") > 0


class TestStateAwareRouter:
    """Test state-aware routing system."""

    @pytest.fixture
    def mock_state_registry(self):
        """Mock state registry (sync)."""
        reg = MagicMock()
        reg.get_worker_states = MagicMock(return_value=[])
        return reg

    @pytest.fixture
    def router(self, mock_state_registry):
        """Create router with mocked dependencies."""
        return StateAwareRouter(state_registry=mock_state_registry)

    @pytest.fixture
    def mock_command(self):
        """Mock distributed command with required capabilities."""
        cmd = MagicMock(spec=DistributedCommand)
        cmd.get_required_capabilities = MagicMock(return_value={"tool_execution"})
        return cmd

    @pytest.mark.unit
    def test_select_worker_with_state_affinity(self, router, mock_state_registry, mock_command):
        """Test worker selection with state affinity (STATE_AWARE strategy)."""
        from datetime import datetime, timedelta
        warm = WorkerState(
            worker_id="worker-1",
            worker_pid=1,
            state_type="mcp_connection",
            expires_at=datetime.utcnow() + timedelta(seconds=300),
        )
        mock_state_registry.get_worker_states.side_effect = lambda wid: [warm] if wid == "worker-1" else []
        
        available_workers = [
            {"worker_id": "worker-1", "worker_pid": 1, "current_load": 0.3, "capabilities": ["tool_execution"]},
            {"worker_id": "worker-2", "worker_pid": 2, "current_load": 0.2, "capabilities": ["tool_execution"]},
        ]
        selected = router.select_optimal_worker(
            mock_command, available_workers, strategy=RoutingStrategy.STATE_AWARE
        )
        
        assert selected is not None
        assert selected.worker_id in ("worker-1", "worker-2")

    @pytest.mark.unit
    def test_select_worker_load_balancing(self, router, mock_state_registry, mock_command):
        """Test worker selection with load balancing strategy."""
        available_workers = [
            {"worker_id": "worker-1", "worker_pid": 1, "current_load": 0.8, "capabilities": ["tool_execution"]},
            {"worker_id": "worker-2", "worker_pid": 2, "current_load": 0.2, "capabilities": ["tool_execution"]},
        ]
        selected = router.select_optimal_worker(
            mock_command, available_workers, strategy=RoutingStrategy.LOAD_BALANCED
        )
        
        assert selected is not None
        assert selected.worker_id == "worker-2"


class TestDistributedCommand:
    """Test distributed command system."""

    @pytest.mark.unit
    def test_distributed_command_context(self):
        """Test distributed command context creation."""
        context = DistributedCommandContext(
            principal_id="user-123",
            task_id="task-456", 
            priority=7,
            timeout_seconds=60.0
        )
        
        assert context.principal_id == "user-123"
        assert context.task_id == "task-456"
        assert context.priority == 7
        assert context.timeout_seconds == 60.0

    @pytest.mark.unit
    def test_distributed_command_serialization(self):
        """Test distributed command serialization."""
        context = DistributedCommandContext(
            principal_id="user-123",
            task_id="task-456"
        )
        
        # Minimal test command (implements abstract methods)
        class TestCommand(DistributedCommand):
            def __init__(self, test_data: str):
                super().__init__("task-456", data=object(), principal_id="user-123")  # data non-None so to_dict() includes command data
                self.test_data = test_data

            def _do_execute(self, *args, **kwargs):
                return None

            def can_undo(self) -> bool:
                return False

            def undo(self):
                pass

            def get_command_type(self) -> str:
                return "test_command"

            def _get_command_specific_data(self) -> Dict[str, Any]:
                return {"test_data": self.test_data}

            def _serialize_command_data(self) -> Dict[str, Any]:
                return {"test_data": self.test_data}

            @classmethod
            def _deserialize_command_data(cls, data: Dict[str, Any], context: DistributedCommandContext):
                return cls(test_data=data["test_data"])

        command = TestCommand("test_value")
        
        # Test serialization
        serialized = command.to_dict()
        assert serialized["command_type"] == "test_command"
        assert serialized["principal_id"] == "user-123"
        assert serialized["test_data"] == "test_value"

    @pytest.mark.unit
    def test_get_worker_id_prefers_explicit_instance_field(self) -> None:
        class TestCommand(DistributedCommand):
            def __init__(self) -> None:
                super().__init__("task-wid", data=object(), principal_id="user-123")

            def _do_execute(self, *args, **kwargs):
                return None

            def can_undo(self) -> bool:
                return False

            def undo(self):
                pass

            def get_command_type(self) -> str:
                return "test_command"

            def _get_command_specific_data(self) -> Dict[str, Any]:
                return {}

            def _serialize_command_data(self) -> Dict[str, Any]:
                return {}

            @classmethod
            def _deserialize_command_data(cls, data, context):
                return cls()

        cmd = TestCommand()
        cmd._worker_id = "explicit_worker"
        assert cmd._get_worker_id() == "explicit_worker"

    @pytest.mark.unit
    def test_get_worker_id_falls_back_to_invoker_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class TestCommand(DistributedCommand):
            def __init__(self) -> None:
                super().__init__("task-wid2", data=object(), principal_id="user-123")

            def _do_execute(self, *args, **kwargs):
                return None

            def can_undo(self) -> bool:
                return False

            def undo(self):
                pass

            def get_command_type(self) -> str:
                return "test_command"

            def _get_command_specific_data(self) -> Dict[str, Any]:
                return {}

            def _serialize_command_data(self) -> Dict[str, Any]:
                return {}

            @classmethod
            def _deserialize_command_data(cls, data, context):
                return cls()

        monkeypatch.setattr(
            "motet.core.workers.invoker_context.get_worker_context",
            lambda: {"worker_id": "cloud_from_invoker_ctx"},
        )
        cmd = TestCommand()
        assert cmd._get_worker_id() == "cloud_from_invoker_ctx"

    @pytest.mark.unit
    def test_get_worker_id_falls_back_to_worker_utils(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class TestCommand(DistributedCommand):
            def __init__(self) -> None:
                super().__init__("task-wid3", data=object(), principal_id="user-123")

            def _do_execute(self, *args, **kwargs):
                return None

            def can_undo(self) -> bool:
                return False

            def undo(self):
                pass

            def get_command_type(self) -> str:
                return "test_command"

            def _get_command_specific_data(self) -> Dict[str, Any]:
                return {}

            def _serialize_command_data(self) -> Dict[str, Any]:
                return {}

            @classmethod
            def _deserialize_command_data(cls, data, context):
                return cls()

        monkeypatch.setattr(
            "motet.core.workers.invoker_context.get_worker_context",
            lambda: None,
        )
        monkeypatch.setenv("MOTET_WORKER_ID", "unit_meta_fallback")
        cmd = TestCommand()
        assert cmd._get_worker_id() == "cloud_unit_meta_fallback"


@pytest.mark.integration
@pytest.mark.requires_redis
class TestDistributedSystemIntegration:
    """Integration tests for distributed system components."""

    @pytest.mark.asyncio
    async def test_end_to_end_state_aware_routing(self):
        """Test complete state-aware routing workflow."""
        # This would be an integration test that requires Redis
        # and tests the complete workflow from command creation
        # to worker selection and execution
        pytest.skip("Integration test - requires Redis setup")

    @pytest.mark.asyncio
    async def test_worker_state_lifecycle(self):
        """Test complete worker state lifecycle."""
        # This would test worker registration, state updates,
        # expiry, and cleanup
        pytest.skip("Integration test - requires Redis setup")
