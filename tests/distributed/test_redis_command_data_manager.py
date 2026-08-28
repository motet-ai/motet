"""
Tests for Redis-based command data storage and retrieval.

This module tests the RedisCommandDataManager class as described in ADR-0014,
including TTL management, MsgPack serialization, and error handling.
"""

import pytest
import json
import msgpack
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta

from motet.core.distributed.redis_command_data_manager import RedisCommandDataManager


class TestRedisCommandDataManager:
    """Test cases for RedisCommandDataManager."""
    
    @pytest.fixture
    def mock_redis(self):
        """Create a mock Redis client (synchronous)."""
        redis_mock = MagicMock()
        redis_mock.setex = MagicMock(return_value=True)
        redis_mock.get = MagicMock(return_value=None)
        redis_mock.exists = MagicMock(return_value=False)
        redis_mock.ttl = MagicMock(return_value=-1)
        redis_mock.delete = MagicMock(return_value=1)
        redis_mock.scan_iter = MagicMock(return_value=iter([]))
        return redis_mock
    
    @pytest.fixture
    def manager(self, mock_redis):
        """Create a RedisCommandDataManager instance with mock Redis (encryption off for unit tests)."""
        return RedisCommandDataManager(redis_client=mock_redis, ttl_seconds=3600, enable_encryption=False)
    
    def test_calculate_redis_ttl_with_timeout(self, manager):
        """Test TTL calculation with command timeout."""
        # Test with command timeout (120s * 2 = 240s, but minimum is 300s)
        ttl = manager.calculate_redis_ttl(120)  # 2 minutes
        assert ttl == 300  # Minimum 5 minutes (240 < 300)
        
        # Test with short timeout (should use minimum)
        ttl = manager.calculate_redis_ttl(60)  # 1 minute
        assert ttl == 300  # Minimum 5 minutes (120 < 300)
        
        # Test with longer timeout (should use 2x timeout)
        ttl = manager.calculate_redis_ttl(200)  # 200 seconds
        assert ttl == 400  # 2x timeout = 400 seconds (400 > 300)
        
        # Test with no timeout (should use default)
        ttl = manager.calculate_redis_ttl(None)
        assert ttl == 3600  # Default TTL
    
    def test_calculate_redis_ttl_edge_cases(self, manager):
        """Test TTL calculation edge cases."""
        # Test with zero timeout (0 * 2 = 0, but minimum is 300)
        ttl = manager.calculate_redis_ttl(0)
        assert ttl == 300  # Minimum 5 minutes
        
        # Test with very long timeout
        ttl = manager.calculate_redis_ttl(3600)  # 1 hour
        assert ttl == 7200  # 2x timeout = 2 hours
    
    def test_get_debug_ttl_normal_mode(self, manager):
        """Test debug TTL in normal mode."""
        manager._debug_mode = False
        base_ttl = 300
        
        ttl = manager._get_debug_ttl(base_ttl, "agent_turn")
        assert ttl == base_ttl  # No extension in normal mode
    
    def test_get_debug_ttl_debug_mode(self, manager):
        """Test debug TTL in debug mode (implementation uses core.* command_type keys)."""
        manager._debug_mode = True
        base_ttl = 300
        ttl = manager._get_debug_ttl(base_ttl, "core.agent_turn")
        assert ttl == base_ttl + 3600  # +1 hour for turn commands
        ttl = manager._get_debug_ttl(base_ttl, "core.model_inference")
        assert ttl == base_ttl + 1800  # +30 min for model inference
        ttl = manager._get_debug_ttl(base_ttl, "core.tool_execution")
        assert ttl == base_ttl + 900  # +15 min for tool execution
        ttl = manager._get_debug_ttl(base_ttl, "core.workflow_execution")
        assert ttl == base_ttl + 7200  # +2 hours for workflow execution
        ttl = manager._get_debug_ttl(base_ttl, "unknown")
        assert ttl == base_ttl + 1800  # Default +30 minutes

    def test_resolve_cmd_meta_key_finds_tenant_prefixed_hash(self, manager, mock_redis):
        """Completion updates must find {tenant}:cmd:meta:{id} without tenant_id."""
        prefixed = "motet-global:cmd:meta:cmd-1"
        payload = {"status": "executing", "tenant_id": "motet-global"}

        def hgetall(key):
            return payload if key == prefixed else {}

        mock_redis.hgetall.side_effect = hgetall
        mock_redis.scan_iter.return_value = iter([prefixed])

        key, existing = manager._resolve_cmd_meta_key("cmd-1")
        assert key == prefixed
        assert existing == payload

    def test_resolve_cmd_meta_key_uses_tenant_hint_without_scan(self, manager, mock_redis):
        prefixed = "acme:cmd:meta:cmd-2"
        payload = {"status": "executing", "tenant_id": "acme"}

        def hgetall(key):
            return payload if key == prefixed else {}

        mock_redis.hgetall.side_effect = hgetall
        mock_redis.scan_iter.return_value = iter([])

        key, existing = manager._resolve_cmd_meta_key("cmd-2", tenant_id="acme")
        assert key == prefixed
        assert existing == payload
        mock_redis.scan_iter.assert_not_called()
    
    def test_store_command_data_success(self, manager):
        """Test successful command data storage."""
        command_id = "test-command-123"
        data = {"test": "data", "nested": {"key": "value"}}
        command_type = "reasoning"
        
        result_key = manager.store_command_data(
            command_id, data, command_timeout_seconds=120, command_type=command_type
        )
        
        # Verify key format
        assert result_key == f"cmd:data:{command_id}"
        
        # Verify Redis setex was called
        manager.redis.setex.assert_called_once()
        call_args = manager.redis.setex.call_args
        
        # Verify key
        assert call_args[0][0] == result_key
        
        # Verify TTL (2x timeout = 240 seconds, but minimum is 300)
        assert call_args[0][1] == 300
        
        # Verify serialized data
        serialized_data = call_args[0][2]
        deserialized = msgpack.unpackb(serialized_data, raw=False)
        
        assert deserialized["data"] == data
        assert deserialized["metadata"]["command_type"] == command_type
        assert deserialized["metadata"]["command_id"] == command_id
        assert deserialized["metadata"]["debug_mode"] is False
        assert deserialized["metadata"]["original_ttl"] == 300
    
    def test_store_command_data_debug_mode(self, manager):
        """Test command data storage in debug mode (implementation uses core.agent_turn)."""
        manager._debug_mode = True
        command_id = "test-command-123"
        data = {"test": "data"}
        result_key = manager.store_command_data(
            command_id, data, command_timeout_seconds=120, command_type="core.agent_turn"
        )
        
        # Verify TTL includes debug extension (implementation uses core.agent_turn key)
        call_args = manager.redis.setex.call_args
        ttl = call_args[0][1]
        assert ttl == 300 + 3600  # Base TTL (300) + debug extension for core.agent_turn
        
        # Verify debug mode in metadata
        serialized_data = call_args[0][2]
        deserialized = msgpack.unpackb(serialized_data, raw=False)
        assert deserialized["metadata"]["debug_mode"] is True
    
    def test_store_command_data_redis_error(self, manager):
        """Test command data storage with Redis error."""
        command_id = "test-command-123"
        data = {"test": "data"}
        
        # Mock Redis error
        manager.redis.setex = MagicMock(side_effect=Exception("Redis connection failed"))
        
        with pytest.raises(RuntimeError, match="Failed to store command data"):
            manager.store_command_data(command_id, data)
    
    def test_retrieve_command_data_success(self, manager):
        """Test successful command data retrieval."""
        key = "cmd:data:test-command-123"
        original_data = {"test": "data", "nested": {"key": "value"}}
        
        # Mock storage data
        storage_data = {
            "data": original_data,
            "metadata": {
                "command_type": "reasoning",
                "stored_at": datetime.utcnow().isoformat(),
                "command_id": "test-command-123"
            }
        }
        serialized_data = msgpack.packb(storage_data, use_bin_type=True)
        
        # Mock Redis get
        manager.redis.get = MagicMock(return_value=serialized_data)
        
        result = manager.retrieve_command_data(key)
        
        # Verify data retrieval
        assert result == original_data
        
        # Verify Redis get was called
        manager.redis.get.assert_called_once_with(key)
    
    def test_retrieve_command_data_not_found(self, manager):
        """Test command data retrieval when data not found."""
        key = "cmd:data:nonexistent"
        
        # Mock Redis get returning None
        manager.redis.get = MagicMock(return_value=None)
        
        with pytest.raises(ValueError, match="Command data not found or expired"):
            manager.retrieve_command_data(key)
    
    def test_retrieve_command_data_redis_error(self, manager):
        """Test command data retrieval with Redis error."""
        key = "cmd:data:test-command-123"
        
        # Mock Redis error
        manager.redis.get = MagicMock(side_effect=Exception("Redis connection failed"))
        
        with pytest.raises(ValueError, match="Command data not found or expired"):
            manager.retrieve_command_data(key)
    
    def test_store_command_result_success(self, manager):
        """Test successful command result storage."""
        command_id = "test-command-123"
        result = {"status": "success", "data": "result data"}
        
        result_key = manager.store_command_result(
            command_id, result, command_timeout_seconds=120
        )
        
        # Verify key format
        assert result_key == f"cmd:result:{command_id}"
        
        # Verify Redis setex was called
        manager.redis.setex.assert_called_once()
        call_args = manager.redis.setex.call_args
        
        # Verify key and TTL
        assert call_args[0][0] == result_key
        assert call_args[0][1] == 300  # 2x timeout (240) but minimum is 300
        
        # Verify serialized data
        serialized_data = call_args[0][2]
        deserialized = msgpack.unpackb(serialized_data, raw=False)
        
        assert deserialized["result"] == result
        assert deserialized["metadata"]["command_id"] == command_id
        assert deserialized["metadata"]["result_type"] == "dict"

    def test_store_command_wait_outcome_uses_outcome_key(self, manager):
        command_id = "test-command-wait"
        envelope = {
            "status": "completed",
            "command_id": command_id,
            "command_type": "core.tool_execution",
            "result": {"ok": True},
        }

        result_key = manager.store_command_wait_outcome(
            command_id, envelope, tenant_id="acme", motet_id="default"
        )

        assert result_key == f"acme:cmd:outcome:{command_id}"
        manager.redis.setex.assert_called_once()
        assert manager.redis.setex.call_args[0][0] == result_key

    def _pack_stored_payload(self, payload, command_id: str) -> bytes:
        return msgpack.packb(
            {
                "result": payload,
                "metadata": {
                    "command_id": command_id,
                    "result_type": "dict",
                    "stored_at": datetime.utcnow().isoformat(),
                },
            },
            use_bin_type=True,
        )

    def test_retrieve_command_wait_outcome_hydrates_redis_result_key(self, manager):
        """Wait retrieve follows cmd:result pointers so join/do see ADR-0029."""
        command_id = "cmd-hydrate"
        tenant_id = "acme"
        result_key = f"{tenant_id}:cmd:result:{command_id}"
        outcome_key = f"{tenant_id}:cmd:outcome:{command_id}"
        domain = {
            "status": "success",
            "data": {"tool_name": "core.tools_search", "hits": 10},
            "metadata": {},
        }
        envelope = {
            "status": "completed",
            "command_id": command_id,
            "command_type": "core.tool_execution",
            "result": {"_redis_result_key": result_key},
        }
        packed = {
            outcome_key: self._pack_stored_payload(envelope, command_id),
            result_key: self._pack_stored_payload(domain, command_id),
        }
        manager.redis.get = MagicMock(side_effect=lambda key: packed.get(key))

        loaded = manager.retrieve_command_wait_outcome(
            command_id, tenant_id=tenant_id, motet_id="default"
        )

        assert loaded["status"] == "completed"
        assert loaded["result"] == domain
        assert loaded["result_retrieved_from_redis"] is True

    def test_retrieve_command_wait_outcome_inline_result_unchanged(self, manager):
        command_id = "cmd-inline"
        envelope = {
            "status": "completed",
            "command_id": command_id,
            "result": {"status": "success", "data": {"v": 1}, "metadata": {}},
        }
        manager.redis.get = MagicMock(
            return_value=self._pack_stored_payload(envelope, command_id)
        )

        loaded = manager.retrieve_command_wait_outcome(command_id)

        assert loaded == envelope
        manager.redis.get.assert_called_once()

    def test_retrieve_command_wait_outcome_missing_result_key_fails(self, manager):
        command_id = "cmd-missing-body"
        result_key = f"cmd:result:{command_id}"
        envelope = {
            "status": "completed",
            "command_id": command_id,
            "result": {"_redis_result_key": result_key},
        }
        manager.redis.get = MagicMock(
            side_effect=lambda key: (
                self._pack_stored_payload(envelope, command_id)
                if key.endswith(f"cmd:outcome:{command_id}")
                else None
            )
        )

        with pytest.raises(ValueError, match="missing command result"):
            manager.retrieve_command_wait_outcome(command_id)
    
    def test_retrieve_command_result_success(self, manager):
        """Test successful command result retrieval."""
        key = "cmd:result:test-command-123"
        original_result = {"status": "success", "data": "result data"}
        
        # Mock storage data
        storage_data = {
            "result": original_result,
            "metadata": {
                "command_id": "test-command-123",
                "stored_at": datetime.utcnow().isoformat(),
                "result_type": "dict"
            }
        }
        serialized_data = msgpack.packb(storage_data, use_bin_type=True)
        
        # Mock Redis get
        manager.redis.get = MagicMock(return_value=serialized_data)
        
        result = manager.retrieve_command_result(key)
        
        # Verify result retrieval
        assert result == original_result
        
        # Verify Redis get was called
        manager.redis.get.assert_called_once_with(key)
    
    def test_retrieve_command_result_not_found(self, manager):
        """Test command result retrieval when result not found."""
        key = "cmd:result:nonexistent"
        
        # Mock Redis get returning None
        manager.redis.get = MagicMock(return_value=None)
        
        with pytest.raises(ValueError, match="Command result not found or expired"):
            manager.retrieve_command_result(key)
    
    def test_cleanup_expired_data_success(self, manager):
        """Test successful cleanup of expired data."""
        # Mock scan_iter to return keys
        mock_keys = ["cmd:data:expired1", "cmd:data:expired2", "cmd:result:expired3"]
        
        manager.redis.scan_iter = MagicMock(return_value=iter(mock_keys))
        manager.redis.exists = MagicMock(return_value=True)
        manager.redis.ttl = MagicMock(return_value=30)  # Less than 60 seconds
        manager.redis.delete = MagicMock(return_value=3)
        
        deleted_count = manager.cleanup_expired_data()
        
        # Verify cleanup
        assert deleted_count == 3
        manager.redis.delete.assert_called_once_with(*mock_keys)
    
    def test_cleanup_expired_data_no_expired(self, manager):
        """Test cleanup when no data is expired."""
        # Mock scan_iter to return keys
        mock_keys = ["cmd:data:active1", "cmd:data:active2"]
        
        manager.redis.scan_iter = MagicMock(return_value=iter(mock_keys))
        manager.redis.exists = MagicMock(return_value=True)
        manager.redis.ttl = MagicMock(return_value=300)  # More than 60 seconds
        manager.redis.delete = MagicMock()
        
        deleted_count = manager.cleanup_expired_data()
        
        # Verify no cleanup
        assert deleted_count == 0
        manager.redis.delete.assert_not_called()
    
    def test_cleanup_expired_data_redis_error(self, manager):
        """Test cleanup with Redis error."""
        # Mock Redis error
        manager.redis.scan_iter = MagicMock(side_effect=Exception("Redis connection failed"))
        
        deleted_count = manager.cleanup_expired_data()
        
        # Should return 0 on error
        assert deleted_count == 0
    
    def test_get_storage_stats_success(self, manager):
        """Test successful storage statistics retrieval."""
        # Mock command data keys
        mock_data_keys = ["cmd:data:cmd1", "cmd:data:cmd2"]
        mock_result_keys = ["cmd:result:cmd1"]
        
        # Mock storage data
        storage_data = {
            "data": {"test": "data"},
            "metadata": {
                "command_type": "reasoning",
                "stored_at": datetime.utcnow().isoformat()
            }
        }
        serialized_data = msgpack.packb(storage_data, use_bin_type=True)
        
        def mock_scan_iter(match):
            if match == "cmd:data:*":
                return iter(mock_data_keys)
            elif match == "cmd:result:*":
                return iter(mock_result_keys)
            return iter([])
        
        manager.redis.scan_iter = MagicMock(side_effect=mock_scan_iter)
        manager.redis.exists = MagicMock(return_value=True)
        manager.redis.get = MagicMock(return_value=serialized_data)
        
        stats = manager.get_storage_stats()
        
        # Verify statistics
        assert stats["total_command_data_keys"] == 2
        assert stats["total_command_result_keys"] == 1
        assert stats["total_storage_bytes"] > 0
        assert "oldest_data_age_seconds" in stats
        assert "newest_data_age_seconds" in stats
    
    def test_get_storage_stats_redis_error(self, manager):
        """Test storage statistics with Redis error."""
        # Mock Redis error
        manager.redis.scan_iter = MagicMock(side_effect=Exception("Redis connection failed"))
        
        stats = manager.get_storage_stats()
        
        # Should return empty dict on error
        assert stats == {}
    
    def test_set_debug_mode(self, manager):
        """Test debug mode setting."""
        # Test enabling debug mode
        manager.set_debug_mode(True)
        assert manager._debug_mode is True
        
        # Test disabling debug mode
        manager.set_debug_mode(False)
        assert manager._debug_mode is False


class TestRedisCommandDataManagerIntegration:
    """Integration tests for RedisCommandDataManager."""
    
    def test_full_command_lifecycle(self):
        """Test complete command data lifecycle."""
        mock_redis = MagicMock()
        manager = RedisCommandDataManager(redis_client=mock_redis, enable_encryption=False)
        
        command_id = "test-command-123"
        command_data = {
            "messages": [{"role": "user", "content": "Test message"}],
            "strategy": "chain_of_thought",
            "conversation_history": [{"role": "assistant", "content": "Previous response"}]
        }
        command_result = {"status": "success", "response": "Test response"}
        
        # Store command data
        data_key = manager.store_command_data(
            command_id, command_data, command_timeout_seconds=120, command_type="agent_turn"
        )
        assert data_key == f"cmd:data:{command_id}"
        
        # Store command result
        result_key = manager.store_command_result(
            command_id, command_result, command_timeout_seconds=120
        )
        assert result_key == f"cmd:result:{command_id}"
        
        # Mock retrieval
        storage_data = {
            "data": command_data,
            "metadata": {"command_type": "reasoning", "stored_at": datetime.utcnow().isoformat()}
        }
        result_storage_data = {
            "result": command_result,
            "metadata": {"command_id": command_id, "stored_at": datetime.utcnow().isoformat()}
        }
        
        mock_redis.get.side_effect = [
            msgpack.packb(storage_data, use_bin_type=True),
            msgpack.packb(result_storage_data, use_bin_type=True)
        ]
        
        # Retrieve command data
        retrieved_data = manager.retrieve_command_data(data_key)
        assert retrieved_data == command_data
        
        # Retrieve command result
        retrieved_result = manager.retrieve_command_result(result_key)
        assert retrieved_result == command_result
    
    def test_large_data_handling(self):
        """Test handling of large command data."""
        mock_redis = MagicMock()
        manager = RedisCommandDataManager(redis_client=mock_redis, enable_encryption=False)
        
        # Create large data
        large_data = {
            "messages": [{"role": "user", "content": "x" * 10000}] * 100,  # Large message array
            "conversation_history": [{"role": "assistant", "content": "y" * 5000}] * 50,
            "metadata": {"large_field": "z" * 20000}
        }
        
        command_id = "large-command-123"
        
        # Store large data
        data_key = manager.store_command_data(
            command_id, large_data, command_timeout_seconds=300, command_type="model_inference"
        )
        
        # Verify storage was called
        mock_redis.setex.assert_called_once()
        call_args = mock_redis.setex.call_args
        
        # Verify TTL (2x timeout = 600 seconds)
        assert call_args[0][1] == 600
        
        # Verify data was serialized with MsgPack
        serialized_data = call_args[0][2]
        deserialized = msgpack.unpackb(serialized_data, raw=False)
        assert deserialized["data"] == large_data
        assert deserialized["metadata"]["command_type"] == "model_inference"


class TestRedisCommandDataManagerPerformance:
    """Performance tests for RedisCommandDataManager."""
    
    def test_serialization_performance(self):
        """Test serialization performance with different data sizes."""
        import time
        mock_redis = MagicMock()
        manager = RedisCommandDataManager(redis_client=mock_redis, enable_encryption=False)
        
        # Test data of different sizes
        test_cases = [
            ("small", {"test": "data"}),
            ("medium", {"messages": [{"content": "x" * 1000}] * 10}),
            ("large", {"messages": [{"content": "x" * 1000}] * 100}),
        ]
        
        for size_name, data in test_cases:
            start_time = time.time()
            
            manager.store_command_data(
                f"test-{size_name}", data, command_type="agent_turn"
            )
            
            end_time = time.time()
            serialization_time = (end_time - start_time) * 1000  # Convert to milliseconds
            
            # Verify reasonable performance (should be much faster than current 50-200ms)
            assert serialization_time < 50, f"{size_name} data took {serialization_time}ms, expected < 50ms"
    
    def test_msgpack_vs_json_efficiency(self):
        """Test MsgPack vs JSON serialization efficiency."""
        import json
        mock_redis = MagicMock()
        manager = RedisCommandDataManager(redis_client=mock_redis, enable_encryption=False)
        
        # Test data
        data = {
            "messages": [{"role": "user", "content": "Test message"}] * 50,
            "conversation_history": [{"role": "assistant", "content": "Response"}] * 20,
            "metadata": {"key": "value", "nested": {"deep": "data"}}
        }
        
        # Serialize with MsgPack (current implementation)
        manager.store_command_data("test-msgpack", data)
        
        # Get MsgPack serialized data
        call_args = mock_redis.setex.call_args
        msgpack_data = call_args[0][2]
        msgpack_size = len(msgpack_data)
        
        # Compare with JSON
        json_data = json.dumps(data).encode('utf-8')
        json_size = len(json_data)
        
        # MsgPack should be smaller than JSON
        assert msgpack_size < json_size, f"MsgPack size: {msgpack_size}, JSON size: {json_size}"
        
        # Calculate efficiency improvement
        efficiency_improvement = (json_size - msgpack_size) / json_size * 100
        assert efficiency_improvement > 10, f"MsgPack should be at least 10% smaller, got {efficiency_improvement:.1f}%"
