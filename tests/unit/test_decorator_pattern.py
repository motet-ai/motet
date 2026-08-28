"""
Unit tests for the decorator-based command pattern (ADR-0030).

Tests cover:
- @distributed_command decorator functionality
- MotetContext API (call, gather, dispatch, map)
- Command registration and discovery
- Error handling and validation
- Context propagation
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from typing import Any, Callable, Dict, cast

from motet.core.commands.decorator import (
    distributed_command,
    MotetContext,
    MotetToolsHelper,
    MotetMemoryHelper,
    DecoratedCommandConfig
)
from motet.core.commands.base_command_data import BaseCommandData
from motet.core.commands.distributed import DistributedCommand
from motet.core.commands.command_type_registry import (
    command_type_registry,
    CommandImplementationType,
)
from motet.core.commands.command_data_registry import register_command_data
from pydantic import Field


# Test data models
class SimpleTestData(BaseCommandData):
    """Simple test data model."""
    value: str = Field(default="test", description="Test value")
    count: int = Field(default=1, description="Count value")


class ComplexTestData(BaseCommandData):
    """Complex test data model."""
    items: list = Field(default_factory=list, description="List of items")
    extra_metadata: Dict[str, Any] = Field(default_factory=dict, description="Extra payload metadata")


# Test decorated commands
@distributed_command()
def simple_command(data: SimpleTestData, motet: MotetContext) -> Dict[str, Any]:
    """Simple test command."""
    return {
        "result": f"Processed: {data.value}",
        "count": data.count
    }


@distributed_command()
def complex_command(data: ComplexTestData, motet: MotetContext) -> Dict[str, Any]:
    """Complex test command."""
    return {
        "items_count": len(data.items),
        "has_metadata": bool(data.extra_metadata)
    }


@distributed_command()
def error_command(data: SimpleTestData, motet: MotetContext) -> Dict[str, Any]:
    """Command that raises so the decorator emits an error envelope."""
    raise ValueError(f"Intentional test error: {data.value}")


@distributed_command()
def nested_command(data: SimpleTestData, motet: MotetContext) -> Dict[str, Any]:
    """Command that calls another command."""
    # Call simple_command via motet._call()
    result = motet._call(simple_command, data=SimpleTestData(value="nested", count=2))
    
    return {
        "nested_result": result,
        "original_value": data.value
    }


@pytest.fixture(autouse=True)
def _ensure_test_commands_registered():
    """Re-register test commands so they are present regardless of test order or registry restore."""
    from motet.core.commands.builtin.schedule import ScheduleCommand as _SC  # noqa: F841 - ensure imported for registry
    DistributedCommand._ensure_commands_registered()
    for cmd_func, data_class in [
        (simple_command, SimpleTestData),
        (complex_command, ComplexTestData),
        (error_command, SimpleTestData),
        (nested_command, SimpleTestData),
    ]:
        command_type = getattr(cmd_func, "__command_type__", cmd_func.__name__)
        impl = getattr(cmd_func, "__command_class__", cmd_func)
        command_type_registry.register_command(
            command_type=command_type,
            implementation=impl,
            implementation_type=CommandImplementationType.DECORATOR_BASED,
            data_class=data_class,
            metadata={"timeout_seconds": 60},
            version="1.0.0",
            overwrite=True,
        )
        register_command_data(command_type, data_class, overwrite=True)
    yield


class TestDecoratorBasics:
    """Test basic decorator functionality."""
    
    def test_decorator_creates_command_class(self):
        """Test that decorator creates a proper command class."""
        # The decorator should have created a command class
        assert hasattr(simple_command, '__command_type__')
        assert getattr(simple_command, "__command_type__", None) == 'simple_command'
        
        # Command should be registered
        DistributedCommand._ensure_commands_registered()
        from motet.core.commands.command_type_registry import command_type_registry
        assert command_type_registry.is_registered('simple_command')
    
    def test_decorator_preserves_function_metadata(self):
        """Test that decorator preserves function name and docstring."""
        assert simple_command.__name__ == 'simple_command'
        assert simple_command.__doc__ == "Simple test command."
    
    def test_multiple_commands_registered(self):
        """Test that multiple decorated commands are registered."""
        DistributedCommand._ensure_commands_registered()
        
        from motet.core.commands.command_type_registry import command_type_registry
        assert command_type_registry.is_registered('simple_command')
        assert command_type_registry.is_registered('complex_command')
        assert command_type_registry.is_registered('error_command')
        assert command_type_registry.is_registered('nested_command')
    
    def test_command_class_has_correct_data_class(self):
        """Test that command class references the correct data class."""
        DistributedCommand._ensure_commands_registered()
        from motet.core.commands.command_type_registry import command_type_registry
        registration = command_type_registry.get('simple_command')
        
        assert registration is not None
        # For decorated commands, data_class is stored in the registration
        assert registration.data_class == SimpleTestData
        
        # Also verify the underlying command class has the data class
        command_class = getattr(simple_command, "__command_class__", None)
        assert command_class is not None
        assert command_class._get_data_class() == SimpleTestData


class TestMotetContextCall:
    """Test MotetContext._call() transport method."""
    
    def test_call_with_dict_data(self):
        """Test calling a command with dict data."""
        motet = MotetContext(
            task_id="test-task-123",
            command_id="test-cmd-456",
            conversation_id="test-conv-789",
            tenant_id="test-tenant",
            principal_id="test-principal"
        )
        
        # Mock the invoker
        with patch('motet.core.workers.invoker_context.get_distributed_invoker') as mock_get_invoker:
            mock_invoker = Mock()
            mock_get_invoker.return_value = mock_invoker
            
            # Mock execution result
            mock_invoker.execute_command.return_value = {
                "status": "completed",
                "result": {
                    "status": "success",
                    "data": {
                        "result": "Processed: test",
                        "count": 1
                    }
                },
                "execution_time_ms": 10
            }
            
            # Call the command
            result = motet._call(simple_command, data={"value": "test", "count": 1})
            
            # Verify result
            assert result["status"] == "success"
            assert result["data"]["result"] == "Processed: test"
            assert result["data"]["count"] == 1
            
            # Verify invoker was called
            mock_invoker.execute_command.assert_called_once()
    
    def test_call_with_data_class_instance(self):
        """Test calling a command with data class instance."""
        motet = MotetContext(
            task_id="test-task-123",
            command_id="test-cmd-456"
        )
        
        with patch('motet.core.workers.invoker_context.get_distributed_invoker') as mock_get_invoker:
            mock_invoker = Mock()
            mock_get_invoker.return_value = mock_invoker
            
            mock_invoker.execute_command.return_value = {
                "status": "completed",
                "result": {
                    "status": "success",
                    "data": {"result": "Processed: direct"}
                }
            }
            
            # Call with data class instance
            data = SimpleTestData(value="direct", count=3)
            result = motet._call(simple_command, data=data)
            
            assert result["status"] == "success"
            assert result["data"]["result"] == "Processed: direct"
    
    def test_call_propagates_context(self):
        """Test that call() propagates task_id and parent_command_id."""
        motet = MotetContext(
            task_id="test-task-789",
            command_id="test-cmd-parent",
            conversation_id="test-conv-123",
            worker_context={}
        )
        
        with patch('motet.core.workers.invoker_context.get_distributed_invoker') as mock_get_invoker:
            mock_invoker = Mock()
            mock_get_invoker.return_value = mock_invoker
            
            mock_invoker.execute_command.return_value = {
                "status": "completed",
                "result": {"status": "success", "data": {}}
            }
            
            motet._call(simple_command, data={"value": "test", "count": 1})
            
            # Verify the command was created with correct context
            call_args = mock_invoker.execute_command.call_args
            cmd = call_args[0][0]
            
            assert cmd.distributed_context.task_id == "test-task-789"
            assert cmd.distributed_context.parent_command_id == "test-cmd-parent"
            assert cmd.distributed_context.conversation_id == "test-conv-123"
    
    def test_call_with_class_based_command(self):
        """Test that call() works with regular DistributedCommand classes (e.g. ScheduleCommand)."""
        from motet.core.commands.builtin.schedule import ScheduleCommand
        from motet.core.commands.command_data_classes import ScheduleData
        
        motet = MotetContext(
            task_id="test-task-123",
            command_id="test-cmd-456",
            conversation_id="test-conv-789",
            worker_context={}
        )
        
        with patch('motet.core.workers.invoker_context.get_distributed_invoker') as mock_get_invoker:
            mock_invoker = Mock()
            mock_get_invoker.return_value = mock_invoker
            
            mock_invoker.execute_command.return_value = {
                "status": "completed",
                "result": {
                    "status": "success",
                    "data": {"scheduled": True}
                }
            }
            
            # Call with DistributedCommand class (ScheduleCommand is registered)
            result = motet._call(
                ScheduleCommand,
                data=ScheduleData(
                    target_command_type="simple_command",
                    target_command_data={"value": "test"},
                    schedule_type="immediate",
                )
            )
            
            # Verify result
            assert result["status"] == "success"
            assert result["data"]["scheduled"] is True
            
            # Verify invoker was called with ScheduleCommand instance
            call_args = mock_invoker.execute_command.call_args
            cmd = call_args[0][0]
            assert isinstance(cmd, ScheduleCommand)
            assert cmd.distributed_context.task_id == "test-task-123"
            assert cmd.distributed_context.parent_command_id == "test-cmd-456"
            assert cmd.data.target_command_type == "simple_command"
    
    def test_call_with_invalid_input(self):
        """Test that call() raises error for invalid input."""
        motet = MotetContext(
            task_id="test-123",
            command_id="cmd-456",
            worker_context={}
        )
        
        # Try to call with a regular function (not decorated)
        def regular_function(x):
            return x * 2
        
        with pytest.raises(ValueError, match="Expected decorated command function, DistributedCommand class, or instance"):
            motet._call(regular_function, data={})
    
    def test_call_propagates_context_for_class_based_command(self):
        """Test that call() propagates context correctly for class-based commands."""
        motet = MotetContext(
            task_id="test-task-123",
            command_id="parent-cmd-456",
            conversation_id="conv-789"
        )
        
        with patch('motet.core.workers.invoker_context.get_distributed_invoker') as mock_get_invoker:
            mock_invoker = Mock()
            mock_get_invoker.return_value = mock_invoker
            
            mock_invoker.execute_command.return_value = {
                "status": "completed",
                "result": {"status": "success", "data": {}}
            }
            
            motet._call(simple_command, data={"value": "test"})
            
            # Get the command that was passed to execute_command
            call_args = mock_invoker.execute_command.call_args
            command = call_args[0][0]
            
            # Verify context propagation (DecoratedCommand uses distributed_context for ids)
            assert command.distributed_context.task_id == "test-task-123"
            assert command.distributed_context.parent_command_id == "parent-cmd-456"
            assert command.distributed_context.conversation_id == "conv-789"
    
    def test_call_raises_on_invalid_command(self):
        """Test that call() raises error for non-decorated function."""
        motet = MotetContext(task_id="test-task-123", command_id="test-cmd-456")
        
        def not_decorated():
            pass
        
        with pytest.raises(ValueError, match="Expected decorated command function, DistributedCommand class, or instance"):
            motet._call(not_decorated, data={})
    
    def test_call_raises_on_execution_failure(self):
        """Test that call() propagates execution failures as exceptions."""
        motet = MotetContext(task_id="test-task-123", command_id="test-cmd-456")
        
        with patch('motet.core.workers.invoker_context.get_distributed_invoker') as mock_get_invoker:
            mock_invoker = Mock()
            mock_get_invoker.return_value = mock_invoker
            
            # Mock execution failure
            mock_invoker.execute_command.side_effect = RuntimeError("Worker unreachable")
            
            with pytest.raises(RuntimeError, match="Worker unreachable"):
                motet._call(simple_command, data={"value": "test"})
    
    def test_call_returns_error_response(self):
        """Test that call() returns error responses (not raises)."""
        motet = MotetContext(task_id="test-task-123", command_id="test-cmd-456")
        
        with patch('motet.core.workers.invoker_context.get_distributed_invoker') as mock_get_invoker:
            mock_invoker = Mock()
            mock_get_invoker.return_value = mock_invoker
            
            # Mock command error (not execution error)
            mock_invoker.execute_command.return_value = {
                "status": "completed",
                "result": {
                    "status": "error",
                    "data": None,
                    "error": {
                        "type": "ValidationError",
                        "message": "Invalid input"
                    }
                }
            }
            
            result = motet._call(simple_command, data={"value": "test"})
            
            # Should return error response, not raise
            assert result["status"] == "error"
            assert result["error"]["type"] == "ValidationError"


class TestMotetContextGather:
    """Test MotetContext.gather() method."""
    
    def test_gather_with_command_instances(self):
        """Test gather with pre-created command instances."""
        in_memory_payloads = {}

        def _fake_store(self, payload_dict):
            key = f"cmd:data:{self.command_id}"
            in_memory_payloads[key] = payload_dict
            return key

        @classmethod
        def _fake_retrieve(cls, redis_key, tenant_id=None, motet_id=None):
            return in_memory_payloads.get(redis_key)

        motet = MotetContext(task_id="test-task-123", command_id="test-cmd-456")

        with patch('motet.core.workers.invoker_context.get_distributed_invoker') as mock_get_invoker, \
             patch.object(DistributedCommand, "_store_command_data_in_redis", _fake_store), \
             patch.object(DistributedCommand, "_retrieve_command_data_from_redis", _fake_retrieve):
            mock_invoker = Mock()
            mock_get_invoker.return_value = mock_invoker

            mock_invoker.execute_command.return_value = {
                "status": "completed",
                "result": {
                    "status": "success",
                    "data": {
                        "results": [
                            {"command_id": "1", "data": {"result": "A"}},
                            {"command_id": "2", "data": {"result": "B"}}
                        ],
                        "successful": 2,
                        "failed": 0
                    }
                }
            }

            # Create command instances (DistributedCommand requires task_id)
            DistributedCommand._ensure_commands_registered()
            from motet.core.commands.command_type_registry import command_type_registry
            registration = command_type_registry.get('simple_command')
            assert registration is not None
            cmd_class = registration.implementation
            cmd1 = cmd_class(task_id="test-task-123", data=SimpleTestData(value="A"))
            cmd2 = cmd_class(task_id="test-task-123", data=SimpleTestData(value="B"))

            result = motet._gather([cmd1, cmd2])

            assert result["status"] == "success"
            assert len(result["data"]["results"]) == 2
    
    def test_gather_with_function_tuples(self):
        """Test gather with (function, data) tuples."""
        in_memory_payloads = {}

        def _fake_store(self, payload_dict):
            key = f"cmd:data:{self.command_id}"
            in_memory_payloads[key] = payload_dict
            return key

        @classmethod
        def _fake_retrieve(cls, redis_key, tenant_id=None, motet_id=None):
            return in_memory_payloads.get(redis_key)

        motet = MotetContext(task_id="test-task-123", command_id="test-cmd-456")

        with patch('motet.core.workers.invoker_context.get_distributed_invoker') as mock_get_invoker, \
             patch.object(DistributedCommand, "_store_command_data_in_redis", _fake_store), \
             patch.object(DistributedCommand, "_retrieve_command_data_from_redis", _fake_retrieve):
            mock_invoker = Mock()
            mock_get_invoker.return_value = mock_invoker

            mock_invoker.execute_command.return_value = {
                "status": "completed",
                "result": {
                    "status": "success",
                    "data": {
                        "results": [
                            {"command_id": "1", "data": {"result": "X"}},
                            {"command_id": "2", "data": {"result": "Y"}}
                        ],
                        "successful": 2,
                        "failed": 0
                    }
                }
            }

            result = motet._gather([
                (simple_command, {"value": "X"}),
                (simple_command, {"value": "Y"})
            ])

            assert result["status"] == "success"
            assert result["data"]["successful"] == 2
    
    def test_gather_with_class_based_tuples(self):
        """Test gather with (DistributedCommand class, data) tuples (e.g. ScheduleCommand)."""
        from motet.core.commands.builtin.schedule import ScheduleCommand
        from motet.core.commands.command_data_classes import ScheduleData

        in_memory_payloads = {}

        def _fake_store(self, payload_dict):
            key = f"cmd:data:{self.command_id}"
            in_memory_payloads[key] = payload_dict
            return key

        @classmethod
        def _fake_retrieve(cls, redis_key, tenant_id=None, motet_id=None):
            return in_memory_payloads.get(redis_key)

        motet = MotetContext(
            task_id="test-task-123",
            command_id="test-cmd-456",
            conversation_id="test-conv-789",
            worker_context={}
        )

        with patch('motet.core.workers.invoker_context.get_distributed_invoker') as mock_get_invoker, \
             patch.object(DistributedCommand, "_store_command_data_in_redis", _fake_store), \
             patch.object(DistributedCommand, "_retrieve_command_data_from_redis", _fake_retrieve):
            mock_invoker = Mock()
            mock_get_invoker.return_value = mock_invoker

            mock_invoker.execute_command.return_value = {
                "status": "completed",
                "result": {
                    "status": "success",
                    "data": {
                        "results": [
                            {"command_id": "1", "data": {"scheduled": True}},
                            {"command_id": "2", "data": {"scheduled": True}}
                        ],
                        "successful": 2,
                        "failed": 0
                    }
                }
            }

            # Use tuple syntax with DistributedCommand classes (ScheduleCommand is registered)
            result = motet._gather([
                (ScheduleCommand, ScheduleData(target_command_type="simple_command", target_command_data={"value": "a"}, schedule_type="immediate")),
                (ScheduleCommand, ScheduleData(target_command_type="simple_command", target_command_data={"value": "b"}, schedule_type="immediate")),
            ])

            # gather() returns the inner ADR-0029 result (execution_result.get('result', {}))
            assert result["status"] == "success"
            assert result["data"]["successful"] == 2
            mock_invoker.execute_command.assert_called_once()

    def test_gather_with_mixed_syntax(self):
        """Test gather with mixed pre-created instances, decorated tuples, and class tuples."""
        from motet.core.commands.builtin.schedule import ScheduleCommand
        from motet.core.commands.command_data_classes import ScheduleData

        in_memory_payloads = {}

        def _fake_store(self, payload_dict):
            key = f"cmd:data:{self.command_id}"
            in_memory_payloads[key] = payload_dict
            return key

        @classmethod
        def _fake_retrieve(cls, redis_key, tenant_id=None, motet_id=None):
            return in_memory_payloads.get(redis_key)

        motet = MotetContext(
            task_id="test-task-123",
            command_id="test-cmd-456",
            conversation_id="test-conv-789",
            worker_context={}
        )

        with patch('motet.core.workers.invoker_context.get_distributed_invoker') as mock_get_invoker, \
             patch.object(DistributedCommand, "_store_command_data_in_redis", _fake_store), \
             patch.object(DistributedCommand, "_retrieve_command_data_from_redis", _fake_retrieve):
            mock_invoker = Mock()
            mock_get_invoker.return_value = mock_invoker

            mock_invoker.execute_command.return_value = {
                "status": "completed",
                "result": {
                    "status": "success",
                    "data": {
                        "results": [
                            {"command_id": "1", "data": {"result": "A"}},
                            {"command_id": "2", "data": {"result": "B"}},
                            {"command_id": "3", "data": {"scheduled": True}}
                        ],
                        "successful": 3,
                        "failed": 0
                    }
                }
            }

            # Create a pre-created instance
            DistributedCommand._ensure_commands_registered()
            from motet.core.commands.command_type_registry import command_type_registry
            registration = command_type_registry.get('simple_command')
            assert registration is not None
            cmd_class = registration.implementation
            pre_created_cmd = cmd_class(task_id="test-123", data=SimpleTestData(value="A"))

            # Mix all three syntaxes: instance, decorated tuple, class tuple (ScheduleCommand)
            result = motet._gather([
                pre_created_cmd,
                (simple_command, {"value": "B"}),
                (ScheduleCommand, ScheduleData(target_command_type="simple_command", target_command_data={"value": "C"}, schedule_type="immediate")),
            ])

            # gather() returns the inner ADR-0029 result
            assert result["status"] == "success"
            assert result["data"]["successful"] == 3


class TestMotetContextDispatch:
    """Test MotetContext.dispatch() method."""
    
    def test_dispatch_returns_command_ids(self):
        """Test that dispatch returns list of dispatched command IDs."""
        in_memory_payloads = {}

        def _fake_store(self, payload_dict):
            key = f"cmd:data:{self.command_id}"
            in_memory_payloads[key] = payload_dict
            return key

        @classmethod
        def _fake_retrieve(cls, redis_key, tenant_id=None, motet_id=None):
            return in_memory_payloads.get(redis_key)

        motet = MotetContext(task_id="test-task-123", command_id="test-cmd-456")

        with patch('motet.core.workers.invoker_context.get_distributed_invoker') as mock_get_invoker, \
             patch.object(DistributedCommand, "_store_command_data_in_redis", _fake_store), \
             patch.object(DistributedCommand, "_retrieve_command_data_from_redis", _fake_retrieve):
            mock_invoker = Mock()
            mock_get_invoker.return_value = mock_invoker

            mock_invoker.execute_command.return_value = {
                "status": "completed",
                "result": {
                    "status": "success",
                    "data": {
                        "dispatched": ["cmd-1", "cmd-2", "cmd-3"]
                    }
                }
            }

            result = motet.dispatch([
                (simple_command, {"value": "A"}),
                (simple_command, {"value": "B"}),
                (simple_command, {"value": "C"})
            ])

            assert isinstance(result, list)
            assert len(result) == 3
    
    def test_dispatch_with_class_based_tuples(self):
        """Test dispatch with (DistributedCommand class, data) tuples (e.g. ScheduleCommand)."""
        from motet.core.commands.builtin.schedule import ScheduleCommand
        from motet.core.commands.command_data_classes import ScheduleData

        in_memory_payloads = {}

        def _fake_store(self, payload_dict):
            key = f"cmd:data:{self.command_id}"
            in_memory_payloads[key] = payload_dict
            return key

        @classmethod
        def _fake_retrieve(cls, redis_key, tenant_id=None, motet_id=None):
            return in_memory_payloads.get(redis_key)

        motet = MotetContext(
            task_id="test-task-123",
            command_id="test-cmd-456",
            conversation_id="test-conv-789",
            worker_context={}
        )

        with patch('motet.core.workers.invoker_context.get_distributed_invoker') as mock_get_invoker, \
             patch.object(DistributedCommand, "_store_command_data_in_redis", _fake_store), \
             patch.object(DistributedCommand, "_retrieve_command_data_from_redis", _fake_retrieve):
            mock_invoker = Mock()
            mock_get_invoker.return_value = mock_invoker

            mock_invoker.execute_command.return_value = {
                "status": "completed",
                "result": {
                    "status": "success",
                    "data": {"dispatched": ["cmd-1", "cmd-2"]}
                }
            }

            result = motet.dispatch([
                (ScheduleCommand, ScheduleData(target_command_type="simple_command", target_command_data={"value": "a"}, schedule_type="immediate")),
                (ScheduleCommand, ScheduleData(target_command_type="simple_command", target_command_data={"value": "b"}, schedule_type="immediate")),
            ])

            assert result == ["cmd-1", "cmd-2"]
            mock_invoker.execute_command.assert_called_once()
    
    def test_dispatch_with_mixed_syntax(self):
        """Test dispatch with mixed pre-created instances, decorated tuples, and class tuples."""
        from motet.core.commands.builtin.schedule import ScheduleCommand
        from motet.core.commands.command_data_classes import ScheduleData

        in_memory_payloads = {}

        def _fake_store(self, payload_dict):
            key = f"cmd:data:{self.command_id}"
            in_memory_payloads[key] = payload_dict
            return key

        @classmethod
        def _fake_retrieve(cls, redis_key, tenant_id=None, motet_id=None):
            return in_memory_payloads.get(redis_key)

        motet = MotetContext(
            task_id="test-task-123",
            command_id="test-cmd-456",
            conversation_id="test-conv-789",
            worker_context={}
        )

        with patch('motet.core.workers.invoker_context.get_distributed_invoker') as mock_get_invoker, \
             patch.object(DistributedCommand, "_store_command_data_in_redis", _fake_store), \
             patch.object(DistributedCommand, "_retrieve_command_data_from_redis", _fake_retrieve):
            mock_invoker = Mock()
            mock_get_invoker.return_value = mock_invoker

            mock_invoker.execute_command.return_value = {
                "status": "completed",
                "result": {
                    "status": "success",
                    "data": {"dispatched": ["cmd-1", "cmd-2", "cmd-3"]}
                }
            }

            DistributedCommand._ensure_commands_registered()
            from motet.core.commands.command_type_registry import command_type_registry
            registration = command_type_registry.get('simple_command')
            assert registration is not None
            cmd_class = registration.implementation
            pre_created_cmd = cmd_class(task_id="test-123", data=SimpleTestData(value="A"))

            result = motet.dispatch([
                pre_created_cmd,
                (simple_command, {"value": "B"}),
                (ScheduleCommand, ScheduleData(target_command_type="simple_command", target_command_data={"value": "C"}, schedule_type="immediate")),
            ])

            assert result == ["cmd-1", "cmd-2", "cmd-3"]


class TestMotetContextMap:
    """Test MotetContext.map() method."""
    
    def test_map_applies_command_to_multiple_inputs(self):
        """Test that map applies the same command to multiple inputs."""
        motet = MotetContext(task_id="test-task-123", command_id="test-cmd-456")
        
        with patch('motet.core.workers.invoker_context.get_distributed_invoker') as mock_get_invoker:
            mock_invoker = Mock()
            mock_get_invoker.return_value = mock_invoker
            
            # Mock MapCommand execution
            mock_invoker.execute_command.return_value = {
                "status": "completed",
                "result": {
                    "status": "success",
                    "data": {
                        "results": [
                            {"input_index": 0, "data": {"result": "Processed: A"}},
                            {"input_index": 1, "data": {"result": "Processed: B"}},
                            {"input_index": 2, "data": {"result": "Processed: C"}}
                        ],
                        "successful": 3,
                        "failed": 0
                    }
                }
            }
            
            inputs = [
                {"value": "A"},
                {"value": "B"},
                {"value": "C"}
            ]
            
            result = motet._map(simple_command, inputs=inputs)
            
            assert result["status"] == "success"
            assert len(result["data"]["results"]) == 3
            assert result["data"]["successful"] == 3
    
    def test_map_with_class_based_command(self):
        """Test map with DistributedCommand class (e.g. ScheduleCommand)."""
        from motet.core.commands.builtin.schedule import ScheduleCommand
        # Force-register ScheduleCommand (may have been cleared by other tests)
        command_type_registry.register_command(
            command_type="schedule",
            implementation=ScheduleCommand,
            implementation_type=CommandImplementationType.CLASS_BASED,
            overwrite=True,
        )

        motet = MotetContext(
            task_id="test-task-123",
            command_id="test-cmd-456",
            conversation_id="test-conv-789",
            worker_context={}
        )

        with patch('motet.core.workers.invoker_context.get_distributed_invoker') as mock_get_invoker:
            mock_invoker = Mock()
            mock_get_invoker.return_value = mock_invoker

            mock_invoker.execute_command.return_value = {
                "status": "completed",
                "result": {
                    "status": "success",
                    "data": {
                        "results": [
                            {"input_index": 0, "data": {"scheduled": True}},
                            {"input_index": 1, "data": {"scheduled": True}},
                            {"input_index": 2, "data": {"scheduled": True}}
                        ],
                        "successful": 3,
                        "failed": 0
                    }
                }
            }

            # ScheduleCommand is registered; inputs are ScheduleData-like dicts
            inputs = [
                {"target_command_type": "simple_command", "target_command_data": {"value": "A"}, "schedule_type": "immediate"},
                {"target_command_type": "simple_command", "target_command_data": {"value": "B"}, "schedule_type": "immediate"},
                {"target_command_type": "simple_command", "target_command_data": {"value": "C"}, "schedule_type": "immediate"},
            ]

            result = motet._map(ScheduleCommand, inputs=inputs, batch_size=10)

            assert result["status"] == "success"
            assert result["data"]["successful"] == 3
            mock_invoker.execute_command.assert_called_once()

    def test_apply_raises_when_all_batch_items_are_errors(self):
        """motet.apply should raise ApplyExecutionError when every item is _error."""
        motet = MotetContext(task_id="test-task-123", command_id="test-cmd-456")

        with patch("motet.core.workers.invoker_context.get_distributed_invoker") as mock_get_invoker:
            from motet.core.commands.response_models import ApplyExecutionError

            mock_invoker = Mock()
            mock_get_invoker.return_value = mock_invoker

            # Simulate map response where transport completed, but all nested items failed.
            mock_invoker.execute_command.return_value = {
                "status": "completed",
                "result": {
                    "status": "partial_success",
                    "data": {
                        "results": [
                            {
                                "status": "error",
                                "data": None,
                                "error": {
                                    "type": "CommandExecutionError",
                                    "message": "reload failed",
                                    "details": {},
                                    "recoverable": False,
                                    "retry_recommended": False,
                                },
                                "metadata": {
                                    "command_id": "cmd-1",
                                    "command_type": "simple_command",
                                    "execution_time_ms": 1.0,
                                },
                                "warnings": [],
                            },
                            {
                                "status": "error",
                                "data": None,
                                "error": {
                                    "type": "CommandExecutionError",
                                    "message": "reload failed",
                                    "details": {},
                                    "recoverable": False,
                                    "retry_recommended": False,
                                },
                                "metadata": {
                                    "command_id": "cmd-2",
                                    "command_type": "simple_command",
                                    "execution_time_ms": 1.0,
                                },
                                "warnings": [],
                            },
                        ],
                        "successful": 0,
                        "failed": 2,
                    },
                    "error": {
                        "type": "BatchProcessingError",
                        "message": "All items failed",
                        "details": {},
                    },
                    "metadata": {
                        "command_type": "core.map",
                        "command_id": "map-123",
                        "execution_time_ms": 1.0,
                    },
                },
            }

            inputs = [{"value": "A"}, {"value": "B"}]
            with pytest.raises(ApplyExecutionError):
                motet.apply(simple_command, inputs=inputs)


class TestDecoratedCommandExecution:
    """Test actual command execution (integration-style)."""
    
    def test_command_execution_flow(self):
        """Test the complete execution flow of a decorated command."""
        motet = MotetContext(
            task_id="test-task-123",
            command_id="test-cmd-456",
            conversation_id="conv-789",
            tenant_id="tenant-1",
            principal_id="user-1"
        )
        
        # Get the command class
        DistributedCommand._ensure_commands_registered()
        from motet.core.commands.command_type_registry import command_type_registry
        registration = command_type_registry.get('simple_command')
        assert registration is not None
        cmd_class = registration.implementation
        
        # Create command instance
        data = SimpleTestData(value="integration_test", count=5)
        cmd = cmd_class(
            task_id="test-task-123",
            data=data,
            conversation_id="conv-789"
        )
        
        # Verify command properties
        assert cmd.get_command_type() == "simple_command"
        assert cmd.data.value == "integration_test"
        assert cmd.data.count == 5
        assert cmd.distributed_context.task_id == "test-task-123"
        assert cmd.distributed_context.conversation_id == "conv-789"
    
    def test_command_serialization(self):
        """Test that decorated commands can be serialized/deserialized."""
        DistributedCommand._ensure_commands_registered()
        from motet.core.commands.command_type_registry import command_type_registry
        registration = command_type_registry.get('simple_command')
        assert registration is not None
        cmd_class = registration.implementation
        
        # Create command
        data = SimpleTestData(value="serialize_test", count=3)
        cmd = cmd_class(task_id="test-123", data=data)

        # Avoid touching real Redis/Vault encryption in unit tests by stubbing the Redis payload path.
        # This keeps the test focused on the transport format and Pydantic reconstruction.
        from unittest.mock import patch
        in_memory_payloads = {}

        def _fake_store(self, payload_dict):  # noqa: ANN001
            key = f"cmd:data:{self.command_id}"
            in_memory_payloads[key] = payload_dict
            return key

        @classmethod
        def _fake_retrieve(cls, redis_key, tenant_id=None, motet_id=None):  # noqa: ANN001
            return in_memory_payloads.get(redis_key)

        with patch.object(DistributedCommand, "_store_command_data_in_redis", _fake_store), patch.object(
            DistributedCommand, "_retrieve_command_data_from_redis", _fake_retrieve
        ):
            # Serialize
            serialized = cmd.serialize_for_transport()
            assert isinstance(serialized, str)

            # Deserialize
            deserialized = DistributedCommand.deserialize_from_transport(serialized)
        assert deserialized.get_command_type() == "simple_command"
        # Data is now retrieved from Redis, verify it's a SimpleTestData instance
        assert isinstance(deserialized.data, SimpleTestData)
        assert deserialized.data.count >= 1  # Has valid data


class TestErrorHandling:
    """Test error handling scenarios."""
    
    def test_invalid_data_type_raises_error(self):
        """Test that invalid data type raises appropriate error."""
        motet = MotetContext(task_id="test-task-123", command_id="test-cmd-456")
        
        with pytest.raises((ValueError, TypeError)):
            # Try to pass wrong data type
            motet._call(simple_command, data="not a dict or data class")
    
    def test_missing_required_field_raises_error(self):
        """Test that missing required fields raise validation errors."""
        # SimpleTestData has defaults, so let's create a stricter model
        from pydantic import Field
        
        class StrictData(BaseCommandData):
            required_field: str = Field(..., description="Required field")
        
        @distributed_command()
        def strict_command(data: StrictData, motet: MotetContext):
            return {"status": "success", "data": {}}
        
        motet = MotetContext(task_id="test-task-123", command_id="test-cmd-456")
        
        with pytest.raises(ValueError):
            # Missing required_field
            motet._call(strict_command, data={})


class TestContextPropagation:
    """Test context propagation through nested calls."""
    
    def test_nested_context_propagation_placeholder(self):
        """Nested motet._call() context propagation is validated via integration tests."""
        pass


class TestMotetContextResourceAccess:
    """Test MotetContext resource access properties."""
    
    def test_context_identifiers_without_agent_property(self):
        """MotetContext no longer exposes agent/llm; use motet.do(model_inference, ...) for model calls."""
        motet = MotetContext(
            task_id="test-123",
            command_id="cmd-456",
            worker_context={"memory_manager": Mock()}
        )
        assert motet.task_id == "test-123"
    
    def test_memory_property(self):
        """Test that memory property returns MotetMemoryHelper when manager present (ADR-0084)."""
        mock_memory = Mock()
        mock_memory.type = "redis_memory"
        
        motet = MotetContext(
            task_id="test-123",
            command_id="cmd-456",
            worker_context={"memory_manager": mock_memory}
        )
        
        assert isinstance(motet.memory, MotetMemoryHelper)
        assert motet.memory.type == "redis_memory"
    
    def test_tools_property(self):
        """Test that tools property returns MotetToolsHelper wrapping registry (ADR-0084)."""
        mock_registry = Mock()
        mock_registry.count = 5
        
        motet = MotetContext(
            task_id="test-123",
            command_id="cmd-456",
            worker_context={"tool_registry": mock_registry}
        )
        
        assert isinstance(motet.tools, MotetToolsHelper)
        assert motet.tools._registry is mock_registry
        assert motet.tools.count == 5
    
    def test_vault_property(self):
        """Test that vault property returns vault from command."""
        mock_command = Mock()
        mock_vault = Mock()
        mock_vault.name = "test_vault"
        mock_command.get_vault_client.return_value = mock_vault
        
        motet = MotetContext(
            task_id="test-123",
            command_id="cmd-456",
            command_instance=mock_command
        )
        
        vault = motet.vault
        assert vault == mock_vault
        assert vault.name == "test_vault"
        assert mock_command.get_vault_client.call_count >= 1
    
    def test_vault_property_no_command(self):
        """Test that vault property returns None when no command instance."""
        motet = MotetContext(
            task_id="test-123",
            command_id="cmd-456"
        )
        
        assert motet.vault is None
    
    def test_event_bus_property(self):
        """Test that event_bus property returns event_bus from worker_context."""
        mock_event_bus = Mock()
        mock_event_bus.name = "global_bus"
        
        motet = MotetContext(
            task_id="test-123",
            command_id="cmd-456",
            worker_context={"event_bus": mock_event_bus}
        )
        
        assert motet.event_bus == mock_event_bus
        assert motet.event_bus.name == "global_bus"
    
    def test_event_bus_property_none(self):
        """Test that event_bus property returns None when not in context."""
        motet = MotetContext(
            task_id="test-123",
            command_id="cmd-456",
            worker_context={}
        )
        
        assert motet.event_bus is None
    
    def test_observer_manager_property(self):
        """Test that observer_manager property returns observer_manager from worker_context."""
        mock_observer_manager = Mock()
        mock_observer_manager.name = "event_observer_manager"
        
        motet = MotetContext(
            task_id="test-123",
            command_id="cmd-456",
            worker_context={"observer_manager": mock_observer_manager}
        )
        
        assert motet.observer_manager == mock_observer_manager
        assert motet.observer_manager.name == "event_observer_manager"
    
    def test_observer_manager_property_none(self):
        """Test that observer_manager property returns None when not in context."""
        motet = MotetContext(
            task_id="test-123",
            command_id="cmd-456",
            worker_context={}
        )
        
        assert motet.observer_manager is None
    
    def test_observe_events_context_manager(self):
        """Test that observe_events() creates a TemporaryObserver context manager."""
        from motet.core.commands.decorator import TemporaryObserver
        
        mock_observer_manager = Mock()
        
        motet = MotetContext(
            task_id="test-123",
            command_id="cmd-456",
            worker_context={"observer_manager": mock_observer_manager}
        )
        
        def callback(event):
            pass
        
        # Create context manager
        ctx_mgr = motet.observe_events(
            event_types={"payment_completed"},
            callback=callback
        )
        
        # Verify it's a TemporaryObserver
        assert isinstance(ctx_mgr, TemporaryObserver)
        assert ctx_mgr.observer_manager == mock_observer_manager
        assert ctx_mgr.event_types == {"payment_completed"}
        assert ctx_mgr.callback == callback
    
    def test_observe_events_registers_and_unregisters(self):
        """Test that observe_events() registers/unregisters observer."""
        mock_observer_manager = Mock()
        
        motet = MotetContext(
            task_id="test-123",
            command_id="cmd-456",
            worker_context={"observer_manager": mock_observer_manager}
        )
        
        events_captured = []
        
        def callback(event):
            events_captured.append(event)
        
        # Use context manager
        with motet.observe_events(
            event_types={"test_event"},
            callback=callback
        ):
            # Verify observer was registered
            assert mock_observer_manager.register_observer.called
        
        # Verify observer was unregistered on exit
        assert mock_observer_manager.unregister_observer.called


class TestMotetContextStreamingHelpers:
    """Test MotetContext streaming helper methods."""

    def test_stream_event_disabled(self):
        """Test streaming event when streaming is disabled."""
        mock_command = Mock()
        mock_command._stream_enabled = False
        mock_command._stream_event = Mock()
        
        motet = MotetContext(
            task_id="test-123",
            command_id="cmd-456",
            command_instance=mock_command
        )
        
        # Should not call _stream_event when disabled
        motet.stream_event("progress", percent=50)
        
        mock_command._stream_event.assert_not_called()
    
    def test_stream_event_no_command(self):
        """Test streaming event with no command instance."""
        motet = MotetContext(
            task_id="test-123",
            command_id="cmd-456"
        )
        
        # Should not raise, just no-op
        motet.stream_event("progress", percent=50)
    


class TestMotetContextResponseHelpers:
    """Test MotetContext response helper methods."""
    
    def test_add_warning(self):
        """Warnings accumulate on the context for the decorator envelope."""
        motet = MotetContext(
            task_id="test-123",
            command_id="cmd-456"
        )
        motet.add_warning("Rate limit approaching")
        motet.add_warning("  ")
        motet.add_warning("Deprecated API used")
        assert motet._warnings == ["Rate limit approaching", "Deprecated API used"]


class TestMotetContextIntegration:
    """Test MotetContext integration scenarios."""
    
    def test_full_context_initialization(self):
        """Test initializing MotetContext with all parameters."""
        mock_agent = Mock()
        mock_memory = Mock()
        mock_tools = Mock()
        mock_event_bus = Mock()
        mock_observer_manager = Mock()
        mock_redis = Mock()
        
        # Create real DistributedCommandContext instead of mocking it
        from motet.core.commands.base import CommandContext
        
        # Create a real CommandContext object with all the required properties
        real_context = CommandContext(
            task_id="task-123",
            conversation_id="conv-456",
            tenant_id="tenant-abc",
            principal_id="user-xyz",
            motet_id="motet-001",
            metadata={"key": "value"},
        )
        
        # Use configure_mock to set the distributed_context and context identifiers
        mock_command = Mock()
        mock_command.configure_mock(
            distributed_context=real_context,
            command_id="cmd-789",
        )
        
        motet = MotetContext(
            redis=mock_redis,
            worker_context={
                "agent": mock_agent,
                "memory_manager": mock_memory,
                "tool_registry": mock_tools,
                "event_bus": mock_event_bus,
                "observer_manager": mock_observer_manager
            },
            command_instance=mock_command
        )
        
        # Verify all properties accessible (ADR-0084: tools is MotetToolsHelper wrapping registry)
        assert motet.task_id == "task-123"
        assert motet.conversation_id == "conv-456"
        assert motet.command_id == "cmd-789"
        assert motet.tenant_id == "tenant-abc"
        assert motet.principal_id == "user-xyz"
        assert motet.metadata == {"key": "value"}
        assert motet.redis == mock_redis
        assert isinstance(motet.memory, MotetMemoryHelper) and motet._worker_context.get("memory_manager") is mock_memory
        assert isinstance(motet.tools, MotetToolsHelper) and motet.tools._registry is mock_tools
        assert motet.event_bus == mock_event_bus
        assert motet.observer_manager == mock_observer_manager
    
    def test_context_used_in_decorated_command(self):
        """Test that MotetContext provides full API in decorated command."""
        @distributed_command()
        def context_test_command(data: SimpleTestData, motet: MotetContext) -> Dict[str, Any]:
            # Test resource access (model inference via motet.do(model_inference, ...), not motet.agent)
            has_memory = motet.memory is not None
            has_event_bus = motet.event_bus is not None
            
            # Test event publishing (if available)
            if motet.event_bus:
                motet.event_bus.publish({
                    "kind": "test_event",
                    "source": "test_command",
                    "data": {"test": True}
                })
            
            if data.value == "error":
                raise ValueError("Test error")

            if data.count > 5:
                motet.add_warning("test_warning")
            return {
                "has_memory": has_memory,
                "has_event_bus": has_event_bus,
                "task_id": motet.task_id
            }
        
        # Verify command was decorated
        assert getattr(context_test_command, "__command_type__", None) == "context_test_command"


class TestMotetNamespaceADR0089:
    """Tests for ADR-0089: @motet.command and @motet.tool namespace."""

    def test_motet_command_is_command_decorator(self):
        """motet.command is the command decorator; decorating a function registers a command."""
        from motet.core.commands.motet_namespace import motet as motet_ns
        cmd_decorator = cast(Callable[..., Any], motet_ns.command)
        assert callable(cmd_decorator), "motet.command should be callable (the decorator)"
        # Use the same decorator as other commands in this file for consistency

        @cmd_decorator(timeout_seconds=30)
        def motet_style_command(data: SimpleTestData, motet_ctx: MotetContext) -> Dict[str, Any]:
            return {"via": "motet.command"}

        assert getattr(motet_style_command, "__command_type__", None) == "motet_style_command"

    def test_motet_tool_registers_when_bundle_namespace_set(self):
        """@motet.tool registers with registry when bundle_tool_namespace(bundle_id) is active."""
        from motet.core.commands.decorator import (
            motet_tool,
            bundle_tool_namespace,
            _get_bundle_tool_namespace,
        )
        from motet.core.tools.registry import registry as tool_registry

        @motet_tool(description="Test tool for ADR-0089")
        def my_bundle_tool(params: Dict[str, Any]) -> Dict[str, Any]:
            return {"echo": params.get("x", "")}

        # Without bundle context, function is returned unchanged and not registered
        assert _get_bundle_tool_namespace() is None
        assert my_bundle_tool({"x": "hi"}) == {"echo": "hi"}

        # With bundle context, tool is registered as bundle_id.tool_name
        with bundle_tool_namespace("test_bundle"):
            @motet_tool(description="Registered test tool")
            def registered_tool(params: Dict[str, Any]) -> Dict[str, Any]:
                return {"ok": True}

            names = tool_registry.get_all_tool_names()
            assert "test_bundle.registered_tool" in names
            tool_registry.unregister("test_bundle.registered_tool")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

