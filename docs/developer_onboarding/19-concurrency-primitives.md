# Concurrency Primitives

Motet provides **pool-agnostic concurrency primitives** that automatically adapt to your worker pool type (fork, threads, or gevent). Write thread-safe code once, and it works correctly across all worker pools.

## Overview

Concurrency primitives enable safe coordination when multiple operations access shared resources. Motet's primitives are **drop-in replacements** for Python's `threading` module, with automatic adaptation to the worker pool type.

### Why Pool-Agnostic Primitives?

Three pools are supported, and they do not share a concurrency model:

- **Fork pool**: Multi-process (default on Unix)
- **Threads pool**: Multi-threaded
- **Gevent pool**: Green threads (cooperative multitasking)

The reason this matters is that `threading.Lock` is not merely suboptimal on gevent — it can deadlock, because a real OS lock blocks the entire hub rather than yielding to another greenlet. `WorkerLock` resolves to `gevent.lock.Semaphore(1)` there and to `threading.Lock` elsewhere, so the same code is correct on all three.

Eventlet is **not** supported. If eventlet monkey-patching is detected, pool detection raises `RuntimeError` rather than degrading quietly — use gevent for green threads.

### What each primitive becomes

Every primitive picks its implementation at construction, from the detected pool. You never select one yourself; this table is here so you can reason about behaviour and read a stack trace.

| Primitive | Fork / Threads | Gevent |
|-----------|----------------|--------|
| `WorkerLock` | `threading.Lock` | `gevent.lock.Semaphore(1)` |
| `WorkerRLock` | `threading.RLock` | `gevent.lock.RLock` |
| `WorkerEvent` | `threading.Event` | `gevent.event.Event` |
| `WorkerSemaphore` | `threading.Semaphore` | `gevent.lock.Semaphore` |
| `WorkerLocal` | `threading.local` | `gevent.local.local` |
| `WorkerThread` | `threading.Thread` | `gevent.spawn` |
| `worker_sleep` | `time.sleep` | `gevent.sleep` |
| `WorkerExecutor` | `ThreadPoolExecutor` | `gevent.pool.Pool` |

If gevent is unavailable when the gevent pool is selected, each primitive logs a warning and falls back to its threading equivalent rather than failing.

## Pool-Agnostic Primitives

### WorkerLock - Mutual Exclusion

Ensures only one thread/greenlet can access a resource at a time.

Examples on this page assume the standard command imports:

```python
from motet import motet
from motet.core.commands.decorator import MotetContext
```

```python
from motet.core.workers.concurrency_primitives import WorkerLock

# Create a lock
cache_lock = WorkerLock()

@motet.command()
def update_cache(data: MyData, motet: MotetContext):
    # Only one operation at a time
    with cache_lock:
        cache = read_cache()
        cache[data.key] = data.value
        write_cache(cache)
    
    return {"updated": True}
```

Use it to protect a shared data structure, coordinate file access, or make a read-modify-write sequence atomic.

### WorkerRLock - Reentrant Lock

Allows the same thread/greenlet to acquire the lock multiple times (recursive locking).

```python
from motet.core.workers.concurrency_primitives import WorkerRLock

config_lock = WorkerRLock()

@motet.command()
def update_config(data: MyData, motet: MotetContext):
    with config_lock:
        # Can call nested function that also needs the lock
        validate_config(data.config)  # Also acquires config_lock
        save_config(data.config)
    
    return {"saved": True}

def validate_config(config):
    with config_lock:  # Safe - same thread can re-acquire
        # Validation logic...
        pass
```

Use it when a locked function calls another that needs the same lock: nested helpers, recursive operations, or class methods sharing guarded state.

### WorkerEvent - Cross-Thread Signaling

Allows threads/greenlets to signal each other.

```python
from motet.core.workers.concurrency_primitives import WorkerEvent, WorkerThread

# Create event
data_ready = WorkerEvent()

def producer():
    # Produce data...
    data = fetch_external_data()
    cache['data'] = data
    
    # Signal consumers
    data_ready.set()

def consumer():
    # Wait for data
    data_ready.wait(timeout=30)  # Block until set or timeout
    
    if data_ready.is_set():
        process_data(cache['data'])

# Start producer and consumer
producer_thread = WorkerThread(target=producer)
consumer_thread = WorkerThread(target=consumer)

producer_thread.start()
consumer_thread.start()

producer_thread.join()
consumer_thread.join()
```

Use it for producer-consumer handoffs, waiting on work finishing elsewhere, and sequencing multi-stage operations.

### WorkerSemaphore - Resource Pooling

Limits concurrent access to a resource (e.g., connection pool).

```python
from motet.core.workers.concurrency_primitives import WorkerSemaphore

# Limit to 5 concurrent database connections
db_semaphore = WorkerSemaphore(5)

@motet.command()
def query_database(data: MyData, motet: MotetContext):
    with db_semaphore:
        # Only 5 operations can be here simultaneously
        conn = get_db_connection()
        result = conn.query(data.sql)
        conn.close()
        return result
```

Use it to cap a connection pool, enforce a quota, or hold concurrency below whatever limit the resource itself imposes.

### WorkerLocal - Per-Worker Storage

Thread-local or greenlet-local storage for per-worker data.

```python
from motet.core.workers.concurrency_primitives import WorkerLocal

# Create worker-local storage
worker_local = WorkerLocal()

@motet.command()
def process_request(data: MyData, motet: MotetContext):
    # Each worker gets its own copy
    worker_local.request_id = motet.task_id
    worker_local.start_time = time.time()
    worker_local.db_connection = create_db_connection()
    
    try:
        result = process_data(data)
        log_metrics(worker_local.start_time)
        return result
    finally:
        # Clean up per-worker resources
        if hasattr(worker_local, 'db_connection'):
            worker_local.db_connection.close()
        worker_local.clear()

def process_data(data):
    # Access worker-local data from nested function
    logger.info(f"Processing {worker_local.request_id}")
    return worker_local.db_connection.query(data.sql)
```

Use it for per-request context such as a request id, for per-worker database connections, and for caches that must not outlive the request.

**Methods**:
- `worker_local.attr = value` - Set attribute
- `worker_local.get('attr', default)` - Get with default
- `worker_local.set('attr', value)` - Set explicitly
- `worker_local.has('attr')` - Check existence
- `worker_local.clear()` - Clear all attributes

### WorkerThread - Spawn Threads/Greenlets

Spawns threads or greenlets depending on pool type.

```python
from motet.core.workers.concurrency_primitives import WorkerThread

@motet.command()
def parallel_processing(data: MyData, motet: MotetContext):
    results = []
    
    def process_chunk(chunk):
        results.append(process(chunk))
    
    # Spawn concurrent operations
    threads = []
    for chunk in data.chunks:
        thread = WorkerThread(target=process_chunk, args=(chunk,))
        thread.start()
        threads.append(thread)
    
    # Wait for all to complete
    for thread in threads:
        thread.join()
    
    return {"results": results}
```

Use it for parallel processing, background work, and concurrent I/O.

**Important**: For async operations in gevent, use `run_async_safe()` instead (see Async Integration below).

### worker_sleep() - Cooperative Yielding

Sleep function that cooperates with the pool type.

```python
from motet.core.workers.concurrency_primitives import worker_sleep

@motet.command()
def polling_operation(data: MyData, motet: MotetContext):
    for i in range(10):
        status = check_status()
        if status == "complete":
            return {"status": status}
        
        # Cooperative sleep - yields to other greenlets on gevent
        worker_sleep(1.0)  # Sleep 1 second
    
    return {"status": "timeout"}
```

Use it in polling loops, retry backoff, and anywhere else you would reach for a delay.

### WorkerExecutor - Pool-Aware Concurrent Execution

Use `WorkerExecutor` when you would reach for `concurrent.futures.ThreadPoolExecutor`. It keeps a ThreadPoolExecutor-compatible API (`submit`, `map`, context manager) but picks the right backend for the worker pool.

```python
from motet.core.workers.concurrency_primitives import WorkerExecutor
# Bundle authors can also: from motet_sdk.concurrency import WorkerExecutor

def process_item(item):
    return transform(item)

@motet.command()
def batch_process(data: MyData, motet: MotetContext):
    with WorkerExecutor(max_workers=20) as executor:
        results = list(executor.map(process_item, data.items))
    return {"results": results}
```

**Why this matters**:
- `ThreadPoolExecutor` always spawns **real OS threads**. On a gevent worker configured for high concurrency, that can mean hundreds or thousands of OS threads (high memory, slow context switching).
- `WorkerExecutor` uses **`gevent.pool.Pool`** on gevent (cooperative greenlets) and `ThreadPoolExecutor` on fork/threads.

**Pool behavior**:
| Pool | Backend |
|------|---------|
| Fork / Threads | `concurrent.futures.ThreadPoolExecutor` |
| Gevent | `gevent.pool.Pool` |

**Submit + collect (with per-task errors)**:

```python
from motet.core.workers.concurrency_primitives import WorkerExecutor

@motet.command()
def parallel_steps(data: MyData, motet: MotetContext):
    results = {}
    errors = []

    with WorkerExecutor(max_workers=len(data.steps)) as executor:
        futures = {
            executor.submit(run_step, step): step.id
            for step in data.steps
        }
        for future, step_id in futures.items():
            try:
                results[step_id] = future.result()
            except Exception as e:
                errors.append({"step_id": step_id, "error": str(e)})

    return {"results": results, "errors": errors}
```

**When to use**:
- Parallel workflow / batch work inside a command
- Many independent I/O-bound calls in one worker process
- Anywhere you would otherwise use `ThreadPoolExecutor` for in-process fan-out

**When not to use**:
- Async libraries (`httpx`, Playwright) — use `run_async_safe()` instead
- CPU-bound work that needs real OS threads — use `ThreadPoolExecutor` directly
- Distributed fan-out across workers — use `motet.join()` / `motet.apply()` (or SDK equivalents), not an in-process executor
- A single call — just invoke the function

**Anti-pattern on gevent**:

```python
from concurrent.futures import ThreadPoolExecutor
from motet.core.workers.concurrency_primitives import WorkerExecutor

# BAD on gevent: up to N real OS threads
with ThreadPoolExecutor(max_workers=1000) as executor:
    futures = [executor.submit(task, i) for i in range(1000)]

# GOOD: cooperative greenlets on gevent; ThreadPoolExecutor on fork/threads
with WorkerExecutor(max_workers=1000) as executor:
    futures = [executor.submit(task, i) for i in range(1000)]
```

## Async Integration

For async/await code (e.g., Playwright, httpx), use `run_async_safe()`:

```python
from motet.core.utils.async_helpers import run_async_safe

@motet.command()
def fetch_data(data: MyData, motet: MotetContext):
    # Async function
    async def fetch():
        async with httpx.AsyncClient() as client:
            response = await client.get(data.url)
            return response.json()
    
    # Run async code - works on all pool types!
    result = run_async_safe(fetch())
    return result
```

**How It Works**:
- **Fork/Threads**: Uses `asyncio.run()`
- **Gevent**: Uses `asyncio-gevent` bridge (isolates asyncio in real OS thread)

This is how you reach Playwright, async HTTP clients such as httpx and aiohttp, async database drivers, and anything else built on async/await.

**Important**: Don't use `await` directly in commands - use `run_async_safe()` instead.

## Complete Examples

### Example 1: Thread-Safe Cache

```python
from motet.core.workers.concurrency_primitives import WorkerLock

cache = {}
cache_lock = WorkerLock()

@motet.command()
def get_or_compute(data: MyData, motet: MotetContext):
    """Get from cache or compute and cache."""
    
    # Check cache (shared read)
    with cache_lock:
        if data.key in cache:
            return {"value": cache[data.key], "cached": True}
    
    # Compute value (expensive)
    value = expensive_computation(data.key)
    
    # Update cache (exclusive write)
    with cache_lock:
        cache[data.key] = value
    
    return {"value": value, "cached": False}
```

### Example 2: Connection Pool

```python
from motet.core.workers.concurrency_primitives import WorkerSemaphore, WorkerLocal

# Limit concurrent connections
db_semaphore = WorkerSemaphore(10)  # Max 10 concurrent
worker_local = WorkerLocal()

@motet.command()
def query_with_pooling(data: MyData, motet: MotetContext):
    """Query database with connection pooling."""
    
    with db_semaphore:
        # Reuse per-worker connection
        if not hasattr(worker_local, 'db_conn'):
            worker_local.db_conn = create_db_connection()
        
        result = worker_local.db_conn.query(data.sql)
        return {"result": result}
```

### Example 3: Producer-Consumer Pattern

```python
from motet.core.workers.concurrency_primitives import (
    WorkerEvent, WorkerThread, WorkerLock
)

queue = []
queue_lock = WorkerLock()
data_available = WorkerEvent()

def producer(items):
    """Produce items and signal consumers."""
    for item in items:
        with queue_lock:
            queue.append(item)
        data_available.set()  # Signal consumers
        worker_sleep(0.1)  # Rate limit

def consumer():
    """Consume items when available."""
    while True:
        data_available.wait(timeout=5)
        
        with queue_lock:
            if not queue:
                break
            item = queue.pop(0)
            if not queue:
                data_available.clear()
        
        process_item(item)

@motet.command()
def process_batch(data: MyData, motet: MotetContext):
    """Process batch using producer-consumer."""
    
    # Start consumer thread
    consumer_thread = WorkerThread(target=consumer)
    consumer_thread.start()
    
    # Produce items
    producer(data.items)
    
    # Wait for consumer
    consumer_thread.join()
    
    return {"processed": len(data.items)}
```

### Example 4: Parallel Async Operations

```python
from motet.core.utils.async_helpers import run_async_safe
import asyncio

@motet.command()
def fetch_multiple_urls(data: MyData, motet: MotetContext):
    """Fetch multiple URLs in parallel using async."""
    
    async def fetch_all():
        async with httpx.AsyncClient() as client:
            # Parallel fetches
            tasks = [client.get(url) for url in data.urls]
            responses = await asyncio.gather(*tasks)
            return [r.json() for r in responses]
    
    # Run async code safely on any pool type
    results = run_async_safe(fetch_all())
    
    return {"results": results}
```

### Example 5: Request-Scoped Context

```python
from motet.core.workers.concurrency_primitives import WorkerLocal

# Global worker-local storage
request_context = WorkerLocal()

@motet.command()
def handle_request(data: MyData, motet: MotetContext):
    """Handle request with scoped context."""
    
    # Set request context
    request_context.user_id = data.user_id
    request_context.request_id = motet.task_id
    request_context.start_time = time.time()
    
    try:
        # All nested calls can access context
        result = process_request(data)
        
        # Log metrics
        duration = time.time() - request_context.start_time
        log_request(request_context.user_id, 
                   request_context.request_id,
                   duration)
        
        return result
    finally:
        # Clean up
        request_context.clear()

def process_request(data):
    """Nested function accessing request context."""
    logger.info(f"User {request_context.user_id} - "
               f"Request {request_context.request_id}")
    # Process...
```

## Getting It Right

**Always acquire with `with`.** A manual `acquire()`/`release()` pair leaks the lock whenever the body raises, and on gevent a leaked lock stalls every greenlet behind it rather than one thread.

**Do the expensive part outside the lock.** Prepare data first, then take the lock only for the mutation. A critical section that holds a lock across I/O or heavy computation serialises the whole worker.

**Clear `WorkerLocal` in a `finally`.** Threads and greenlets are reused, so a value left behind is visible to the next command that lands on the same one — a leak that presents as a bewildering cross-request bug rather than as memory growth.

**Bound anything with a connection limit using `WorkerSemaphore`.** Without it, concurrency is capped by the worker's pool size, which is not the same number as your database's connection limit.

**Acquire multiple locks in a consistent order.** This is the one deadlock the primitives cannot protect you from:

```python
lock_a = WorkerLock()
lock_b = WorkerLock()

def operation_one():
    with lock_a:
        with lock_b:  # a then b
            pass

def operation_two():
    with lock_b:
        with lock_a:  # b then a — deadlocks against operation_one
            pass
```

**Reach async code through `run_async_safe`.** Command functions are synchronous; declaring one `async def` does not get it awaited.

```python
@motet.command()
def my_command(data: MyData, motet: MotetContext):
    async def fetch():
        return await client.get(url)

    return run_async_safe(fetch())
```

## Pool Type Detection

Primitives automatically detect the pool type:

```python
from motet.core.workers.concurrency_primitives import (
    get_current_pool_type,
    get_pool_info,
)

# Check current pool type
pool_type = get_current_pool_type()  # "fork", "threads", or "gevent"

# Optional: richer snapshot for diagnostics
info = get_pool_info()
# e.g. {"pool_type": "gevent", "is_cooperative": True, ...}
print(f"Running on: {pool_type}")  # "fork", "threads", or "gevent"
```

**Detection Logic**:
1. Check for gevent monkey patching → "gevent"
2. Check for fork capability (Unix) → "fork"
3. Fallback → "threads"

**Cached**: Detection runs once and caches result.

## Migration from threading

Motet primitives are **drop-in replacements** for Python's `threading` module:

```python
# Before (threading / stdlib)
import threading
from concurrent.futures import ThreadPoolExecutor
import time

lock = threading.Lock()
event = threading.Event()
local = threading.local()
thread = threading.Thread(target=func)
time.sleep(0.1)
with ThreadPoolExecutor(max_workers=10) as ex:
    list(ex.map(func, items))

# After (Motet - works on all pools)
from motet.core.workers.concurrency_primitives import (
    WorkerLock, WorkerEvent, WorkerLocal, WorkerThread,
    WorkerExecutor, worker_sleep,
)

lock = WorkerLock()
event = WorkerEvent()
local = WorkerLocal()
thread = WorkerThread(target=func)
worker_sleep(0.1)
with WorkerExecutor(max_workers=10) as ex:
    list(ex.map(func, items))
```

**Benefits**:
- Same API as `threading` / `ThreadPoolExecutor`
- Works on gevent pools (plain `threading` / OS-thread executors don't scale there)
- Automatic pool adaptation

## Troubleshooting

### Lock Contention

**Symptoms**: Slow performance, high wait times

**Diagnosis**:
```python
import time

# Measure lock wait time
start = time.time()
with lock:
    wait_time = time.time() - start
    if wait_time > 0.1:  # 100ms threshold
        logger.warning(f"High lock contention: {wait_time}s")
    # Critical section...
```

**Solutions**:
1. Reduce critical section size
2. Use finer-grained locks
3. Use lock-free data structures (if possible)
4. Increase worker concurrency

### WorkerLocal Not Isolated

**Symptoms**: Data leaking between requests

**Diagnosis**:
```python
# Check if clear() is called
@motet.command()
def my_command(data: MyData, motet: MotetContext):
    print(f"Before: {hasattr(worker_local, 'old_data')}")  # Should be False
    worker_local.current_data = data
    # ... process ...
    worker_local.clear()  # Essential!
```

**Solutions**:
1. Always call `worker_local.clear()` in `finally` block
2. Use unique attribute names
3. Check for stale data before use

### Deadlock

**Symptoms**: Command hangs indefinitely

**Diagnosis**:
```python
# Add timeouts
acquired = lock.acquire(timeout=5.0)
if not acquired:
    logger.error("Lock acquisition timeout - possible deadlock")
    raise TimeoutError("Deadlock detected")
```

**Solutions**:
1. Use consistent lock ordering
2. Use timeouts on lock acquisition
3. Avoid nested locks when possible
4. Use lock-free alternatives

### Async Not Working on Gevent

**Symptoms**: `RuntimeError: no running event loop`

**Solution**:
```python
# ✅ CORRECT: Use run_async_safe
from motet.core.utils.async_helpers import run_async_safe

@motet.command()
def my_command(data: MyData, motet: MotetContext):
    async def fetch():
        return await client.get(url)
    
    return run_async_safe(fetch())  # Handles gevent bridge
```

## Architecture Reference

Concurrency primitives support the three worker pools (fork, threads, gevent); eventlet is rejected at detection.

**Key Design Principles**:
1. **Pool-Agnostic**: Write once, run on any pool
2. **Automatic Adaptation**: Runtime pool detection
3. **Zero Knowledge**: No pool internals needed
4. **Drop-In Replacement**: Compatible with `threading`
5. **Async Integration**: Seamless async/await support

**Implementation**: `motet/core/workers/concurrency_primitives.py`

## Next Steps

Now that you understand concurrency primitives:

- **[Memory Management](./20-memory-management.md)** - Learn about memory systems
- **[Tool Ecosystem](./21-tool-ecosystem.md)** - Understand tool development
- **[Best Practices](./27-best-practices.md)** - Learn from experience

## Navigation

- **[← Back to Documentation Home](./00-landing-page.md)** - Main documentation hub

---

**Last Updated**: 2026-08-21

**Ready for advanced topics?** Continue to [Memory Management](./20-memory-management.md).
