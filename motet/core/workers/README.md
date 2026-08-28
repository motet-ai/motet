## Package: workers

**Distributed worker coordination system** for executing AI operations across Celery workers with intelligent routing and multi-pool support.

### Purpose
- **Distributed Command Execution**: Core system for executing commands across workers
- **Worker-to-Worker Communication**: Seamless command routing and execution
- **Multi-Pool Support**: Works with fork, threads, eventlet, and gevent worker pools
- **Concurrency Primitives**: Pool-agnostic concurrency abstractions for safe cross-pool development
- **Event Streaming**: Real-time event emission for UI integration
- **State Management**: Distributed state tracking and coordination
- **Parent Process Coordination**: MCP server management in parent process

### Core Components

#### Worker Router (`routing/worker_router.py`)
The heart of distributed command routing:
- **Unified Routing Engine**: Filter-then-select pattern with pluggable strategies
- **Worker Coordination**: Intelligent routing based on capabilities and load
- **Circuit Breakers**: Fault tolerance and graceful degradation
- **Pool Type Preference**: Soft preferences for optimal pool selection
- **Edge Capability Guard**: Commands requiring `EDGE_*` capabilities are restricted to `edge_*` workers

#### Routing Strategies (`routing/strategies/`)
Pluggable routing algorithms:
- **Load-Based**: Round-robin, least-loaded, weighted strategies
- **Performance-Based**: Fastest response, state-aware routing
- **Capability-Based**: Capability-optimized worker selection
- **Geographic**: Proximity-based routing for latency optimization

#### Concurrency Primitives (`concurrency_primitives.py`)
Pool-agnostic concurrency abstractions:
- **WorkerLock/WorkerRLock**: Mutual exclusion across all pool types
- **WorkerEvent**: Cross-thread/greenlet signaling
- **WorkerSemaphore**: Resource pooling with bounded concurrency
- **worker_sleep**: Cooperative yielding for green threads
- **WorkerExecutor**: Pool-aware concurrent execution

#### Celery Tasks (`tasks.py` / `command_tasks.py`)
Distributed task execution infrastructure:
- **process_distributed_command**: Core task for executing distributed commands. Writes Motet `cmd:outcome:{command_id}` then wakes the parent (`ignore_result=True`; unary `motet.do` and gather/map `join` / `apply` load that key after the wake, issues #229 / #242). Large results stay in `cmd:result`; retrieve hydrates `{_redis_result_key}` pointers. Gather leftover waits and hydrates run concurrently.
- **Worker Initialization**: Automatic MCP server discovery and setup
- **State Registration**: Worker capability and state management
- **Function discovery index**: On worker context creation, calls
 `FunctionDiscoveryVectorStore.ensure_shared_index` so the shared Valkey
 index is adopted or rebuilt under a distributed writer lock.
 MCP add/remove callbacks retry that same lock and update the index
 incrementally; they must not drop tools on contention.

#### Function discovery coordination
The discovery index (tools, workflows, commands for `core.tools_search`) is
shared across workers in Valkey. Rebuilding it is destructive, so workers
coordinate rather than each clearing and repopulating from a partial catalog:

- **Manifest in Redis** — entry metadata (including descriptions used by the
 keyword half) is published next to the index; `persist_dir` is only a
 per-container cache.
- **`ensure_shared_index`** — adopt a published index if current; otherwise
 lock, re-check, rebuild once; waiters adopt the winner.
- **Incremental merge** — MCP and bundle updates merge into the shared
 manifest so one worker cannot evict another’s tools from the entry map.

See `motet/core/tools/README.md` and for details.

#### Parent Coordinator (`parent_coordinator.py`)
Parent process coordination:
- **Worker Registration**: Tracks worker lifecycle and capabilities in Redis (`worker:registered` + `worker:registration:{id}`)
- **Ready Guard**: Does not mark workers ready with empty/fallback capabilities (#151)
- **Readiness Rewrite**: `publish_worker_readiness_from_context` rewrites Redis after successful context rebuild (`worker_initialization.py`)
- **Health Monitoring**: Heartbeat, health check, and cleanup threads

#### Observers (`observers.py`)
Distributed event monitoring and analysis:
- **DistributedExecutionObserver**: Monitors command execution across workers
- **WorkerObserver**: Tracks worker health and performance
- **CommandRoutingObserver**: Analyzes routing decisions and performance

### Usage Examples

#### Distributed Command Execution
```python
from motet.core.workers import global_invoker
from motet.core.commands.builtin.tool import tool_execution, ToolExecutionData

# Execute command across distributed workers (decorator-based API)
global_invoker.initialize
command = tool_execution(data=ToolExecutionData(tool_name="web_search", parameters={"query": "AI"}),
 task_id="task-123",
 conversation_id="conv-456",)
result = global_invoker.execute_command(command)
```

#### Pool-Agnostic Concurrency
```python
from motet.core.workers.concurrency_primitives import WorkerLock, worker_sleep

# Works on ALL pool types (fork, threads, eventlet, gevent)
lock = WorkerLock
with lock:
 # Critical section
 shared_resource.update(data)

# Cooperative yielding
worker_sleep(0.1) # Yields cooperatively on eventlet/gevent
```

#### Worker Routing
```python
from motet.core.workers.routing import WorkerRouter

# Route command to optimal worker
router = WorkerRouter
worker = await router.route_command(command)
```

### Key Features
- **Distributed-First**: All operations designed for multi-worker execution
- **Multi-Pool Support**: Fork, threads, eventlet, gevent worker pools
- **Pool-Agnostic**: Write once, run on any pool type
- **Fault Tolerant**: Circuit breakers, retries, and graceful degradation
- **Event-Driven**: Real-time event streaming and coordination
- **State-Aware**: Dynamic routing based on worker capabilities
- **Parent Process MCP**: All workers share MCP servers for 10x scalability
- **Observable**: Comprehensive monitoring and metrics

### Related Documentation



- [Tools package (discovery ranking / coordination)](../tools/README.md)
- [Concurrency Primitives Guide](../../docs/developer_onboarding/19-concurrency-primitives.md)
- [Worker Pool Configuration](../../docs/operations/worker-pool-configuration.md)

