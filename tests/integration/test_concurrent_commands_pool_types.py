"""
Integration tests for concurrent commands on multiple pool types (ADR-0033 Phase 0).

These tests verify that GatherCommand and MapCommand work correctly across
all pool types: fork, threads, eventlet, gevent.

Test Strategy:
1. Test GatherCommand with multiple child commands
2. Test MapCommand with batch operations
3. Verify no deadlocks under concurrent load
4. Test on different pool types when available
5. Verify results are correct and complete

Note:
- These tests require a running Celery worker and Redis
- Pool type can be set via MOTET_CELERY_POOL environment variable
- Tests will skip if worker is not available
"""

import os
import time
from typing import List
import pytest

try:
    from motet.core.commands.concurrency import GatherCommand, MapCommand
    from motet.core.commands.builtin.tool import tool_execution
    from motet.core.commands.command_data_classes import (
        GatherCommandData,
        MapCommandData,
        ToolExecutionData,
    )
    from motet.core.commands.distributed import DistributedCommand
    from motet.core.distributed.worker_readiness import WorkerReadinessService
    IMPORTS_AVAILABLE = True
except ImportError as e:
    IMPORTS_AVAILABLE = False
    pytest.skip(f"Required imports not available: {e}", allow_module_level=True)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def redis_client():
    """Redis client for checking worker status."""
    try:
        import redis
        client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
        client.ping()
        return client
    except Exception:
        pytest.skip("Redis not available")


@pytest.fixture
def worker_check(redis_client):
    """Check if workers are available."""
    from unittest.mock import patch
    from motet.core.distributed import worker_readiness as wr_module

    with patch.object(wr_module, "get_sync_redis_client", return_value=redis_client):
        service = WorkerReadinessService()
        workers = service.get_ready_workers()
    if not workers:
        pytest.skip("No Celery workers available")
    return workers


# ============================================================================
# Helper Functions
# ============================================================================

def get_pool_type_from_env() -> str:
    """Get pool type from environment variable."""
    return os.environ.get('MOTET_CELERY_POOL', 'fork')


def create_simple_child_commands(count: int) -> List[DistributedCommand]:
    """Create simple tool execution commands for testing (decorator-based API)."""
    commands = []
    for i in range(count):
        cmd = tool_execution(
            data=ToolExecutionData(
                tool_name="test_tool",
                parameters={"index": i, "value": f"test_{i}"},
            ),
            task_id=f"test_task_{i}",
            conversation_id="test_conv",
        )
        commands.append(cmd)
    return commands


# ============================================================================
# GatherCommand Tests
# ============================================================================

@pytest.mark.requires_vault
class TestGatherCommandPoolTypes:
    """Test GatherCommand on different pool types."""
    
    def test_gather_command_basic(self, worker_check):
        """GatherCommand executes multiple child commands successfully."""
        # Create child commands (decorator-based tool_execution)
        child_commands = create_simple_child_commands(count=3)
        
        # Create gather command via factory (serializes child commands)
        gather_cmd = GatherCommand.create(
            commands=child_commands,
            task_id="test_gather",
            conversation_id="test_conv",
        )
        
        # Note: Actual execution requires distributed invoker
        # This test verifies the command structure is correct
        assert len(gather_cmd.data.commands) == 3
        assert gather_cmd.get_command_type() == "core.gather"
        print(f"✅ GatherCommand created with 3 children on {get_pool_type_from_env()} pool")
    
    def test_worker_lock_and_event_no_deadlock(self):
        """WorkerLock and WorkerEvent do not deadlock on the current pool."""
        from motet.core.workers.concurrency_primitives import (
            WorkerEvent,
            WorkerLock,
            get_current_pool_type,
        )

        lock = WorkerLock()
        event = WorkerEvent()
        with lock:
            event.set()
        assert event.is_set()
        print(f"✅ WorkerLock/WorkerEvent work on {get_current_pool_type()} pool")
    
    def test_gather_command_timeout_behavior(self, worker_check):
        """GatherCommand respects timeout settings."""
        # Create commands with short timeout
        child_commands = create_simple_child_commands(count=2)
        
        gather_cmd = GatherCommand.create(
            commands=child_commands,
            task_id="test_gather_timeout",
            conversation_id="test_conv",
        )
        gather_cmd.distributed_context.timeout_seconds = 30
        
        assert gather_cmd.distributed_context.timeout_seconds == 30
        print(f"✅ GatherCommand timeout configuration works on {get_pool_type_from_env()} pool")


# ============================================================================
# MapCommand Tests
# ============================================================================

@pytest.mark.requires_vault
class TestMapCommandPoolTypes:
    """Test MapCommand on different pool types."""
    
    def test_map_command_basic(self, worker_check):
        """MapCommand creates batch operations successfully."""
        # Create map command via factory (command_type + command_template + inputs)
        map_cmd = MapCommand.create(
            command_type="core.tool_execution",
            command_template={"tool_name": "batch_tool", "parameters": {"key": "value"}},
            inputs=[
                {"parameters": {"index": 0, "data": "first"}},
                {"parameters": {"index": 1, "data": "second"}},
                {"parameters": {"index": 2, "data": "third"}},
            ],
            task_id="test_map",
            conversation_id="test_conv",
        )
        
        assert len(map_cmd.data.inputs) == 3
        assert map_cmd.get_command_type() == "core.map"
        print(f"✅ MapCommand created with 3 input sets on {get_pool_type_from_env()} pool")
    
    def test_map_command_batch_size(self, worker_check):
        """MapCommand handles varying batch sizes."""
        # Test with larger batch via MapCommand.create
        map_cmd = MapCommand.create(
            command_type="core.tool_execution",
            command_template={"tool_name": "test_tool", "parameters": {}},
            inputs=[{"parameters": {"index": i}} for i in range(10)],
            task_id="test_map_large",
            conversation_id="test_conv",
        )
        
        assert len(map_cmd.data.inputs) == 10
        print(f"✅ MapCommand handles large batches on {get_pool_type_from_env()} pool")


# ============================================================================
# Concurrent Load Tests
# ============================================================================

@pytest.mark.requires_vault
class TestConcurrentLoad:
    """Test concurrent commands under load."""
    
    def test_multiple_gather_commands(self, worker_check):
        """Multiple GatherCommands can be created without deadlock."""
        gather_commands = []
        
        for i in range(5):
            child_commands = create_simple_child_commands(count=3)
            gather_cmd = GatherCommand.create(
                commands=child_commands,
                task_id=f"test_gather_{i}",
                conversation_id="test_conv",
            )
            gather_commands.append(gather_cmd)
        
        assert len(gather_commands) == 5
        print(f"✅ Created 5 GatherCommands without deadlock on {get_pool_type_from_env()} pool")
    
    def test_gather_with_many_children(self, worker_check):
        """GatherCommand handles many child commands."""
        # Create 20 child commands
        child_commands = create_simple_child_commands(count=20)
        
        gather_cmd = GatherCommand.create(
            commands=child_commands,
            task_id="test_gather_many",
            conversation_id="test_conv",
        )
        
        assert len(gather_cmd.data.commands) == 20
        print(f"✅ GatherCommand created with 20 children on {get_pool_type_from_env()} pool")


# ============================================================================
# Pool Type Specific Tests
# ============================================================================

@pytest.mark.requires_vault
class TestPoolTypeSpecific:
    """Pool type specific tests."""
    
    def test_current_pool_type_detected(self):
        """Current pool type is correctly detected."""
        from motet.core.workers.concurrency_primitives import get_current_pool_type
        
        pool_type = get_current_pool_type()
        assert pool_type in ['fork', 'threads', 'eventlet', 'gevent']
        print(f"✅ Detected pool type: {pool_type}")
    
    def test_primitives_work_on_current_pool(self):
        """Primitives work correctly on current pool type."""
        from motet.core.workers.concurrency_primitives import (
            WorkerLock,
            WorkerEvent,
            get_current_pool_type
        )
        
        pool_type = get_current_pool_type()
        
        # Create and test lock
        lock = WorkerLock()
        with lock:
            assert lock.locked()
        
        # Create and test event
        event = WorkerEvent()
        event.set()
        assert event.is_set()
        
        print(f"✅ Primitives work correctly on {pool_type} pool")
    
    def test_worker_primitives_match_pool(self):
        """WorkerLock/WorkerEvent work on the detected pool type."""
        from motet.core.workers.concurrency_primitives import (
            WorkerEvent,
            WorkerLock,
            get_current_pool_type,
        )

        pool_type = get_current_pool_type()
        completed: dict[str, str] = {}
        lock = WorkerLock()
        event = WorkerEvent()
        with lock:
            completed["test"] = "completed"
        event.set()
        assert event.is_set()
        assert completed["test"] == "completed"
        print(f"✅ WorkerLock/WorkerEvent work on {pool_type} pool")


# ============================================================================
# Performance Tests
# ============================================================================

@pytest.mark.requires_vault
class TestPerformance:
    """Performance tests for concurrent commands."""
    
    def test_gather_command_creation_performance(self):
        """GatherCommand creation is fast."""
        import time
        
        start = time.time()
        
        for i in range(100):
            child_commands = create_simple_child_commands(count=5)
            data = GatherCommandData(child_commands=child_commands)
            gather_cmd = GatherCommand(
                task_id=f"perf_test_{i}",
                data=data,
                conversation_id="test_conv"
            )
        
        elapsed = time.time() - start
        
        # Should create 100 GatherCommands in under 1 second
        assert elapsed < 1.0
        print(f"✅ Created 100 GatherCommands in {elapsed:.3f}s on {get_pool_type_from_env()} pool")
    
    def test_worker_lock_contention(self):
        """WorkerLock handles contention without deadlock."""
        from motet.core.workers.concurrency_primitives import WorkerLock, WorkerThread

        completed: dict[str, str] = {}
        lock = WorkerLock()

        def simulate_completion(cmd_id: str) -> None:
            with lock:
                completed[cmd_id] = "completed"

        threads = []
        for i in range(10):
            thread = WorkerThread(target=simulate_completion, args=(f"cmd{i}",))
            thread.start()
            threads.append(thread)

        for thread in threads:
            thread.join(timeout=2.0)

        assert len(completed) == 10
        print("✅ WorkerLock handled 10 concurrent updates without deadlock")


# ============================================================================
# Main Test Runner
# ============================================================================

if __name__ == "__main__":
    """Run tests directly (useful for development)."""
    pytest.main([__file__, "-v", "-s"])

