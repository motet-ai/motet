"""
Fixtures for distributed system testing.

Provides common fixtures for distributed command testing, worker mocking,
and system setup.
"""
import pytest
import asyncio
from unittest.mock import Mock, AsyncMock
from typing import Dict, Any, List

from motet.core.commands.distributed import DistributedCommandContext
from motet.core.distributed.state_aware_routing import WorkerCandidate
from motet.core.types import Message


@pytest.fixture
def distributed_context():
    """Standard distributed command context for testing."""
    return DistributedCommandContext(
        principal_id="test-user-123",
        task_id="test-task-456",
        priority=5,
        timeout_seconds=30.0
    )


@pytest.fixture
def high_priority_context():
    """High priority distributed command context."""
    return DistributedCommandContext(
        principal_id="test-user-123",
        task_id="urgent-task-789",
        priority=9,
        timeout_seconds=60.0
    )


@pytest.fixture
def sample_messages():
    """Sample conversation messages for testing."""
    return [
        Message(role="user", content="What is the weather like today?"),
        Message(role="assistant", content="I'll help you check the weather for you."),
        Message(role="user", content="Thank you, I'm in San Francisco.")
    ]


@pytest.fixture
def complex_messages():
    """Complex conversation messages for testing reasoning."""
    return [
        Message(role="user", content="I need to plan a trip to Europe for 2 weeks in summer. I want to visit 3 countries, have a budget of $5000, and prefer historical sites. Can you help me create a detailed itinerary?"),
        Message(role="assistant", content="I'd be happy to help you plan your European trip! Let me break this down and create a comprehensive itinerary for you."),
        Message(role="user", content="I'm particularly interested in Rome, Paris, and Prague. What would be the best order to visit them?")
    ]


@pytest.fixture
def mock_worker_candidates():
    """Mock worker candidates for routing tests."""
    return [
        WorkerCandidate(
            worker_id="worker-1",
            capabilities={"tool_execution", "reasoning"},
            current_load=0.2,
            warm_states={"mcp_connection": {"server": "weather"}},
            last_seen=1234567890
        ),
        WorkerCandidate(
            worker_id="worker-2",
            capabilities={"tool_execution", "model_inference"},
            current_load=0.7,
            warm_states={"model_cache": {"model": "gpt-4o-mini"}},
            last_seen=1234567890
        ),
        WorkerCandidate(
            worker_id="worker-3",
            capabilities={"memory_operations", "reasoning"},
            current_load=0.1,
            warm_states={"database_pool": {"connections": 5}},
            last_seen=1234567890
        )
    ]


@pytest.fixture
def mock_redis_client():
    """Mock Redis client for testing."""
    client = AsyncMock()
    
    # Mock common Redis operations
    client.hset.return_value = True
    client.hget.return_value = None
    client.hgetall.return_value = {}
    client.expire.return_value = True
    client.delete.return_value = 1
    client.exists.return_value = False
    
    # Mock Redis streams
    client.xadd.return_value = "1234567890-0"
    client.xread.return_value = []
    client.xdel.return_value = 1
    
    return client


@pytest.fixture
def mock_state_registry(mock_redis_client):
    """Mock state registry for testing."""
    from motet.core.distributed.state_registry import EphemeralStateRegistry
    
    with pytest.mock.patch('motet.core.distributed.state_registry.get_redis_client', return_value=mock_redis_client):
        registry = EphemeralStateRegistry()
        return registry


@pytest.fixture
def mock_orchestrator():
    """Mock orchestrator for testing."""
    orchestrator = Mock()
    orchestrator.config = Mock()

    # Mock streaming via stream_events (primary public streaming interface).
    async def mock_stream_events(stack, messages, context=None):
        events = [
            {"event": "turn", "state": "preparing"},
            {"event": "turn", "state": "thinking"},
            {"event": "reasoning_meta", "data": {"strategy": "direct", "complexity": "simple"}},
            {"event": "reasoning_step", "data": {"step": 1, "trace": "Analyzing request"}},
            {"event": "token", "data": "Test "},
            {"event": "token", "data": "response"},
            {"event": "turn", "state": "completing"},
            {"event": "end", "content": "Test response"},
        ]
        for event in events:
            yield event
            await asyncio.sleep(0.01)

    orchestrator.stream_events = mock_stream_events

    return orchestrator


@pytest.fixture
def mock_tool_registry():
    """Mock tool registry for testing."""
    registry = Mock()
    
    # Mock tool list
    tool_list = [
        {"name": "note", "description": "Create a note"},
        {"name": "web_search", "description": "Search the web"},
        {"name": "weather", "description": "Get weather information"}
    ]
    registry.list.return_value = tool_list
    registry.list_items.return_value = {tool["name"]: tool for tool in tool_list}
    
    # Mock tool registration
    registry.register.return_value = True
    
    # Mock tool execution
    async def execute_tool(name, parameters, context=None):
        if name == "note":
            return {"result": f"Note created: {parameters.get('content', 'Empty note')}"}
        elif name == "web_search":
            return {"result": f"Search results for: {parameters.get('query', 'empty query')}"}
        elif name == "weather":
            return {"result": f"Weather for {parameters.get('location', 'unknown location')}: Sunny, 72°F"}
        else:
            return {"error": f"Unknown tool: {name}"}
    
    registry.execute = execute_tool
    
    return registry


@pytest.fixture
def mock_mcp_manager():
    """Mock MCP manager for testing."""
    manager = AsyncMock()
    
    # Mock MCP server configurations
    manager.get_server_configs.return_value = [
        {
            "name": "weather_server",
            "command": ["python", "-m", "weather_mcp"],
            "transport_type": "stdio"
        },
        {
            "name": "file_server", 
            "command": ["python", "-m", "file_mcp"],
            "transport_type": "stdio"
        }
    ]
    
    # Mock connection creation
    async def get_or_create_connection(server_config):
        connection = Mock()
        connection.server_name = server_config.name
        connection.is_connected = True
        return connection
    
    manager.get_or_create_connection = get_or_create_connection
    
    return manager


@pytest.fixture
def performance_baseline():
    """Performance baseline metrics for testing."""
    return {
        "command_routing_ms": 5.0,
        "tool_execution_ms": 100.0,
        "model_inference_ms": 1000.0,
        "memory_operation_ms": 50.0,
        "state_lookup_ms": 10.0,
        "worker_selection_ms": 15.0,
        "local_chat_latency_ms": 500.0,
        "local_reasoning_latency_ms": 2000.0,
        "local_tool_latency_ms": 300.0,
    }


@pytest.fixture
def performance_tracker():
    """Simple performance timer for distributed/performance tests."""
    import time as _time

    class _Tracker:
        def __init__(self):
            self._starts = {}
            self._ends = {}

        def start_timer(self, name: str) -> None:
            self._starts[name] = _time.perf_counter()

        def end_timer(self, name: str) -> None:
            self._ends[name] = _time.perf_counter()

        def get_duration(self, name: str) -> float:
            end = self._ends.get(name, _time.perf_counter())
            start = self._starts.get(name, end - 0.001)
            return (end - start) * 1000.0  # ms

        def compare_to_baseline(self, name: str, baseline_ms: float) -> dict:
            actual = self.get_duration(name)
            return {"actual_ms": actual, "baseline_ms": baseline_ms, "ratio": actual / baseline_ms if baseline_ms else 0}
    return _Tracker()


@pytest.fixture
def test_data():
    """Chat API payloads for distributed and performance tests (POST /api/v1/chat shape)."""
    return {
        "simple_chat": {
            "messages": [{"role": "user", "content": "What is 2+2?"}],
            "stream": False,
        },
        "streaming_chat": {
            "messages": [{"role": "user", "content": "Count from 1 to 5."}],
            "stream": True,
        },
        "complex_reasoning": {
            "messages": [{"role": "user", "content": "Explain photosynthesis and its importance for climate. Use simple terms."}],
            "stream": False,
        },
        "tool_usage": {
            "messages": [{"role": "user", "content": "Create a note saying 'distributed test note'"}],
            "stream": False,
        },
    }


@pytest.fixture
def test_scenarios():
    """Common test scenarios and expected outcomes."""
    return {
        "simple_chat": {
            "input": [{"role": "user", "content": "Hello"}],
            "expected_phases": ["preparing", "thinking", "responding", "completing"],
            "max_duration_seconds": 10
        },
        "tool_usage": {
            "input": [{"role": "user", "content": "Create a note saying 'test note'"}],
            "expected_tools": ["note"],
            "max_duration_seconds": 15
        },
        "complex_reasoning": {
            "input": [{"role": "user", "content": "Plan a 3-day trip to Paris with a $1000 budget"}],
            "expected_phases": ["preparing", "thinking", "responding", "completing"],
            "expected_reasoning": True,
            "max_duration_seconds": 30
        },
        "memory_operation": {
            "input": [{"role": "user", "content": "Remember that I like Italian food"}],
            "expected_memory_ops": ["store"],
            "max_duration_seconds": 10
        }
    }


@pytest.fixture(scope="session")
def event_loop():
    """Create an event loop for the test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def wait_for_system_ready():
    """Utility to wait for distributed system to be ready."""
    async def _wait(timeout_seconds=30):
        """Wait for the distributed system to be ready."""
        import time
        start_time = time.time()
        
        while time.time() - start_time < timeout_seconds:
            try:
                # Check if core components are available
                from motet.core.workers.pure_invoker import global_invoker
                if global_invoker is not None:
                    return True
            except Exception:
                pass
            
            await asyncio.sleep(0.5)
        
        return False
    
    return _wait


@pytest.fixture
def assert_performance_within_bounds():
    """Utility to assert performance is within expected bounds."""
    def _assert_performance(actual_ms: float, expected_ms: float, tolerance_factor: float = 2.0):
        """
        Assert that actual performance is within tolerance of expected performance.
        
        Args:
            actual_ms: Actual execution time in milliseconds
            expected_ms: Expected execution time in milliseconds
            tolerance_factor: Multiplier for acceptable performance degradation
        """
        max_allowed_ms = expected_ms * tolerance_factor
        assert actual_ms <= max_allowed_ms, (
            f"Performance degraded: {actual_ms:.1f}ms > {max_allowed_ms:.1f}ms "
            f"(expected {expected_ms:.1f}ms with {tolerance_factor}x tolerance)"
        )
    
    return _assert_performance


@pytest.fixture
def create_test_command():
    """Utility to create test distributed commands."""
    def _create_command(command_type: str, context: DistributedCommandContext, **kwargs):
        """Create a test distributed command of the specified type."""
        if command_type == "tool_execution":
            from motet.core.commands.builtin.tool import tool_execution, ToolExecutionData
            return tool_execution(
                command_id=f"test-{command_type}",
                task_id=context.task_id,
                conversation_id=context.conversation_id,
                tenant_id=context.tenant_id,
                principal_id=context.principal_id,
                data=ToolExecutionData(
                    tool_name=kwargs.get("tool_name", "note"),
                    parameters=kwargs.get("parameters", {"content": "test"})
                )
            )
        elif command_type == "model_inference":
            from motet.core.commands.builtin.model import model_inference
            from motet.core.commands.command_data_classes import ModelInferenceData
            return model_inference(
                data=ModelInferenceData(
                    messages=kwargs.get("messages", [Message(role="user", content="test")]),
                    model_settings=kwargs.get("model_settings", {"provider": "openai", "model_name": "gpt-4o-mini"}),
                ),
                command_id=f"test-{command_type}",
                task_id=context.task_id,
                conversation_id=context.conversation_id,
                tenant_id=context.tenant_id,
                principal_id=context.principal_id,
            )
        elif command_type == "agent_turn":
            from motet.core.orchestration.turn import agent_turn
            from motet.core.commands.command_data_classes import AgentTurnData
            return agent_turn(
                data=AgentTurnData(
                    messages=kwargs.get("messages", [Message(role="user", content="test")]),
                    context=kwargs.get("context"),
                ),
                command_id=f"test-{command_type}",
                task_id=context.task_id,
                conversation_id=context.conversation_id,
                tenant_id=context.tenant_id,
                principal_id=context.principal_id,
            )
        else:
            raise ValueError(f"Unknown command type: {command_type}")
    
    return _create_command


@pytest.fixture
def mock_event_observer_manager():
    """Mock EventObserverManager for testing."""
    from motet.core.workers.event_observer_manager import EventObserverManager
    
    manager = EventObserverManager()
    
    # Mock Redis operations to avoid external dependencies
    async def mock_register_observer(observer):
        manager.observers.append(observer)
    
    async def mock_unregister_observer(observer):
        if observer in manager.observers:
            manager.observers.remove(observer)
    
    async def mock_notify_observers(event):
        for observer in manager.observers:
            try:
                await observer.notify(event)
            except Exception:
                pass  # Ignore observer errors in tests
    
    manager.register_observer = mock_register_observer
    manager.unregister_observer = mock_unregister_observer
    manager.notify_observers = mock_notify_observers
    
    return manager


@pytest.fixture
def mock_streaming_observer():
    """Mock StreamingObserver for testing."""
    from motet.core.workers.observers import StreamingObserver
    
    observer = StreamingObserver(
        name="test_streaming_observer",
        task_id="test_task_123",
        stream_until_event="command_completed",
        target_command_type="agent_turn"
    )
    
    return observer
