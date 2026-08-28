"""
Unit tests for resilience components (retry, bulkhead patterns).

Tests the fault tolerance mechanisms used throughout the distributed AI framework.
"""
from __future__ import annotations

import time
import pytest

from motet.core.resilience import (
    Bulkhead,
    bulkhead_sync,
    exponential_backoff,
    retry,
    retry_sync,
)


@pytest.mark.unit
@pytest.mark.unit
def test_exponential_backoff_no_jitter_caps_and_scales():
    assert exponential_backoff(1, base=0.5, cap=5.0, jitter=0.0) == 0.5
    assert exponential_backoff(2, base=0.5, cap=5.0, jitter=0.0) == 1.0
    assert exponential_backoff(3, base=0.5, cap=5.0, jitter=0.0) == 2.0
    assert exponential_backoff(4, base=0.5, cap=5.0, jitter=0.0) == 4.0
    # capped at 5.0 for higher attempts
    assert exponential_backoff(5, base=0.5, cap=5.0, jitter=0.0) == 5.0
    assert exponential_backoff(10, base=0.5, cap=5.0, jitter=0.0) == 5.0


@pytest.mark.unit
@pytest.mark.unit
def test_retry_succeeds_after_failures():
    calls = {"n": 0}

    def sometimes_fails():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("temporary")
        return "ok"

    result = retry(sometimes_fails, max_attempts=5)
    assert result == "ok"
    assert calls["n"] == 3


@pytest.mark.unit
@pytest.mark.unit
def test_retry_stops_after_max_attempts():
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise RuntimeError("always")

    try:
        retry(always_fails, max_attempts=3)
        assert False, "expected failure"
    except RuntimeError:
        pass
    assert calls["n"] == 3


@pytest.mark.unit
def test_retry_should_retry_predicate_blocks_non_retryable():
    calls = {"n": 0}

    class NonRetryable(Exception):
        pass

    def mixed_failures():
        calls["n"] += 1
        if calls["n"] == 1:
            raise NonRetryable("do not retry")
        raise RuntimeError("retryable")

    def should_retry(exc: Exception) -> bool:
        return not isinstance(exc, NonRetryable)

    try:
        retry(mixed_failures, max_attempts=5, should_retry=should_retry)
        assert False, "expected failure"
    except NonRetryable:
        pass
    assert calls["n"] == 1


@pytest.mark.unit
def test_retry_sync_succeeds_after_failures():
    """Test retry_sync with sync functions (async retry removed)"""
    calls = {"n": 0}

    def sometimes_fails():
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("temporary")
        return "ok"

    result = retry_sync(sometimes_fails, max_attempts=5)
    assert result == "ok"
    assert calls["n"] == 2


@pytest.mark.unit
def test_bulkhead_limits_concurrency():
    import threading
    import concurrent.futures

    bh = Bulkhead(max_concurrent=2)
    active = 0
    max_active = 0
    lock = threading.Lock()

    def task():
        nonlocal active, max_active
        def _inner():
            nonlocal active, max_active
            with lock:
                active += 1
                if active > max_active:
                    max_active = active
            time.sleep(0.05)
            with lock:
                active -= 1
            return "done"

        return bh.run(_inner)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(task) for _ in range(6)]
        results = [f.result() for f in futures]
    assert all(r == "done" for r in results)
    assert max_active <= 2


@pytest.mark.unit
def test_bulkhead_decorator_limits_concurrency():
    """Test bulkhead with sync functions (async bulkhead removed)"""
    import threading
    import time
    
    decorated_calls = {"n": 0}
    concurrent_count = {"max": 0, "current": 0}
    lock = threading.Lock()

    @bulkhead_sync(max_concurrent=3)
    def work():
        with lock:
            concurrent_count["current"] += 1
            concurrent_count["max"] = max(concurrent_count["max"], concurrent_count["current"])
            decorated_calls["n"] += 1
        
        time.sleep(0.02)  # Simulate work
        
        with lock:
            concurrent_count["current"] -= 1
        
        return "ok"

    # fire 10 tasks in threads; concurrency must not exceed 3
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        start = time.monotonic()
        futures = [executor.submit(work) for _ in range(10)]
        results = [f.result() for f in futures]
        elapsed = time.monotonic() - start
    
    assert all(r == "ok" for r in results)
    assert decorated_calls["n"] == 10
    assert concurrent_count["max"] <= 3  # Bulkhead should limit concurrency
    # with 3-at-a-time and ~20ms each, elapsed must be at least ~4*20ms ~= 0.06s
    assert elapsed >= 0.06


