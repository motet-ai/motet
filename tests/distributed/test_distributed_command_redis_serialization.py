"""
Tests for Redis-based command serialization in DistributedCommand.

This module tests the hybrid JSON + Redis serialization approach described in ADR-0014.
"""

import os
import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Dict, Any, List
from pydantic import BaseModel, Field

from motet.core.commands.distributed import (
    DistributedCommand, DistributedCommandContext, WorkerCapability, DistributionStrategy
)
from motet.core.commands.command_data_classes import ModelInferenceData
from motet.core.distributed import get_redis_command_data_manager
from motet.core.workers.observers import EventPriority


class TestCommandConfig(BaseModel):
    """Test configuration for commands (not a pytest class)."""
    __test__ = False
    test_data: str = "test_value"
    large_data: List[str] = Field(default_factory=lambda: ["item"] * 1000)  # Large data to trigger Redis storage


class TestDistributedCommand(DistributedCommand):
    """Test command implementation (not a pytest class)."""
    __test__ = False
    
    def _get_default_timeout(self) -> int:
        return 60
    
    def _get_default_priority(self) -> int:
        return 5  # EventPriority.NORMAL
    
    def _setup_command_specifics(self):
        self.distributed_context.required_capabilities = {WorkerCapability.MODEL_INFERENCE}
    
    @classmethod
    def _get_data_class(cls):
        return TestCommandConfig
    
    def get_command_type(self) -> str:
        return "test_command"
    
    def _do_execute(self, worker_context: Dict[str, Any]) -> Any:
        """Test implementation of _do_execute (sync)."""
        return {"result": "test_execution", "config": self.config.test_data}
    
    def can_undo(self) -> bool:
        """Test implementation of can_undo."""
        return False
    
    def undo(self) -> None:
        """Test implementation of undo."""
        pass
    
    @classmethod
    def _deserialize_from_data(cls, data: Dict[str, Any]) -> 'TestDistributedCommand':
        """Create command from dictionary data."""
        envelope = data.get("envelope") or {}
        payload = data.get("payload") or {}
        command_data = TestCommandConfig(
            test_data=payload.get("test_data", ""),
            large_data=payload.get("large_data", []),
        )
        
        cmd = cls(
            task_id=envelope.get("task_id", ""),
            data=command_data,
            command_id=envelope.get("command_id"),
            conversation_id=envelope.get("conversation_id", ""),
            tenant_id=envelope.get("tenant_id", "default"),
            motet_id=envelope.get("motet_id", "default"),
            priority=envelope.get("priority", 5),  # EventPriority.NORMAL
            timeout_seconds=envelope.get("timeout_seconds", 60),
            required_capabilities=envelope.get("required_capabilities", []),
        )
        serialized_caps = set()
        for cap in envelope.get("required_capabilities", []) or []:
            try:
                serialized_caps.add(WorkerCapability(str(cap)))
            except Exception:
                pass
        if serialized_caps:
            cmd.distributed_context.required_capabilities = serialized_caps
        return cmd


class TestDistributedCommandRedisSerialization:
    """Test Redis-based serialization for DistributedCommand."""
    
    def test_should_use_redis_storage_small_data(self):
        """Test that small data doesn't trigger Redis storage when threshold > 0."""
        context = DistributedCommandContext(
            task_id="test-task",
            conversation_id="test-conv",
            tenant_id="test-tenant"
        )
        config = TestCommandConfig(test_data="small", large_data=["item"])
        command = TestDistributedCommand("test-task", config)
        
        # Small data should not use Redis storage when threshold is > 0
        small_data = {"test_data": "small"}
        with patch.dict(os.environ, {"MOTET_DEBUG_MODE": "false"}), \
             patch('motet.core.config.Config') as mock_config:
            mock_config.return_value.redis_command_size_threshold_bytes = 1024
            mock_config.return_value.redis_command_complex_object_threshold = 500
            assert not command._should_use_redis_storage(small_data)
    
    def test_should_use_redis_storage_large_data(self):
        """Test that large data triggers Redis storage."""
        config = TestCommandConfig()
        command = TestDistributedCommand("test-task", config, tenant_id="test-tenant")
        
        # Large data should use Redis storage
        large_data = {"large_data": ["item"] * 1000}
        assert command._should_use_redis_storage(large_data)
    
    def test_should_use_redis_storage_complex_objects(self):
        """Test that complex objects trigger Redis storage."""
        config = TestCommandConfig()
        command = TestDistributedCommand("test-task", config, tenant_id="test-tenant")
        
        # Complex objects should use Redis storage
        complex_data = {"complex_obj": {"nested": {"deep": "value" * 200}}}
        assert command._should_use_redis_storage(complex_data)
    
    def test_estimate_data_size(self):
        """Test data size estimation."""
        config = TestCommandConfig()
        command = TestDistributedCommand("test-task", config, tenant_id="test-tenant")
        
        # Test size estimation
        small_data = {"test": "value"}
        large_data = {"test": "value" * 1000}
        
        small_size = command._estimate_data_size(small_data)
        large_size = command._estimate_data_size(large_data)
        
        assert small_size < large_size
        assert small_size > 0
        assert large_size > 1000
    
    def test_contains_complex_objects(self):
        """Test complex object detection."""
        config = TestCommandConfig()
        command = TestDistributedCommand("test-task", config, tenant_id="test-tenant")
        
        # Simple data should not contain complex objects
        simple_data = {"test": "value", "number": 42}
        assert not command._contains_complex_objects(simple_data)
        
        # Large lists should contain complex objects
        complex_data = {"large_list": ["item"] * 1000}
        assert command._contains_complex_objects(complex_data)
        
        # Large dicts should contain complex objects
        complex_data = {"large_dict": {"key": "value" * 1000}}  # Make it larger to exceed threshold
        assert command._contains_complex_objects(complex_data)
    
    @patch('motet.core.distributed.get_redis_command_data_manager')
    @pytest.mark.asyncio
    async def test_store_command_data_in_redis(self, mock_get_manager):
        """Test storing command data in Redis."""
        # Mock Redis manager
        mock_manager = MagicMock()
        mock_manager.store_command_data = MagicMock(return_value="cmd:data:test-command-id")
        mock_get_manager.return_value = mock_manager
        
        context = DistributedCommandContext(
            task_id="test-task",
            conversation_id="test-conv",
            tenant_id="test-tenant"
        )
        config = TestCommandConfig()
        command = TestDistributedCommand("test-task", config, tenant_id="test-tenant")
        
        # Test storing data in Redis
        test_data = {"test_data": "value", "large_data": ["item"] * 1000}
        redis_key = command._store_command_data_in_redis(test_data)
        
        assert redis_key == "cmd:data:test-command-id"
        mock_manager.store_command_data.assert_called_once()
        
        # Verify the call arguments
        call_args = mock_manager.store_command_data.call_args
        assert call_args[1]['command_id'] == command.command_id
        assert call_args[1]['command_timeout_seconds'] == 60
        # Verify tenant_id is passed for encryption (ADR-0056 Phase 1B)
        assert call_args[1]['tenant_id'] == "test-tenant"
    
    @patch('motet.core.distributed.get_redis_command_data_manager')
    def test_create_command_data_instance(self, mock_get_manager):
        """Test creating CommandData instance from serialized data."""
        context = DistributedCommandContext(
            task_id="test-task",
            conversation_id="test-conv",
            tenant_id="test-tenant"
        )
        config = TestCommandConfig()
        command = TestDistributedCommand("test-task", config)
        
        # Test creating CommandData instance
        test_data = {
            "messages": [],
            "model_settings": {"temperature": 0.7},
            "temperature": 0.7,
            "max_tokens": 1000,
            "stream": False
        }
        
        command_data = command._create_command_data_instance(test_data)
        
        # test_command has no registered data class, so _create_command_data_instance returns raw dict
        assert command_data is not None
        # Raw dict has the keys we passed; or if a model was used, it has message-related attributes
        assert "messages" in command_data or hasattr(command_data, "messages") or hasattr(command_data, "conversation_history")
        assert "model_settings" in command_data or hasattr(command_data, "model_settings") or hasattr(command_data, "metadata")
    
    @pytest.mark.asyncio
    async def test_serialize_for_transport_small_data(self):
        """Test serialization for small data (direct JSON)."""
        context = DistributedCommandContext(
            task_id="test-task",
            conversation_id="test-conv",
            tenant_id="test-tenant"
        )
        config = TestCommandConfig(test_data="small", large_data=["item"])
        command = TestDistributedCommand("test-task", config, use_redis_storage=False)

        # Serialize command
        serialized = command.serialize_for_transport()
        data = json.loads(serialized)
        
        # Small data should be included directly
        assert data["payload"]["test_data"] == "small"
        assert data["envelope"]["required_capabilities"] == ["model_inference"]
        assert "_redis_data_key" not in data["envelope"]
        assert "_data_size" not in data["envelope"]
    
    @pytest.mark.asyncio
    @patch('motet.core.distributed.get_redis_command_data_manager')
    async def test_serialize_for_transport_large_data(self, mock_get_manager):
        """Test serialization for large data (Redis storage)."""
        # Mock Redis manager
        mock_manager = MagicMock()
        mock_manager.store_command_data = MagicMock(return_value="cmd:data:test-command-id")
        mock_get_manager.return_value = mock_manager

        context = DistributedCommandContext(
            task_id="test-task",
            conversation_id="test-conv",
            tenant_id="test-tenant"
        )
        config = TestCommandConfig()  # This creates large data
        command = TestDistributedCommand("test-task", config)

        # Serialize command
        serialized = command.serialize_for_transport()
        data = json.loads(serialized)
        
        # Large data should be stored in Redis
        assert data["envelope"]["_redis_data_key"] == "cmd:data:test-command-id"
        assert data["envelope"]["_data_size"] > 1000
        
        # Large data should not be in transport payload
        assert "payload" not in data
    
    @pytest.mark.asyncio
    @patch('motet.core.distributed.get_redis_command_data_manager')
    async def test_deserialize_from_transport_with_redis_data(self, mock_get_manager):
        """Test deserialization from transport with Redis data."""
        # Mock Redis manager
        mock_manager = MagicMock()
        mock_manager.retrieve_command_data = MagicMock(return_value={"test_data": "from_redis", "large_data": []})
        mock_get_manager.return_value = mock_manager
        
        # Create transport data with Redis reference
        transport_data = {
            "envelope": {
                "command_id": "test-command-id",
                "command_type": "test_command",
                "task_id": "test-task",
                "conversation_id": "test-conv",
                "tenant_id": "test-tenant",
                "motet_id": "default",
                "_redis_data_key": "cmd:data:test-command-id",
                "_data_size": 2000,
            }
        }
        
        # Deserialize command
        command = TestDistributedCommand.deserialize_from_transport(json.dumps(transport_data))
        
        # Verify command was deserialized correctly
        assert command.command_id == 'test-command-id'
        assert command.distributed_context.task_id == 'test-task'
        assert command.distributed_context.conversation_id == 'test-conv'
        
        # Verify Redis data was retrieved (implementation passes motet_id)
        mock_manager.retrieve_command_data.assert_called_once_with(
            'cmd:data:test-command-id',
            tenant_id='test-tenant',
            motet_id='default'
        )
    
    @pytest.mark.asyncio
    @patch('motet.core.distributed.get_redis_command_data_manager')
    async def test_deserialize_from_transport_without_redis_data(self, mock_get_manager):
        """Test deserialization from transport without Redis data."""
        # Create transport data without Redis reference
        transport_data = {
            "envelope": {
                "command_id": "test-command-id",
                "command_type": "test_command",
                "task_id": "test-task",
                "conversation_id": "test-conv",
                "tenant_id": "test-tenant",
                "motet_id": "default",
                "timeout_seconds": 60,
                "required_capabilities": ["edge_execution", "edge_clipboard", "tool_execution"],
            },
            "payload": {"test_data": "small", "large_data": []},
        }
        
        # Deserialize command
        command = TestDistributedCommand.deserialize_from_transport(json.dumps(transport_data))
        
        # Verify command was deserialized correctly
        assert command.command_id == 'test-command-id'
        assert command.distributed_context.task_id == 'test-task'
        assert command.distributed_context.conversation_id == 'test-conv'
        assert command.distributed_context.required_capabilities == {
            WorkerCapability.EDGE_EXECUTION,
            WorkerCapability.EDGE_CLIPBOARD,
            WorkerCapability.TOOL_EXECUTION,
        }
        
        # Redis manager should not be called
        mock_get_manager.assert_not_called()
    
    @pytest.mark.asyncio
    @patch('motet.core.distributed.get_redis_command_data_manager')
    async def test_retrieve_command_data_from_redis_success(self, mock_get_manager):
        """Test successful retrieval of command data from Redis."""
        # Mock Redis manager
        mock_manager = MagicMock()
        mock_command_data = ModelInferenceData(
            messages=[],
            model_settings={"temperature": 0.7}
        )
        mock_manager.retrieve_command_data = MagicMock(return_value=mock_command_data)
        mock_get_manager.return_value = mock_manager
        
        # Test retrieval
        result = TestDistributedCommand._retrieve_command_data_from_redis("cmd:data:test-command-id")
        
        assert result == mock_command_data
        mock_manager.retrieve_command_data.assert_called_once_with(
            'cmd:data:test-command-id',
            tenant_id=None,
            motet_id=None
        )
    
    @pytest.mark.asyncio
    @patch('motet.core.distributed.get_redis_command_data_manager')
    async def test_retrieve_command_data_from_redis_failure(self, mock_get_manager):
        """Test failure handling when retrieving command data from Redis."""
        # Mock Redis manager to raise exception
        mock_manager = MagicMock()
        mock_manager.retrieve_command_data = MagicMock(side_effect=Exception("Redis connection failed"))
        mock_get_manager.return_value = mock_manager
        
        # Test retrieval with failure
        result = TestDistributedCommand._retrieve_command_data_from_redis("cmd:data:test-command-id")
        
        assert result is None
        mock_manager.retrieve_command_data.assert_called_once_with(
            'cmd:data:test-command-id',
            tenant_id=None,
            motet_id=None
        )
    
    @patch('motet.core.distributed.get_redis_command_data_manager')
    def test_store_result_in_redis(self, mock_get_manager):
        """Test storing command result in Redis."""
        # Mock Redis manager
        mock_manager = MagicMock()
        mock_manager.store_command_result.return_value = "cmd:result:test-command-id"
        mock_get_manager.return_value = mock_manager
        
        context = DistributedCommandContext(
            task_id="test-task",
            conversation_id="test-conv",
            tenant_id="test-tenant"
        )
        config = TestCommandConfig()
        command = TestDistributedCommand("test-task", config)
        
        # Test storing result
        test_result = {"output": "test result", "metadata": {"processing_time": 1.5}}
        redis_key = command.store_result_in_redis(test_result)
        
        assert redis_key == "cmd:result:test-command-id"
        mock_manager.store_command_result.assert_called_once()
        
        # Verify the call arguments
        call_args = mock_manager.store_command_result.call_args
        assert call_args[1]['command_id'] == command.command_id
        assert call_args[1]['result'] == test_result
        assert call_args[1]['command_timeout_seconds'] == 60
        assert call_args[1]['tenant_id'] == command.distributed_context.tenant_id
    
    @patch('motet.core.distributed.get_redis_command_data_manager')
    def test_retrieve_result_from_redis_success(self, mock_get_manager):
        """Test successful retrieval of command result from Redis."""
        # Mock Redis manager
        mock_manager = MagicMock()
        test_result = {"output": "test result", "metadata": {"processing_time": 1.5}}
        mock_manager.retrieve_command_result.return_value = test_result
        mock_get_manager.return_value = mock_manager
        
        context = DistributedCommandContext(
            task_id="test-task",
            conversation_id="test-conv",
            tenant_id="test-tenant"
        )
        config = TestCommandConfig()
        command = TestDistributedCommand("test-task", config)
        
        # Test retrieval
        result = command.retrieve_result_from_redis()
        
        assert result == test_result
        mock_manager.retrieve_command_result.assert_called_once_with(
            command.command_id,
            tenant_id=command.distributed_context.tenant_id,
            motet_id=command.distributed_context.motet_id
        )
    
    @patch('motet.core.distributed.get_redis_command_data_manager')
    def test_retrieve_result_from_redis_failure(self, mock_get_manager):
        """Test failure handling when retrieving command result from Redis."""
        # Mock Redis manager to raise exception
        mock_manager = MagicMock()
        mock_manager.retrieve_command_result.side_effect = Exception("Redis connection failed")
        mock_get_manager.return_value = mock_manager
        
        context = DistributedCommandContext(
            task_id="test-task",
            conversation_id="test-conv",
            tenant_id="test-tenant"
        )
        config = TestCommandConfig()
        command = TestDistributedCommand("test-task", config)
        
        # Test retrieval with failure
        result = command.retrieve_result_from_redis()
        
        assert result is None
        mock_manager.retrieve_command_result.assert_called_once_with(
            command.command_id,
            tenant_id=command.distributed_context.tenant_id,
            motet_id=command.distributed_context.motet_id
        )


class TestDistributedCommandRedisSerializationIntegration:
    """Integration tests for Redis-based command serialization."""
    
    @pytest.mark.asyncio
    @patch('motet.core.distributed.get_redis_command_data_manager')
    async def test_full_serialization_lifecycle(self, mock_get_manager):
        """Test complete serialization and deserialization lifecycle."""
        # Mock Redis manager
        mock_manager = MagicMock()
        mock_manager.store_command_data = MagicMock(return_value="cmd:data:test-command-id")
        mock_manager.store_command_result = MagicMock(return_value="cmd:result:test-command-id")
        
        # Mock command data for retrieval (RedisCommandDataManager returns dicts)
        mock_manager.retrieve_command_data = MagicMock(return_value={"test_data": "from_redis", "large_data": []})
        
        # Mock result for retrieval
        test_result = {"output": "test result", "metadata": {"processing_time": 1.5}}
        mock_manager.retrieve_command_result.return_value = test_result
        
        mock_get_manager.return_value = mock_manager
        
        # Create original command
        context = DistributedCommandContext(
            task_id="test-task",
            conversation_id="test-conv",
            tenant_id="test-tenant"
        )
        config = TestCommandConfig()  # Large data
        original_command = TestDistributedCommand("test-task", config)
        
        # Serialize command
        serialized = original_command.serialize_for_transport()
        
        # Deserialize command
        deserialized_command = TestDistributedCommand.deserialize_from_transport(serialized)
        
        # Verify deserialized command
        assert deserialized_command.command_id == original_command.command_id
        assert deserialized_command.distributed_context.task_id == original_command.distributed_context.task_id
        assert deserialized_command.distributed_context.conversation_id == original_command.distributed_context.conversation_id
        
        # Store and retrieve result
        result_key = deserialized_command.store_result_in_redis(test_result)
        retrieved_result = deserialized_command.retrieve_result_from_redis()
        
        assert result_key == "cmd:result:test-command-id"
        assert retrieved_result == test_result
        
        # Verify all Redis operations were called
        mock_manager.store_command_data.assert_called_once()
        mock_manager.retrieve_command_data.assert_called_once()
        mock_manager.store_command_result.assert_called_once()
        mock_manager.retrieve_command_result.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_performance_comparison_small_vs_large_data(self):
        """Test performance characteristics of small vs large data serialization."""
        import time
        from unittest.mock import MagicMock, patch
        
        context = DistributedCommandContext(
            task_id="test-task",
            conversation_id="test-conv",
            tenant_id="test-tenant"
        )
        
        # Test small data serialization
        small_config = TestCommandConfig(test_data="small", large_data=["item"])
        small_command = TestDistributedCommand("test-task", small_config, use_redis_storage=False)
        
        start_time = time.time()
        small_serialized = small_command.serialize_for_transport()
        small_time = time.time() - start_time
        
        # Test large data serialization (mock Redis manager to avoid Vault/encryption dependencies)
        large_config = TestCommandConfig()  # Large data
        large_command = TestDistributedCommand("test-task", large_config)
        with patch("motet.core.distributed.get_redis_command_data_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.store_command_data = MagicMock(return_value="cmd:data:test-command-id")
            mock_get_manager.return_value = mock_manager

            start_time = time.time()
            large_serialized = large_command.serialize_for_transport()
            large_time = time.time() - start_time
        
        # Small data should be faster to serialize (direct JSON)
        # Large data might be slower due to Redis operations, but this is expected
        assert small_time < 0.1  # Should be very fast
        assert large_time < 1.0  # Should be reasonable even with Redis operations
        
        # Verify serialized data characteristics
        small_data = json.loads(small_serialized)
        large_data = json.loads(large_serialized)
        
        # Small data should be included directly
        assert "payload" in small_data
        assert small_data["payload"].get("test_data") == "small"
        assert "_redis_data_key" not in small_data["envelope"]
        
        # Large data should reference Redis
        assert "_redis_data_key" in large_data["envelope"]
        assert "payload" not in large_data


class TestDistributedCommandRedisStorageFlag:
    """Test the use_redis_storage flag functionality."""
    
    def test_use_redis_storage_flag_disabled(self):
        """Test that Redis storage is disabled when flag is False."""
        config = TestCommandConfig()
        command = TestDistributedCommand(
            "test-task", 
            config,
            conversation_id="test-conv",
            tenant_id="test-tenant",
            use_redis_storage=False  # Disable Redis storage
        )
        
        # Even large data should not use Redis storage when flag is disabled
        large_data = {"large_data": ["item"] * 1000}
        assert not command._should_use_redis_storage(large_data)
    
    def test_use_redis_storage_flag_enabled(self):
        """Test that Redis storage works when flag is True (default)."""
        config = TestCommandConfig()
        command = TestDistributedCommand(
            "test-task", 
            config,
            conversation_id="test-conv",
            tenant_id="test-tenant",
            use_redis_storage=True  # Enable Redis storage (default)
        )
        
        # Large data should use Redis storage when flag is enabled
        large_data = {"large_data": ["item"] * 1000}
        with patch('motet.core.config.Config') as mock_config:
            mock_config.return_value.redis_command_size_threshold_bytes = 1024
            mock_config.return_value.redis_command_complex_object_threshold = 500
            assert command._should_use_redis_storage(large_data)
    
    def test_use_redis_storage_flag_default(self):
        """Test that Redis storage is enabled by default."""
        config = TestCommandConfig()
        command = TestDistributedCommand(
            "test-task", 
            config,
            conversation_id="test-conv",
            tenant_id="test-tenant"
            # use_redis_storage not specified - should default to True
        )
        
        # Should default to True
        assert command.distributed_context.use_redis_storage is True


class TestDistributedCommandConfigOptions:
    """Test the config options for Redis storage thresholds."""
    
    def test_redis_command_size_threshold_zero_always_redis(self):
        """Test that threshold 0 forces Redis storage for all data."""
        config = TestCommandConfig()
        command = TestDistributedCommand(
            "test-task", 
            config,
            conversation_id="test-conv",
            tenant_id="test-tenant"
        )
        
        # Even small data should use Redis when threshold is 0
        small_data = {"test_data": "small"}
        with patch('motet.core.config.Config') as mock_config:
            mock_config.return_value.redis_command_size_threshold_bytes = 0
            mock_config.return_value.redis_command_complex_object_threshold = 500
            assert command._should_use_redis_storage(small_data)
    
    def test_redis_command_size_threshold_custom(self):
        """Test custom size threshold configuration."""
        config = TestCommandConfig()
        command = TestDistributedCommand(
            "test-task", 
            config,
            conversation_id="test-conv",
            tenant_id="test-tenant"
        )
        
        # Test with custom threshold
        small_data = {"test_data": "small"}
        large_data = {"large_data": ["item"] * 1000}
        
        with patch.dict(os.environ, {"MOTET_DEBUG_MODE": "false"}), \
             patch('motet.core.config.Config') as mock_config:
            mock_config.return_value.redis_command_size_threshold_bytes = 5000  # 5KB threshold
            mock_config.return_value.redis_command_complex_object_threshold = 500
            
            # Small data should not use Redis
            assert not command._should_use_redis_storage(small_data)
            
            # Large data should use Redis
            assert command._should_use_redis_storage(large_data)
    
    def test_redis_command_complex_object_threshold_custom(self):
        """Test custom complex object threshold configuration."""
        config = TestCommandConfig()
        command = TestDistributedCommand(
            "test-task", 
            config,
            conversation_id="test-conv",
            tenant_id="test-tenant"
        )
        
        # Test with custom complex object threshold
        complex_data = {"complex_obj": {"nested": {"deep": "value" * 100}}}  # ~500 chars
        
        with patch.dict(os.environ, {"MOTET_DEBUG_MODE": "false"}), \
             patch('motet.core.config.Config') as mock_config:
            mock_config.return_value.redis_command_size_threshold_bytes = 1024
            mock_config.return_value.redis_command_complex_object_threshold = 1000  # Higher threshold
            
            # Should not trigger complex object detection with higher threshold
            assert not command._should_use_redis_storage(complex_data)
            
            # Lower threshold should trigger complex object detection
            mock_config.return_value.redis_command_complex_object_threshold = 200
            assert command._should_use_redis_storage(complex_data)


# Register the test command
DistributedCommand.register_command_type(TestDistributedCommand)
