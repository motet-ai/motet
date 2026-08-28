"""
Unit tests for pool-aware concurrency primitives (ADR-0033 Phase 0).

These tests verify that WorkerLock, WorkerEvent, WorkerThread, and other
primitives work correctly across all pool types: fork, threads, eventlet, gevent.

Test Strategy:
1. Mock pool type detection for deterministic testing
2. Test each primitive on each pool type
3. Verify correct underlying implementation is selected
4. Test basic functionality (acquire/release, set/wait, etc.)
5. Test edge cases (timeouts, blocking, reentrance)
"""

import sys
import time
from unittest.mock import Mock, patch, MagicMock
import pytest

from motet.core.workers.concurrency_primitives import (
    WorkerLock,
    WorkerRLock,
    WorkerEvent,
    WorkerSemaphore,
    WorkerLocal,
    WorkerThread,
    worker_sleep,
    detect_worker_pool_type,
    get_current_pool_type,
    get_pool_info,
)


# ============================================================================
# Fixtures for pool type mocking
# ============================================================================

@pytest.fixture
def mock_fork_pool():
    """Mock fork pool environment."""
    with patch('motet.core.workers.concurrency_primitives.get_current_pool_type', return_value='fork'):
        # Clear cached pool type
        import motet.core.workers.concurrency_primitives as module
        module._cached_pool_type = None
        yield
        module._cached_pool_type = None


@pytest.fixture
def mock_threads_pool():
    """Mock threads pool environment."""
    with patch('motet.core.workers.concurrency_primitives.get_current_pool_type', return_value='threads'):
        import motet.core.workers.concurrency_primitives as module
        module._cached_pool_type = None
        yield
        module._cached_pool_type = None


@pytest.fixture
def mock_eventlet_pool():
    """Mock eventlet pool environment."""
    with patch('motet.core.workers.concurrency_primitives.get_current_pool_type', return_value='eventlet'):
        import motet.core.workers.concurrency_primitives as module
        module._cached_pool_type = None
        yield
        module._cached_pool_type = None


@pytest.fixture
def mock_gevent_pool():
    """Mock gevent pool environment."""
    with patch('motet.core.workers.concurrency_primitives.get_current_pool_type', return_value='gevent'):
        import motet.core.workers.concurrency_primitives as module
        module._cached_pool_type = None
        yield
        module._cached_pool_type = None


# ============================================================================
# WorkerLock Tests
# ============================================================================

class TestWorkerLock:
    """Test WorkerLock primitive."""
    
    def test_lock_fork_pool(self, mock_fork_pool):
        """WorkerLock uses a lock with acquire/release on fork pool."""
        lock = WorkerLock()
        
        # Should have lock-like interface (avoid isinstance in case threading is mocked)
        assert hasattr(lock._lock, "acquire") and hasattr(lock._lock, "release")
        
        # Test basic functionality
        assert lock.acquire()
        assert lock.locked()
        lock.release()
        assert not lock.locked()
        print("✅ WorkerLock works on fork pool")
    
    def test_lock_threads_pool(self, mock_threads_pool):
        """WorkerLock uses a lock with acquire/release on threads pool."""
        lock = WorkerLock()
        
        assert hasattr(lock._lock, "acquire") and hasattr(lock._lock, "release")
        
        # Test context manager
        with lock:
            assert lock.locked()
        assert not lock.locked()
        print("✅ WorkerLock works on threads pool")
    
    def test_lock_eventlet_pool_with_eventlet(self, mock_eventlet_pool):
        """When eventlet is reported, WorkerLock falls back to threading (eventlet deprecated)."""
        lock = WorkerLock()
        assert hasattr(lock._lock, "acquire") and hasattr(lock._lock, "release")
        assert lock.acquire()
        lock.release()
        print("✅ WorkerLock works on eventlet-mocked pool (threading fallback)")
    
    def test_lock_gevent_pool_with_gevent(self, mock_gevent_pool):
        """WorkerLock uses gevent.lock on gevent pool (if available)."""
        try:
            import gevent.lock
            lock = WorkerLock()
            assert isinstance(lock._lock, gevent.lock.Semaphore)
            
            # Test basic functionality
            assert lock.acquire()
            lock.release()
            print("✅ WorkerLock uses gevent.lock.Semaphore on gevent pool")
        except ImportError:
            pytest.skip("gevent not available")
    
    def test_lock_timeout(self, mock_threads_pool):
        """WorkerLock supports timeout."""
        lock = WorkerLock()
        
        # Acquire lock
        assert lock.acquire()
        
        # Try to acquire with timeout (should fail)
        acquired = lock.acquire(blocking=True, timeout=0.1)
        assert not acquired
        
        lock.release()
        print("✅ WorkerLock timeout works")
    
    def test_lock_non_blocking(self, mock_threads_pool):
        """WorkerLock supports non-blocking acquire."""
        lock = WorkerLock()
        
        # First acquire should succeed
        assert lock.acquire(blocking=False)
        
        # Second acquire should fail immediately
        acquired = lock.acquire(blocking=False)
        assert not acquired
        
        lock.release()
        print("✅ WorkerLock non-blocking works")


# ============================================================================
# WorkerRLock Tests
# ============================================================================

class TestWorkerRLock:
    """Test WorkerRLock primitive."""
    
    def test_rlock_reentrant(self, mock_threads_pool):
        """WorkerRLock can be acquired multiple times by same thread."""
        lock = WorkerRLock()
        
        # Acquire multiple times (reentrant)
        assert lock.acquire()
        assert lock.acquire()
        assert lock.acquire()
        
        # Release same number of times
        lock.release()
        lock.release()
        lock.release()
        
        print("✅ WorkerRLock is reentrant")
    
    def test_rlock_gevent_pool(self, mock_gevent_pool):
        """WorkerRLock uses gevent.lock.RLock on gevent pool."""
        try:
            import gevent.lock
            lock = WorkerRLock()
            assert isinstance(lock._lock, gevent.lock.RLock)
            print("✅ WorkerRLock uses gevent.lock.RLock on gevent pool")
        except ImportError:
            pytest.skip("gevent not available")


# ============================================================================
# WorkerEvent Tests
# ============================================================================

class TestWorkerEvent:
    """Test WorkerEvent primitive."""
    
    def test_event_fork_pool(self, mock_fork_pool):
        """WorkerEvent uses threading.Event on fork pool."""
        event = WorkerEvent()
        
        import threading
        assert isinstance(event._event, threading.Event)
        
        # Test basic functionality
        assert not event.is_set()
        event.set()
        assert event.is_set()
        event.clear()
        assert not event.is_set()
        print("✅ WorkerEvent works on fork pool")
    
    def test_event_wait_timeout(self, mock_threads_pool):
        """WorkerEvent wait respects timeout."""
        event = WorkerEvent()
        
        # Wait with timeout (should timeout)
        result = event.wait(timeout=0.1)
        assert not result
        
        # Set event and wait (should succeed immediately)
        event.set()
        result = event.wait(timeout=0.1)
        assert result
        print("✅ WorkerEvent timeout works")
    
    def test_event_eventlet_pool(self, mock_eventlet_pool):
        """When eventlet is reported, WorkerEvent falls back to threading (eventlet deprecated)."""
        event = WorkerEvent()
        assert hasattr(event._event, "set") and hasattr(event._event, "wait")
        event.set()
        assert event.is_set()
        print("✅ WorkerEvent works on eventlet-mocked pool (threading fallback)")
    
    def test_event_gevent_pool(self, mock_gevent_pool):
        """WorkerEvent uses gevent.event on gevent pool."""
        try:
            import gevent.event
            event = WorkerEvent()
            assert isinstance(event._event, gevent.event.Event)
            
            # Test set/wait
            event.set()
            assert event.is_set()
            print("✅ WorkerEvent uses gevent.event.Event on gevent pool")
        except ImportError:
            pytest.skip("gevent not available")


# ============================================================================
# WorkerSemaphore Tests
# ============================================================================

class TestWorkerSemaphore:
    """Test WorkerSemaphore primitive."""
    
    def test_semaphore_value(self, mock_threads_pool):
        """WorkerSemaphore respects initial value."""
        sem = WorkerSemaphore(value=3)
        
        # Can acquire 3 times
        assert sem.acquire(blocking=False)
        assert sem.acquire(blocking=False)
        assert sem.acquire(blocking=False)
        
        # Fourth acquire should fail
        acquired = sem.acquire(blocking=False)
        assert not acquired
        
        # Release and try again
        sem.release()
        assert sem.acquire(blocking=False)
        
        print("✅ WorkerSemaphore value works")
    
    def test_semaphore_context_manager(self, mock_threads_pool):
        """WorkerSemaphore works as context manager."""
        sem = WorkerSemaphore(value=1)
        
        with sem:
            # Should be acquired
            acquired = sem.acquire(blocking=False)
            assert not acquired  # Already held
        
        # Should be released
        assert sem.acquire(blocking=False)
        print("✅ WorkerSemaphore context manager works")


# ============================================================================
# WorkerLocal Tests
# ============================================================================

class TestWorkerLocal:
    """Test WorkerLocal primitive."""
    
    def test_local_fork_pool(self, mock_fork_pool):
        """WorkerLocal uses threading.local on fork pool."""
        worker_local = WorkerLocal()
        
        import threading
        assert isinstance(worker_local._local, threading.local)
        
        # Test basic attribute setting/getting
        worker_local.test_value = 42
        assert worker_local.test_value == 42
        print("✅ WorkerLocal works on fork pool")
    
    def test_local_threads_pool(self, mock_threads_pool):
        """WorkerLocal uses threading.local on threads pool."""
        worker_local = WorkerLocal()
        
        import threading
        assert isinstance(worker_local._local, threading.local)
        
        # Test attribute operations
        worker_local.name = "test"
        worker_local.count = 100
        
        assert worker_local.name == "test"
        assert worker_local.count == 100
        print("✅ WorkerLocal works on threads pool")
    
    def test_local_gevent_pool(self, mock_gevent_pool):
        """WorkerLocal uses gevent.local on gevent pool."""
        try:
            import gevent.local
            worker_local = WorkerLocal()
            assert isinstance(worker_local._local, gevent.local.local)
            
            # Test basic functionality
            worker_local.greenlet_id = 123
            assert worker_local.greenlet_id == 123
            print("✅ WorkerLocal uses gevent.local on gevent pool")
        except ImportError:
            pytest.skip("gevent not available")
    
    def test_local_attribute_isolation(self, mock_threads_pool):
        """WorkerLocal isolates attributes per thread."""
        worker_local = WorkerLocal()
        results = {}
        
        def worker_func(worker_id):
            # Set worker-specific value
            worker_local.worker_id = worker_id
            worker_sleep(0.05)  # Give other threads time to interfere
            
            # Value should still be worker-specific
            results[worker_id] = worker_local.worker_id
        
        threads = []
        for i in range(3):
            t = WorkerThread(target=worker_func, args=(i,))
            t.start()
            threads.append(t)
        
        for t in threads:
            t.join(timeout=1.0)
        
        # Each worker should have its own value
        assert results == {0: 0, 1: 1, 2: 2}
        print("✅ WorkerLocal isolates attributes per thread")
    
    def test_local_get_method(self, mock_threads_pool):
        """WorkerLocal.get() returns default for missing attributes."""
        worker_local = WorkerLocal()
        
        # Get with default
        value = worker_local.get('missing', 'default_value')
        assert value == 'default_value'
        
        # Set and get
        worker_local.existing = 'real_value'
        value = worker_local.get('existing', 'default_value')
        assert value == 'real_value'
        
        print("✅ WorkerLocal.get() works with defaults")
    
    def test_local_set_method(self, mock_threads_pool):
        """WorkerLocal.set() alternative to direct assignment."""
        worker_local = WorkerLocal()
        
        # Use set method
        worker_local.set('key1', 'value1')
        assert worker_local.key1 == 'value1'
        
        # Direct assignment should still work
        worker_local.key2 = 'value2'
        assert worker_local.get('key2') == 'value2'
        
        print("✅ WorkerLocal.set() works")
    
    def test_local_has_method(self, mock_threads_pool):
        """WorkerLocal.has() checks attribute existence."""
        worker_local = WorkerLocal()
        
        # Check non-existent attribute
        assert not worker_local.has('nonexistent')
        
        # Set attribute
        worker_local.test_attr = 'value'
        assert worker_local.has('test_attr')
        
        # Delete attribute
        del worker_local.test_attr
        assert not worker_local.has('test_attr')
        
        print("✅ WorkerLocal.has() checks existence")
    
    def test_local_clear_method(self, mock_threads_pool):
        """WorkerLocal.clear() removes all attributes."""
        worker_local = WorkerLocal()
        
        # Set multiple attributes
        worker_local.attr1 = 'value1'
        worker_local.attr2 = 'value2'
        worker_local.attr3 = 'value3'
        
        assert worker_local.has('attr1')
        assert worker_local.has('attr2')
        assert worker_local.has('attr3')
        
        # Clear all
        worker_local.clear()
        
        assert not worker_local.has('attr1')
        assert not worker_local.has('attr2')
        assert not worker_local.has('attr3')
        
        print("✅ WorkerLocal.clear() removes all attributes")
    
    def test_local_delete_attribute(self, mock_threads_pool):
        """WorkerLocal supports attribute deletion."""
        worker_local = WorkerLocal()
        
        # Set and verify
        worker_local.temp_value = 'temporary'
        assert worker_local.has('temp_value')
        
        # Delete
        del worker_local.temp_value
        assert not worker_local.has('temp_value')
        
        # Accessing deleted attribute should raise AttributeError
        with pytest.raises(AttributeError):
            _ = worker_local.temp_value
        
        print("✅ WorkerLocal attribute deletion works")
    
    def test_local_context_manager_pattern(self, mock_threads_pool):
        """WorkerLocal works well in context manager pattern."""
        from contextlib import contextmanager
        
        @contextmanager
        def request_context(request_id):
            local = WorkerLocal()
            local.request_id = request_id
            local.start_time = time.time()
            try:
                yield local
            finally:
                local.clear()
        
        # Use context manager
        with request_context('req-123') as ctx:
            assert ctx.request_id == 'req-123'
            assert hasattr(ctx, 'start_time')
            ctx.data = 'test_data'
        
        # After context, new instance should be clean
        with request_context('req-456') as ctx:
            assert ctx.request_id == 'req-456'
            assert not ctx.has('data')  # Previous context data is gone
        
        print("✅ WorkerLocal works in context manager pattern")
    
    def test_local_connection_pooling_pattern(self, mock_threads_pool):
        """WorkerLocal works for connection pooling pattern."""
        db_local = WorkerLocal()
        connections_created = []
        
        def create_db_connection(worker_id):
            conn = f"connection-{worker_id}"
            connections_created.append(conn)
            return conn
        
        def get_db_connection(worker_id):
            if not db_local.has('connection'):
                db_local.connection = create_db_connection(worker_id)
            return db_local.connection
        
        def worker_func(worker_id):
            # First call creates connection
            conn1 = get_db_connection(worker_id)
            worker_sleep(0.01)
            
            # Second call reuses connection
            conn2 = get_db_connection(worker_id)
            assert conn1 == conn2
            
            # Clean up
            del db_local.connection
        
        threads = []
        for i in range(3):
            t = WorkerThread(target=worker_func, args=(i,))
            t.start()
            threads.append(t)
        
        for t in threads:
            t.join(timeout=1.0)
        
        # Should have created exactly 3 connections (one per worker)
        assert len(connections_created) == 3
        print("✅ WorkerLocal works for connection pooling")
    
    def test_local_distributed_tracing_pattern(self, mock_threads_pool):
        """WorkerLocal works for distributed tracing pattern."""
        trace_local = WorkerLocal()
        trace_logs = []
        
        def start_span(trace_id, span_id):
            trace_local.trace_id = trace_id
            trace_local.span_id = span_id
            trace_local.start_time = time.time()
        
        def end_span():
            duration = time.time() - trace_local.start_time
            trace_logs.append({
                'trace_id': trace_local.trace_id,
                'span_id': trace_local.span_id,
                'duration': duration
            })
            trace_local.clear()
        
        def worker_with_trace(worker_id):
            start_span(f'trace-{worker_id}', f'span-{worker_id}')
            worker_sleep(0.02)  # Simulate work
            end_span()
        
        threads = []
        for i in range(3):
            t = WorkerThread(target=worker_with_trace, args=(i,))
            t.start()
            threads.append(t)
        
        for t in threads:
            t.join(timeout=1.0)
        
        # Should have 3 trace logs with correct IDs
        assert len(trace_logs) == 3
        trace_ids = {log['trace_id'] for log in trace_logs}
        assert trace_ids == {'trace-0', 'trace-1', 'trace-2'}
        print("✅ WorkerLocal works for distributed tracing")
    
    def test_local_memory_cleanup(self, mock_threads_pool):
        """WorkerLocal properly cleans up memory."""
        worker_local = WorkerLocal()
        
        # Set large-ish data structures
        worker_local.large_list = list(range(1000))
        worker_local.large_dict = {i: f"value-{i}" for i in range(100)}
        
        # Verify data exists
        assert len(worker_local.large_list) == 1000
        assert len(worker_local.large_dict) == 100
        
        # Clear should remove everything
        worker_local.clear()
        
        # Accessing should raise AttributeError
        with pytest.raises(AttributeError):
            _ = worker_local.large_list
        
        with pytest.raises(AttributeError):
            _ = worker_local.large_dict
        
        print("✅ WorkerLocal properly cleans up memory")


# ============================================================================
# WorkerThread Tests
# ============================================================================

class TestWorkerThread:
    """Test WorkerThread primitive."""
    
    def test_thread_fork_pool(self, mock_fork_pool):
        """WorkerThread uses threading.Thread on fork pool."""
        result = []
        
        def target_func(value):
            result.append(value)
        
        thread = WorkerThread(target=target_func, args=(42,))
        thread.start()
        thread.join(timeout=1.0)
        
        assert result == [42]
        assert not thread.is_alive()
        print("✅ WorkerThread works on fork pool")
    
    def test_thread_with_kwargs(self, mock_threads_pool):
        """WorkerThread supports kwargs."""
        result = {}
        
        def target_func(key, value):
            result[key] = value
        
        thread = WorkerThread(target=target_func, kwargs={'key': 'test', 'value': 123})
        thread.start()
        thread.join(timeout=1.0)
        
        assert result == {'test': 123}
        print("✅ WorkerThread kwargs work")
    
    def test_thread_eventlet_pool(self, mock_eventlet_pool):
        """When eventlet is reported, WorkerThread falls back to threading (eventlet deprecated)."""
        result = []

        def target_func(value):
            result.append(value)

        thread = WorkerThread(target=target_func, args=(99,))
        assert thread._pool_type == "threading"
        thread.start()
        thread.join(timeout=1.0)

        assert result == [99]
        print("✅ WorkerThread works on eventlet-mocked pool (threading fallback)")


# ============================================================================
# worker_sleep Tests
# ============================================================================

class TestWorkerSleep:
    """Test worker_sleep function."""
    
    def test_sleep_fork_pool(self, mock_fork_pool):
        """worker_sleep uses time.sleep on fork pool."""
        start = time.time()
        worker_sleep(0.1)
        elapsed = time.time() - start
        
        assert 0.08 < elapsed < 0.15  # Allow some variance
        print("✅ worker_sleep works on fork pool")
    
    def test_sleep_threads_pool(self, mock_threads_pool):
        """worker_sleep uses time.sleep on threads pool."""
        start = time.time()
        worker_sleep(0.05)
        elapsed = time.time() - start
        
        assert 0.03 < elapsed < 0.10
        print("✅ worker_sleep works on threads pool")
    
    @patch('motet.core.workers.concurrency_primitives.get_current_pool_type', return_value='eventlet')
    def test_sleep_eventlet_pool(self, mock_pool_type):
        """When eventlet is reported, worker_sleep falls back to time.sleep (eventlet deprecated)."""
        with patch.object(time, 'sleep') as mock_sleep:
            worker_sleep(0.5)
            mock_sleep.assert_called_once_with(0.5)
        print("✅ worker_sleep works on eventlet-mocked pool (time.sleep fallback)")
    
    @patch('motet.core.workers.concurrency_primitives.get_current_pool_type', return_value='gevent')
    def test_sleep_gevent_pool(self, mock_pool_type):
        """worker_sleep uses gevent.sleep on gevent pool."""
        try:
            import gevent
            with patch.object(gevent, 'sleep') as mock_sleep:
                worker_sleep(0.3)
                mock_sleep.assert_called_once_with(0.3)
                print("✅ worker_sleep uses gevent.sleep on gevent pool")
        except ImportError:
            pytest.skip("gevent not available")


# ============================================================================
# Pool Detection Tests
# ============================================================================

class TestPoolDetection:
    """Test pool type detection."""
    
    def test_get_pool_info(self, mock_threads_pool):
        """get_pool_info returns correct information."""
        info = get_pool_info()
        
        assert 'pool_type' in info
        assert 'is_cooperative' in info
        assert 'supports_os_threads' in info
        assert 'primitives_available' in info
        
        # All primitives should be available
        assert info['primitives_available']['WorkerLock']
        assert info['primitives_available']['WorkerEvent']
        assert info['primitives_available']['WorkerLocal']
        assert info['primitives_available']['worker_sleep']
        
        print("✅ get_pool_info works")
    
    def test_pool_info_cooperative(self, mock_eventlet_pool):
        """get_pool_info correctly identifies cooperative pools."""
        info = get_pool_info()
        
        assert info['pool_type'] == 'eventlet'
        assert info['is_cooperative'] is True
        assert info['supports_os_threads'] is False
        print("✅ Pool info correctly identifies cooperative pools")
    
    def test_pool_info_threads(self, mock_threads_pool):
        """get_pool_info correctly identifies thread pools."""
        info = get_pool_info()
        
        assert info['pool_type'] == 'threads'
        assert info['is_cooperative'] is False
        assert info['supports_os_threads'] is True
        print("✅ Pool info correctly identifies thread pools")


# ============================================================================
# Integration Tests
# ============================================================================

class TestConcurrencyPrimitivesIntegration:
    """Integration tests for primitives working together."""
    
    def test_lock_and_event_together(self, mock_threads_pool):
        """WorkerLock and WorkerEvent work together."""
        lock = WorkerLock()
        event = WorkerEvent()
        shared_data = []
        
        def producer():
            with lock:
                shared_data.append("data")
                event.set()
        
        def consumer():
            event.wait(timeout=1.0)
            with lock:
                assert "data" in shared_data
        
        thread1 = WorkerThread(target=producer)
        thread2 = WorkerThread(target=consumer)
        
        thread1.start()
        thread2.start()
        
        thread1.join(timeout=2.0)
        thread2.join(timeout=2.0)
        
        assert shared_data == ["data"]
        print("✅ WorkerLock and WorkerEvent work together")
    
    def test_semaphore_limits_concurrency(self, mock_threads_pool):
        """WorkerSemaphore correctly limits concurrency."""
        semaphore = WorkerSemaphore(value=2)
        concurrent_count = []
        max_concurrent = [0]
        lock = WorkerLock()
        
        def worker(worker_id):
            with semaphore:
                with lock:
                    concurrent_count.append(worker_id)
                    max_concurrent[0] = max(max_concurrent[0], len(concurrent_count))
                
                worker_sleep(0.05)  # Simulate work
                
                with lock:
                    concurrent_count.remove(worker_id)
        
        threads = []
        for i in range(5):
            t = WorkerThread(target=worker, args=(i,))
            t.start()
            threads.append(t)
        
        for t in threads:
            t.join(timeout=2.0)
        
        # Max concurrent should be 2 (semaphore value)
        assert max_concurrent[0] <= 2
        print(f"✅ WorkerSemaphore limited concurrency to {max_concurrent[0]}")


# ============================================================================
# Main Test Runner
# ============================================================================

if __name__ == "__main__":
    """Run tests directly (useful for development)."""
    pytest.main([__file__, "-v", "-s"])

