## Package: resilience

**Distributed resilience infrastructure** with circuit breakers, retries, and bulkheads for fault-tolerant distributed AI operations.

### Purpose
- **Distributed Fault Tolerance**: Prevent cascading failures across distributed workers and commands
- **Circuit Breaker Integration**: Protect distributed command execution with automatic failure detection
- **Retry Mechanisms**: Handle transient failures in distributed operations with intelligent backoff
- **Load Shedding**: Bulkhead patterns to isolate and protect critical distributed resources

### Components
- `breaker.py`: `CircuitBreaker`, `CircuitState`, `get_breaker`, `get_breaker_configured` with Prometheus metrics for transitions and blocked calls.
- `retry.py`: `retry`, `retry_async`, `exponential_backoff` with jitter and caps.
- `bulkhead.py`: `Bulkhead` and `bulkhead_async` decorator using asyncio.Semaphore.

### Implemented
- Circuit breaker transitions and blocked counters; integrated in tools/models.
- Sync/async retry with exponential backoff; deterministic tests.
- Bulkhead concurrency limiting and decorator; concurrency tests.

### Planned
- Configurable policies per component and dynamic overrides.
- Bulkhead pools and isolation levels; breaker dashboards.

