"""
Comprehensive tests for Worker Readiness Architecture (ADR-0004)

Tests the complete worker lifecycle, state management, and readiness signaling
as defined in ADR-0004.
"""

import pytest
import time
import json
from unittest.mock import MagicMock, patch
from typing import Dict, Any

from motet.core.distributed.worker_readiness import (
    WorkerReadinessService, 
    WorkerInfo, 
    WorkerState,
    get_readiness_service
)


class TestWorkerState:
    """Test WorkerState enum and transitions"""
    
    def test_worker_state_values(self):
        """Test that all expected worker states exist"""
        assert WorkerState.STARTING.value == "starting"
        assert WorkerState.WARMING.value == "warming" 
        assert WorkerState.READY.value == "ready"
        assert WorkerState.ACCEPTING.value == "accepting"
        assert WorkerState.BUSY.value == "busy"
        assert WorkerState.UNHEALTHY.value == "unhealthy"


class TestWorkerInfo:
    """Test WorkerInfo dataclass and serialization"""
    
    def test_worker_info_creation(self):
        """Test creating WorkerInfo with required fields"""
        worker_info = WorkerInfo(
            worker_id="test-worker-1",
            state=WorkerState.READY,
            capabilities=["model_inference", "tool_execution"],
            last_heartbeat=time.time(),
            warmup_completed=True,
            tool_count=15,
            mcp_tool_count=8
        )
        
        assert worker_info.worker_id == "test-worker-1"
        assert worker_info.state == WorkerState.READY
        assert worker_info.capabilities == ["model_inference", "tool_execution"]
        assert worker_info.warmup_completed is True
        assert worker_info.tool_count == 15
        assert worker_info.mcp_tool_count == 8
    
    def test_worker_info_to_dict(self):
        """Test serialization to dictionary"""
        worker_info = WorkerInfo(
            worker_id="test-worker-1",
            state=WorkerState.READY,
            capabilities=["model_inference"],
            last_heartbeat=1234567890.0,
            warmup_completed=True
        )
        
        data = worker_info.to_dict()
        
        assert data["worker_id"] == "test-worker-1"
        assert data["state"] == "ready"  # Enum converted to string
        assert data["capabilities"] == ["model_inference"]
        assert data["last_heartbeat"] == 1234567890.0
        assert data["warmup_completed"] is True
    
    def test_worker_info_from_dict(self):
        """Test deserialization from dictionary"""
        data = {
            "worker_id": "test-worker-1",
            "state": "ready",
            "capabilities": ["model_inference"],
            "last_heartbeat": 1234567890.0,
            "warmup_completed": True,
            "active_commands": 2,
            "max_concurrency": 6,
            "tool_count": 15,
            "mcp_tool_count": 8,
            "startup_time": 1234567880.0,
            "warmup_duration_ms": 2000
        }
        
        worker_info = WorkerInfo.from_dict(data)
        
        assert worker_info.worker_id == "test-worker-1"
        assert worker_info.state == WorkerState.READY
        assert worker_info.capabilities == ["model_inference"]
        assert worker_info.last_heartbeat == 1234567890.0
        assert worker_info.warmup_completed is True


class _RecordingPipeline:
    """Minimal redis-py pipeline that executes queued HGETALLs against the mock."""

    def __init__(self, redis):
        self._redis = redis
        self._keys = []

    def hgetall(self, key):
        self._keys.append(key)
        return self

    def execute(self):
        return [self._redis.hgetall(key) for key in self._keys]


@pytest.fixture
def mock_redis():
    """Mock Redis client for testing (synchronous)"""
    redis_mock = MagicMock()
    redis_mock.hset = MagicMock(return_value=1)
    redis_mock.hgetall = MagicMock(return_value={})
    redis_mock.hincrby = MagicMock(return_value=1)
    redis_mock.hget = MagicMock(return_value="0")
    redis_mock.sadd = MagicMock(return_value=1)
    redis_mock.srem = MagicMock(return_value=1)
    redis_mock.smembers = MagicMock(return_value=set())
    redis_mock.delete = MagicMock(return_value=1)
    redis_mock.keys = MagicMock(return_value=[])
    redis_mock.expire = MagicMock(return_value=True)
    redis_mock.exists = MagicMock(return_value=False)
    redis_mock.close = MagicMock()
    redis_mock.pipeline = lambda transaction=False: _RecordingPipeline(redis_mock)
    return redis_mock


@pytest.fixture
def readiness_service(mock_redis):
    """Create WorkerReadinessService with mocked Redis (sync)."""
    with patch('motet.core.distributed.worker_readiness.get_sync_redis_client', return_value=mock_redis):
        service = WorkerReadinessService()
        service.redis_client = mock_redis
        return service


class TestWorkerReadinessService:
    """Test WorkerReadinessService core functionality (sync)."""

    def test_service_initialization(self, mock_redis):
        """Test service initialization with distributed lock key."""
        with patch('motet.core.distributed.worker_readiness.get_sync_redis_client', return_value=mock_redis):
            service = WorkerReadinessService()
        assert service._validation_lock_key == "lock:worker:readiness"
        assert service.WORKERS_KEY_PREFIX == "worker:registration:"
        assert service.REGISTERED_WORKERS_SET == "worker:registered"
        assert service.READY_WORKERS_SET == "worker:ready"
        assert service.WORKER_HEARTBEAT_TTL == 120
        assert service.redis_client is not None

    def test_register_worker(self, readiness_service, mock_redis):
        """Test worker registration in STARTING state (sync)."""
        worker_id = "test-worker-1"
        capabilities = ["model_inference", "tool_execution"]
        with patch.object(readiness_service, '_cleanup_stale_worker_entries'):
            with patch('motet.core.distributed.redis_manager.store_structured_data_sync') as mock_store:
                readiness_service.register_worker(worker_id, capabilities)
                mock_store.assert_called_once()
                call_args = mock_store.call_args
                assert call_args[0][0] == "worker_readiness"
                assert call_args[0][1] == f"worker:registration:{worker_id}"
                stored_data = call_args[0][2]
                assert stored_data["worker_id"] == worker_id
                assert stored_data["state"] == "starting"
                assert stored_data["capabilities"] == capabilities
                assert stored_data["warmup_completed"] is False
                mock_redis.sadd.assert_called_with("worker:registered", worker_id)
    
    def test_update_worker_state(self, readiness_service, mock_redis):
        """Test updating worker state (sync)."""
        worker_id = "test-worker-1"
        mock_worker_info = WorkerInfo(
            worker_id=worker_id,
            state=WorkerState.STARTING,
            capabilities=["model_inference"],
            last_heartbeat=1234567890.0,
            warmup_completed=False,
            active_commands=0,
            max_concurrency=6,
            tool_count=0,
            mcp_tool_count=0,
            startup_time=1234567880.0,
            warmup_duration_ms=0
        )
        with patch.object(readiness_service, 'get_worker_info', return_value=mock_worker_info):
            with patch('motet.core.distributed.redis_manager.store_structured_data_sync') as mock_store:
                with patch('motet.core.distributed.redis_manager.get_sync_redis_client', return_value=mock_redis):
                    readiness_service.update_worker_state(
                        worker_id,
                        WorkerState.READY,
                        tool_count=15,
                        warmup_completed=True
                    )
                mock_store.assert_called_once()
                stored_data = mock_store.call_args[0][2]
                assert stored_data["state"] == "ready"
                assert stored_data["tool_count"] == 15
                assert stored_data["warmup_completed"] is True
                mock_redis.sadd.assert_called_with("worker:ready", worker_id)
    
    def test_mark_worker_ready(self, readiness_service, mock_redis):
        """Test marking worker as ready (sync)."""
        worker_id = "test-worker-1"
        mock_worker_info = WorkerInfo(
            worker_id=worker_id,
            state=WorkerState.WARMING,
            capabilities=["model_inference"],
            last_heartbeat=1234567890.0,
            warmup_completed=False,
            active_commands=0,
            max_concurrency=6,
            tool_count=0,
            mcp_tool_count=0,
            startup_time=1234567880.0,
            warmup_duration_ms=0
        )
        with patch.object(readiness_service, 'get_worker_info', return_value=mock_worker_info):
            with patch('motet.core.distributed.redis_manager.store_structured_data_sync') as mock_store:
                with patch('motet.core.distributed.redis_manager.get_sync_redis_client', return_value=mock_redis):
                    readiness_service.mark_worker_ready(
                        worker_id=worker_id,
                        tool_count=15,
                        mcp_tool_count=8,
                        warmup_duration_ms=2000
                    )
                mock_store.assert_called_once()
                stored_data = mock_store.call_args[0][2]
                assert stored_data["state"] == "ready"
                assert stored_data["warmup_completed"] is True
                assert stored_data["tool_count"] == 15
                assert stored_data["mcp_tool_count"] == 8
                assert stored_data["warmup_duration_ms"] == 2000
                mock_redis.sadd.assert_called_with("worker:ready", worker_id)

    def test_get_ready_workers(self, readiness_service, mock_redis):
        """Test getting list of ready workers (sync)."""
        mock_redis.smembers.return_value = {b"worker-1", b"worker-2", b"worker-3"}
        ready_workers = readiness_service.get_ready_workers()
        assert len(ready_workers) == 3
        assert "worker-1" in ready_workers
        assert "worker-2" in ready_workers
        assert "worker-3" in ready_workers
        mock_redis.smembers.assert_called_with("worker:ready")

    def test_get_ready_workers_with_capabilities(self, readiness_service, mock_redis):
        """Test getting ready workers filtered by capabilities (sync)."""
        mock_redis.smembers.return_value = {"worker-1", "worker-2"}
        def mock_get_worker_info(worker_id):
            if worker_id == "worker-1":
                return WorkerInfo(
                    worker_id="worker-1",
                    state=WorkerState.READY,
                    capabilities=["model_inference", "tool_execution"],
                    last_heartbeat=1234567890.0,
                    warmup_completed=True,
                    active_commands=0,
                    max_concurrency=6,
                    tool_count=15,
                    mcp_tool_count=8,
                    startup_time=1234567880.0,
                    warmup_duration_ms=2000
                )
            elif worker_id == "worker-2":
                return WorkerInfo(
                    worker_id="worker-2",
                    state=WorkerState.READY,
                    capabilities=["model_inference"],
                    last_heartbeat=1234567890.0,
                    warmup_completed=True,
                    active_commands=0,
                    max_concurrency=6,
                    tool_count=10,
                    mcp_tool_count=5,
                    startup_time=1234567880.0,
                    warmup_duration_ms=1800
                )
            return None
        with patch.object(readiness_service, 'get_worker_info', side_effect=mock_get_worker_info):
            ready_workers = readiness_service.get_ready_workers(["model_inference"])
            assert len(ready_workers) == 2
            ready_workers = readiness_service.get_ready_workers(["tool_execution"])
            assert len(ready_workers) == 1
            assert "worker-1" in ready_workers

    def test_get_worker_info(self, readiness_service, mock_redis):
        """Test getting detailed worker information (sync)."""
        worker_id = "test-worker-1"
        mock_worker_data = {
            "worker_id": "test-worker-1",
            "state": "ready",
            "capabilities": ["model_inference", "tool_execution"],
            "last_heartbeat": 1234567890.0,
            "warmup_completed": True,
            "active_commands": 2,
            "max_concurrency": 6,
            "tool_count": 15,
            "mcp_tool_count": 8,
            "startup_time": 1234567880.0,
            "warmup_duration_ms": 2000
        }
        with patch('motet.core.distributed.redis_manager.retrieve_structured_data_sync', return_value=mock_worker_data):
            worker_info = readiness_service.get_worker_info(worker_id)
        assert worker_info is not None
        assert worker_info.worker_id == "test-worker-1"
        assert worker_info.state == WorkerState.READY
        assert worker_info.capabilities == ["model_inference", "tool_execution"]
        assert worker_info.warmup_completed is True
        assert worker_info.tool_count == 15
        assert worker_info.mcp_tool_count == 8

    def test_get_worker_info_not_found(self, readiness_service, mock_redis):
        """Test getting worker info for non-existent worker (sync)."""
        with patch('motet.core.distributed.redis_manager.retrieve_structured_data_sync', return_value=None):
            worker_info = readiness_service.get_worker_info("non-existent")
        assert worker_info is None

    def test_remove_worker(self, readiness_service, mock_redis):
        """Test removing worker from registry (sync)."""
        worker_id = "test-worker-1"
        readiness_service.remove_worker(worker_id)
        mock_redis.delete.assert_called_with(f"worker:registration:{worker_id}")
        mock_redis.srem.assert_any_call("worker:ready", worker_id)
        mock_redis.srem.assert_any_call("worker:registered", worker_id)

    def test_get_all_workers(self, readiness_service, mock_redis):
        """Test getting all registered workers including non-ready (sync)."""
        mock_redis.smembers.return_value = {"worker-1", "worker-2"}
        # get_all_workers uses redis_client.hgetall(key); _convert_worker_data_types + WorkerInfo.from_dict
        worker1_data = {
            "worker_id": "worker-1", "state": "ready", "capabilities": "[\"model_inference\"]",
            "last_heartbeat": "1234567890.0", "warmup_completed": "True", "active_commands": "0",
            "max_concurrency": "6", "tool_count": "10", "mcp_tool_count": "5",
            "startup_time": "1234567880.0", "warmup_duration_ms": "1500"
        }
        worker2_data = {
            "worker_id": "worker-2", "state": "busy", "capabilities": "[\"tool_execution\"]",
            "last_heartbeat": "1234567890.0", "warmup_completed": "True", "active_commands": "3",
            "max_concurrency": "6", "tool_count": "10", "mcp_tool_count": "5",
            "startup_time": "1234567880.0", "warmup_duration_ms": "1800"
        }
        mock_redis.hgetall.side_effect = lambda key: {
            "worker:registration:worker-1": worker1_data,
            "worker:registration:worker-2": worker2_data,
        }.get(key, {})
        all_workers = readiness_service.get_all_workers()
        assert len(all_workers) == 2
        assert "worker-1" in all_workers
        assert "worker-2" in all_workers
        assert all_workers["worker-1"].state == WorkerState.READY
        assert all_workers["worker-2"].state == WorkerState.BUSY
        mock_redis.smembers.assert_called_with("worker:registered")
        mock_redis.keys.assert_not_called()

    def test_get_all_workers_skips_missing_hashes(self, readiness_service, mock_redis):
        """Stale membership entries with no hash are skipped and removed."""
        mock_redis.smembers.return_value = {b"worker-1", b"ghost"}
        worker1_data = {
            "worker_id": "worker-1", "state": "warming", "capabilities": "[\"model_inference\"]",
            "last_heartbeat": "1234567890.0", "warmup_completed": "False", "active_commands": "0",
            "max_concurrency": "6", "tool_count": "0", "mcp_tool_count": "0",
            "startup_time": "1234567880.0", "warmup_duration_ms": "0"
        }
        mock_redis.hgetall.side_effect = lambda key: worker1_data if key.endswith("worker-1") else {}
        all_workers = readiness_service.get_all_workers()
        assert list(all_workers.keys()) == ["worker-1"]
        assert all_workers["worker-1"].state == WorkerState.WARMING
        mock_redis.srem.assert_any_call("worker:registered", "ghost")
        mock_redis.srem.assert_any_call("worker:ready", "ghost")
        mock_redis.keys.assert_not_called()

    def test_cleanup_prunes_registered_set(self, readiness_service, mock_redis):
        """Cleanup drops ready and registered members whose hash is gone."""
        def _smembers(key):
            if key == "worker:ready":
                return {"stale-ready"}
            if key == "worker:registered":
                return {"stale-registered", "live-worker"}
            return set()

        mock_redis.smembers.side_effect = _smembers
        mock_redis.exists.side_effect = lambda key: key == "worker:registration:live-worker"
        readiness_service._cleanup_stale_worker_entries()
        mock_redis.srem.assert_any_call("worker:ready", "stale-ready")
        mock_redis.srem.assert_any_call("worker:registered", "stale-registered")

    def test_increment_active_commands_skips_missing_hash(self, readiness_service, mock_redis):
        """HINCRBY/HSET must not invent a registration hash."""
        mock_redis.exists.return_value = False
        readiness_service.increment_active_commands("ghost")
        mock_redis.hincrby.assert_not_called()
        mock_redis.hset.assert_not_called()

    def test_decrement_active_commands_skips_missing_hash(self, readiness_service, mock_redis):
        mock_redis.exists.return_value = False
        readiness_service.decrement_active_commands("ghost")
        mock_redis.hincrby.assert_not_called()
        mock_redis.hset.assert_not_called()

    def test_increment_active_commands_heals_membership(self, readiness_service, mock_redis):
        mock_redis.exists.return_value = True
        mock_redis.hincrby.return_value = 2
        readiness_service.increment_active_commands("worker-1")
        mock_redis.hincrby.assert_called_with("worker:registration:worker-1", "active_commands", 1)
        mock_redis.sadd.assert_called_with("worker:registered", "worker-1")


class TestWorkerLifecycle:
    """Test complete worker lifecycle as defined in ADR-0004 (sync)."""

    def test_complete_worker_lifecycle(self, readiness_service, mock_redis):
        """Test the complete worker lifecycle: STARTING → WARMING → READY (sync)."""
        worker_id = "lifecycle-test-worker"
        capabilities = ["model_inference", "tool_execution"]
        with patch.object(readiness_service, '_cleanup_stale_worker_entries'):
            with patch('motet.core.distributed.redis_manager.store_structured_data_sync') as mock_store:
                readiness_service.register_worker(worker_id, capabilities, max_concurrency=6)
                mock_store.assert_called()
                stored_data = mock_store.call_args[0][2]
                assert stored_data["state"] == "starting"
                assert stored_data["warmup_completed"] is False
        assert mock_store.call_count >= 1


class TestDistributedLockOptimization:
    """Test the distributed lock optimization for validation operations (sync)."""

    def test_validate_ready_workers_set_no_changes_needed(self, readiness_service, mock_redis):
        """Test validation when no changes are needed (no lock acquired)."""
        mock_redis.smembers.return_value = {"worker-1", "worker-2"}
        mock_workers = {
            "worker-1": WorkerInfo(worker_id="worker-1", state=WorkerState.READY, capabilities=["model_inference"], last_heartbeat=time.time(), warmup_completed=True),
            "worker-2": WorkerInfo(worker_id="worker-2", state=WorkerState.ACCEPTING, capabilities=["tool_execution"], last_heartbeat=time.time(), warmup_completed=True)
        }
        with patch.object(readiness_service, 'get_all_workers', return_value=mock_workers):
            result = readiness_service._validate_ready_workers_set()
        assert result["total_workers"] == 2
        assert result["ready_set_size"] == 2
        assert result["should_be_ready"] == 2
        assert result["missing_from_set"] == 0
        assert result["stale_in_set"] == 0
        assert result["validation_fixed"] is False

    def test_validate_ready_workers_set_decodes_bytes_members(self, readiness_service, mock_redis):
        """Bytes set members must compare as the same worker ids as hashes."""
        mock_redis.smembers.return_value = {b"worker-1"}
        mock_workers = {
            "worker-1": WorkerInfo(
                worker_id="worker-1",
                state=WorkerState.READY,
                capabilities=["model_inference"],
                last_heartbeat=time.time(),
                warmup_completed=True,
            ),
        }
        with patch.object(readiness_service, 'get_all_workers', return_value=mock_workers):
            result = readiness_service._validate_ready_workers_set()
        assert result["missing_from_set"] == 0
        assert result["stale_in_set"] == 0
        assert result["validation_fixed"] is False

    def test_validate_ready_workers_set_with_fixes_needed(self, readiness_service, mock_redis):
        """Test validation when fixes are needed (lock acquired for writes)."""
        mock_redis.smembers.return_value = {"worker-1", "stale-worker"}
        mock_workers = {
            "worker-1": WorkerInfo(worker_id="worker-1", state=WorkerState.READY, capabilities=["model_inference"], last_heartbeat=time.time(), warmup_completed=True),
            "worker-2": WorkerInfo(worker_id="worker-2", state=WorkerState.READY, capabilities=["tool_execution"], last_heartbeat=time.time(), warmup_completed=True)
        }
        mock_lock = MagicMock()
        with patch.object(readiness_service, 'get_all_workers', return_value=mock_workers):
            with patch('motet.core.distributed.redis_manager.acquire_distributed_lock_sync') as mock_acquire_lock:
                mock_acquire_lock.return_value = mock_lock
                result = readiness_service._validate_ready_workers_set()
        mock_acquire_lock.assert_called_once()
        mock_redis.sadd.assert_called_once_with("worker:ready", "worker-2")
        mock_redis.srem.assert_called_once_with("worker:ready", "stale-worker")
        mock_lock.release_sync.assert_called_once()
        assert result["total_workers"] == 2
        assert result["missing_from_set"] == 1
        assert result["stale_in_set"] == 1
        assert result["validation_fixed"] is True

    def test_validate_ready_workers_set_lock_acquisition_failure(self, readiness_service, mock_redis):
        """Test validation when distributed lock cannot be acquired."""
        mock_redis.smembers.return_value = {"worker-1"}
        mock_workers = {
            "worker-2": WorkerInfo(worker_id="worker-2", state=WorkerState.READY, capabilities=["tool_execution"], last_heartbeat=time.time(), warmup_completed=True)
        }
        with patch.object(readiness_service, 'get_all_workers', return_value=mock_workers):
            with patch('motet.core.distributed.redis_manager.acquire_distributed_lock_sync', return_value=None):
                result = readiness_service._validate_ready_workers_set()
        assert "error" in result
        assert "Could not acquire validation lock" in result["error"]
        assert result["validation_skipped"] is True

    def test_apply_ready_set_fixes_success(self, readiness_service, mock_redis):
        """Test the helper method for applying fixes with distributed lock (sync)."""
        missing_workers = {"worker-2", "worker-3"}
        stale_workers = {"stale-worker"}
        mock_lock = MagicMock()
        with patch('motet.core.distributed.redis_manager.acquire_distributed_lock_sync') as mock_acquire_lock:
            mock_acquire_lock.return_value = mock_lock
            result = readiness_service._apply_ready_set_fixes(missing_workers, stale_workers)
        mock_acquire_lock.assert_called_once()
        mock_redis.sadd.assert_called_once()
        call_args = mock_redis.sadd.call_args[0]
        assert call_args[0] == "worker:ready"
        assert set(call_args[1:]) == {"worker-2", "worker-3"}
        mock_redis.srem.assert_called_once_with("worker:ready", "stale-worker")
        mock_lock.release_sync.assert_called_once()
        assert result is True

    def test_apply_ready_set_fixes_lock_failure(self, readiness_service, mock_redis):
        """Test the helper method when lock acquisition fails (sync)."""
        missing_workers = {"worker-2"}
        stale_workers = {"stale-worker"}
        with patch('motet.core.distributed.redis_manager.acquire_distributed_lock_sync', return_value=None):
            result = readiness_service._apply_ready_set_fixes(missing_workers, stale_workers)
        assert result is False
        mock_redis.sadd.assert_not_called()
        mock_redis.srem.assert_not_called()


class TestWorkerReadinessIntegration:
    """Integration tests for worker readiness (sync)."""

    def test_get_readiness_service_singleton(self, mock_redis):
        """Test that get_readiness_service returns singleton instance (sync)."""
        import motet.core.distributed.worker_readiness as wr
        with patch('motet.core.distributed.worker_readiness.get_sync_redis_client', return_value=mock_redis):
            orig = getattr(wr, '_readiness_service', None)
            wr._readiness_service = None
            try:
                service1 = get_readiness_service()
                service2 = get_readiness_service()
                assert service1 is service2
            finally:
                wr._readiness_service = orig

    def test_redis_connection_failure(self, mock_redis):
        """Test handling of Redis connection failures (sync)."""
        with patch('motet.core.distributed.worker_readiness.get_sync_redis_client', return_value=mock_redis):
            service = WorkerReadinessService()
        assert service.redis_client is not None
        assert service._validation_lock_key == "lock:worker:readiness"


class TestWorkerReadinessErrorHandling:
    """Test error handling and edge cases (sync)."""

    def test_malformed_worker_data(self, readiness_service, mock_redis):
        """Test handling of malformed worker data in Redis (sync). Implementation returns None when corrupted."""
        with patch('motet.core.distributed.redis_manager.retrieve_structured_data_sync') as mock_retrieve:
            mock_retrieve.return_value = {"worker_id": "malformed-worker"}
            worker_info = readiness_service.get_worker_info("malformed-worker")
        assert worker_info is None

    def test_concurrent_worker_updates(self, readiness_service, mock_redis):
        """Test concurrent updates to worker state (sync)."""
        worker_id = "concurrent-test-worker"
        mock_worker_info = WorkerInfo(
            worker_id=worker_id,
            state=WorkerState.READY,
            capabilities=["model_inference"],
            last_heartbeat=time.time(),
            warmup_completed=True,
            active_commands=0,
            max_concurrency=6,
            tool_count=15,
            mcp_tool_count=8,
            startup_time=time.time(),
            warmup_duration_ms=2000
        )
        with patch.object(readiness_service, 'get_worker_info', return_value=mock_worker_info):
            with patch('motet.core.distributed.redis_manager.store_structured_data_sync') as mock_store:
                for i in range(5):
                    readiness_service.update_worker_state(
                        worker_id,
                        WorkerState.ACCEPTING,
                        active_commands=i
                    )
                assert mock_store.call_count >= 5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
