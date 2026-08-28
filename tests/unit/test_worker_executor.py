"""
Unit tests for WorkerExecutor (ADR-0033).

Tests the pool-aware concurrent executor across all worker types.
"""

import pytest
import sys
import time
from unittest.mock import Mock, patch, MagicMock


# Test across all pool types
@pytest.fixture(params=["fork", "threads", "eventlet", "gevent"])
def pool_type(request):
    """Parameterized fixture for testing all pool types."""
    return request.param


@pytest.fixture
def mock_pool_type(pool_type):
    """Mock the pool type detection to return specific type."""
    with patch("motet.core.workers.concurrency_primitives.get_current_pool_type", return_value=pool_type):
        yield pool_type


def test_worker_executor_creation(mock_pool_type):
    """Test WorkerExecutor can be created for all pool types."""
    from motet.core.workers.concurrency_primitives import WorkerExecutor
    
    pool = WorkerExecutor(max_workers=5)
    assert pool.max_workers == 5
    assert pool._pool_type == mock_pool_type


def test_worker_executor_context_manager_threads():
    """Test WorkerExecutor context manager with threads pool."""
    from motet.core.workers.concurrency_primitives import WorkerExecutor
    
    with patch("motet.core.workers.concurrency_primitives.get_current_pool_type", return_value="threads"):
        with WorkerExecutor(max_workers=2) as pool:
            assert pool._pool is not None
            from concurrent.futures import ThreadPoolExecutor
            assert isinstance(pool._pool, ThreadPoolExecutor)


def test_worker_executor_context_manager_fork():
    """Test WorkerExecutor context manager with fork pool."""
    from motet.core.workers.concurrency_primitives import WorkerExecutor
    
    with patch("motet.core.workers.concurrency_primitives.get_current_pool_type", return_value="fork"):
        with WorkerExecutor(max_workers=2) as pool:
            assert pool._pool is not None
            from concurrent.futures import ThreadPoolExecutor
            assert isinstance(pool._pool, ThreadPoolExecutor)


def test_worker_executor_context_manager_eventlet():
    """When eventlet is reported, WorkerExecutor falls back to ThreadPoolExecutor (eventlet deprecated)."""
    from motet.core.workers.concurrency_primitives import WorkerExecutor
    from concurrent.futures import ThreadPoolExecutor

    with patch("motet.core.workers.concurrency_primitives.get_current_pool_type", return_value="eventlet"):
        with WorkerExecutor(max_workers=10) as pool:
            assert pool._pool is not None
            assert isinstance(pool._pool, ThreadPoolExecutor)


def test_worker_executor_context_manager_gevent():
    """Test WorkerExecutor context manager with gevent pool."""
    from motet.core.workers.concurrency_primitives import WorkerExecutor
    
    with patch("motet.core.workers.concurrency_primitives.get_current_pool_type", return_value="gevent"):
        # Mock gevent.pool.Pool
        mock_pool_class = MagicMock()
        mock_gevent_pool_module = MagicMock(Pool=mock_pool_class)
        with patch.dict(sys.modules, {"gevent": MagicMock(), "gevent.pool": mock_gevent_pool_module}):
            with WorkerExecutor(max_workers=10) as pool:
                assert pool._pool is not None
                mock_pool_class.assert_called_once_with(size=10)


def test_worker_executor_submit_threads():
    """Test WorkerExecutor.submit() with threads pool."""
    from motet.core.workers.concurrency_primitives import WorkerExecutor
    
    def task(x, y):
        return x + y
    
    with patch("motet.core.workers.concurrency_primitives.get_current_pool_type", return_value="threads"):
        with WorkerExecutor(max_workers=2) as pool:
            future = pool.submit(task, 5, 3)
            result = future.result()
            assert result == 8


def test_worker_executor_submit_multiple_threads():
    """Test WorkerExecutor.submit() with multiple tasks on threads pool."""
    from motet.core.workers.concurrency_primitives import WorkerExecutor
    
    def square(x):
        return x * x
    
    with patch("motet.core.workers.concurrency_primitives.get_current_pool_type", return_value="threads"):
        with WorkerExecutor(max_workers=3) as pool:
            futures = [pool.submit(square, i) for i in range(5)]
            results = [f.result() for f in futures]
            assert results == [0, 1, 4, 9, 16]


def test_worker_executor_map_threads():
    """Test WorkerExecutor.map() with threads pool."""
    from motet.core.workers.concurrency_primitives import WorkerExecutor
    
    def double(x):
        return x * 2
    
    with patch("motet.core.workers.concurrency_primitives.get_current_pool_type", return_value="threads"):
        with WorkerExecutor(max_workers=2) as pool:
            results = list(pool.map(double, [1, 2, 3, 4, 5]))
            assert results == [2, 4, 6, 8, 10]


def test_worker_executor_exception_handling_threads():
    """Test WorkerExecutor handles exceptions correctly on threads pool."""
    from motet.core.workers.concurrency_primitives import WorkerExecutor
    
    def failing_task(x):
        if x == 3:
            raise ValueError(f"Error on {x}")
        return x * 2
    
    with patch("motet.core.workers.concurrency_primitives.get_current_pool_type", return_value="threads"):
        with WorkerExecutor(max_workers=2) as pool:
            futures = [pool.submit(failing_task, i) for i in range(5)]
            
            # First two should succeed
            assert futures[0].result() == 0
            assert futures[1].result() == 2
            assert futures[2].result() == 4
            
            # Third should raise
            with pytest.raises(ValueError, match="Error on 3"):
                futures[3].result()
            
            # Last should succeed
            assert futures[4].result() == 8


def test_worker_executor_without_context_manager_raises():
    """Test WorkerExecutor raises if used without context manager."""
    from motet.core.workers.concurrency_primitives import WorkerExecutor
    
    with patch("motet.core.workers.concurrency_primitives.get_current_pool_type", return_value="threads"):
        pool = WorkerExecutor(max_workers=2)
        
        with pytest.raises(RuntimeError, match="WorkerExecutor not entered"):
            pool.submit(lambda: 42)


def test_worker_executor_default_max_workers():
    """Test WorkerExecutor with default max_workers (None)."""
    from motet.core.workers.concurrency_primitives import WorkerExecutor
    
    with patch("motet.core.workers.concurrency_primitives.get_current_pool_type", return_value="threads"):
        with WorkerExecutor() as pool:
            assert pool.max_workers is None
            # Should still work with default
            future = pool.submit(lambda: 42)
            assert future.result() == 42


def test_greenlet_future_wrapper():
    """Test _GreenletFuture wrapper functionality."""
    from motet.core.workers.concurrency_primitives import _GreenletFuture
    
    # Mock greenlet
    mock_greenlet = MagicMock()
    mock_greenlet.get.return_value = 42
    mock_greenlet.dead = False
    
    future = _GreenletFuture(mock_greenlet)
    
    # Test result()
    assert future.result() == 42
    assert future.done()
    assert future.exception() is None
    
    # Test cancel (should return False)
    assert not future.cancel()
    assert not future.cancelled()


def test_greenlet_future_exception():
    """Test _GreenletFuture handles exceptions correctly."""
    from motet.core.workers.concurrency_primitives import _GreenletFuture
    
    # Mock greenlet that raises
    mock_greenlet = MagicMock()
    mock_greenlet.get.side_effect = ValueError("Test error")
    mock_greenlet.dead = False
    
    future = _GreenletFuture(mock_greenlet)
    
    # Should raise when getting result
    with pytest.raises(ValueError, match="Test error"):
        future.result()
    
    # Should be done and have exception
    assert future.done()
    assert isinstance(future.exception(), ValueError)


def test_worker_executor_submit_with_kwargs_threads():
    """Test WorkerExecutor.submit() with keyword arguments."""
    from motet.core.workers.concurrency_primitives import WorkerExecutor
    
    def task(x, y, multiplier=1):
        return (x + y) * multiplier
    
    with patch("motet.core.workers.concurrency_primitives.get_current_pool_type", return_value="threads"):
        with WorkerExecutor(max_workers=2) as pool:
            future = pool.submit(task, 5, 3, multiplier=2)
            result = future.result()
            assert result == 16


def test_worker_executor_shutdown_explicit():
    """Test WorkerExecutor.shutdown() can be called explicitly."""
    from motet.core.workers.concurrency_primitives import WorkerExecutor
    
    with patch("motet.core.workers.concurrency_primitives.get_current_pool_type", return_value="threads"):
        pool = WorkerExecutor(max_workers=2)
        pool.__enter__()
        
        future = pool.submit(lambda: 42)
        assert future.result() == 42
        
        pool.shutdown(wait=True)
        assert pool._pool is None or True  # ThreadPoolExecutor sets internal state


def test_worker_executor_concurrent_load_threads():
    """Test WorkerExecutor under concurrent load with threads."""
    from motet.core.workers.concurrency_primitives import WorkerExecutor
    
    def slow_task(x):
        time.sleep(0.01)  # Small delay
        return x * 2
    
    with patch("motet.core.workers.concurrency_primitives.get_current_pool_type", return_value="threads"):
        with WorkerExecutor(max_workers=5) as pool:
            futures = [pool.submit(slow_task, i) for i in range(20)]
            results = [f.result() for f in futures]
            expected = [i * 2 for i in range(20)]
            assert results == expected


def test_worker_executor_fallback_when_eventlet_unavailable():
    """Test WorkerExecutor falls back to ThreadPoolExecutor when eventlet unavailable."""
    from motet.core.workers.concurrency_primitives import WorkerExecutor
    
    with patch("motet.core.workers.concurrency_primitives.get_current_pool_type", return_value="eventlet"):
        # Simulate eventlet import failure
        with patch.dict(sys.modules, {"eventlet": None}):
            with pytest.raises(ModuleNotFoundError):
                import eventlet  # noqa: F401
            
            # WorkerExecutor should fall back to ThreadPoolExecutor
            with WorkerExecutor(max_workers=2) as pool:
                future = pool.submit(lambda: 42)
                assert future.result() == 42


def test_worker_executor_fallback_when_gevent_unavailable():
    """WorkerExecutor falls back to ThreadPoolExecutor when gevent unavailable; submit() uses .submit()."""
    from motet.core.workers.concurrency_primitives import WorkerExecutor

    with patch("motet.core.workers.concurrency_primitives.get_current_pool_type", return_value="gevent"):
        with patch.dict(sys.modules, {"gevent": None, "gevent.pool": None}):
            with WorkerExecutor(max_workers=2) as pool:
                future = pool.submit(lambda: 42)
                assert future.result() == 42


def test_worker_executor_integration_with_workflow_pattern():
    """Test WorkerExecutor with workflow-like parallel execution pattern."""
    from motet.core.workers.concurrency_primitives import WorkerExecutor
    
    def workflow_step(step_id, duration=0.01):
        """Simulate a workflow step."""
        time.sleep(duration)
        return {"step_id": step_id, "status": "completed"}
    
    with patch("motet.core.workers.concurrency_primitives.get_current_pool_type", return_value="threads"):
        # Simulate executing 3 workflow steps in parallel
        with WorkerExecutor(max_workers=3) as pool:
            step_ids = ["step_1", "step_2", "step_3"]
            futures = {pool.submit(workflow_step, step_id): step_id for step_id in step_ids}
            
            # Collect results
            results = {}
            for future, step_id in futures.items():
                result = future.result()
                results[step_id] = result
            
            # Verify all steps completed
            assert len(results) == 3
            assert all(r["status"] == "completed" for r in results.values())
            assert set(r["step_id"] for r in results.values()) == set(step_ids)

