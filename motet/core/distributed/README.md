## Package: distributed

**Core distributed infrastructure** for coordinating AI operations across multiple workers with intelligent routing and state management.

### Purpose
- **Execution Tracking**: Monitor and track distributed command execution across workers
- **State-Aware Routing**: Intelligent command routing based on worker capabilities and state
- **Worker Coordination**: Manage worker state, capabilities, and health across the distributed system
- **MCP Server Management**: Coordinate MCP server startup and discovery across workers

### Core Components

#### Execution observability
Live worker/task views use Motet Redis keys, not Flower and not Celery `celery-task-meta-*`:
- **Workers**: `worker:registered` + `worker:registration:{id}` / readiness via `/api/v1/workers`
- **Commands**: `cmd:result:` / `cmd:outcome:` / `cmd:meta:` (tenant-prefixed). Parent wait and gather/map fan-in read `cmd:outcome:` (#229 / #242), not `celery-task-meta-*` or EventBus completion events. `retrieve_command_wait_outcome` hydrates `{_redis_result_key}` pointers from `cmd:result`.
- **Tasks**: ephemeral `{tenant}:task:live:{task_id}` + `{tenant}:tasks:live:{principal}` for `/api/v1/tasks`
- **Debug**: `/api/v1/debug` task-flow (`task:events:{task_id}` when `MOTET_DEBUG_MODE` is on)

The Flower-replacement libraries (`direct_redis_monitor.py`, `execution_monitor.py`) were unused and retired (#230). Do not reintroduce `KEYS celery-task-meta-*`.

#### State-Aware Routing (`state_aware_routing.py`)
Intelligent command routing based on worker state and capabilities:
- **StateAwareRouter**: Routes commands to optimal workers based on current state and capabilities
- **Routing Strategies**: Multiple routing algorithms (load-balanced, capability-based, state-aware)
- **Worker Selection**: Intelligent worker candidate selection with scoring and fallback mechanisms
- **Performance Optimization**: Route commands to workers with optimal performance characteristics

#### State Registry (`state_registry.py`)
Distributed worker state management and coordination:
- **EphemeralStateRegistry**: Redis-based registry for tracking ephemeral worker state
- **Worker State Tracking**: Monitor worker capabilities, load, and availability
- **State Synchronization**: Coordinate state updates across distributed workers
- **Capability Management**: Track and update worker capabilities dynamically

#### Redis Manager (`redis_manager.py`)
Unified Redis connection management:
- **UnifiedRedisManager**: Centralized Redis connection pooling for all services
- **Event-Loop Safe**: Automatic async/sync client management
- **Service-Specific Clients**: Isolated clients for different services
- **Distributed Locks**: `DistributedLock` for coordination across workers
- **Structured Data Storage**: Type-safe data operations with format consistency
- **Valkey GLIDE** (`glide_backend.py`): opt-in Rust-core client via `MOTET_VALKEY_CLIENT=glide`. Application get/set/hash/zset/BLPOP/FT.* use a redis-py-shaped adapter. Pub/Sub objects, pipelines, and Celery stay on redis-py. SCAN cursors are sent as bytes (GLIDE rejects ``int``); the adapter still returns an int cursor to redis-py callers. Sync adapters share one process-wide GLIDE client; adapter `close()` drops only the redis-py fallback so a health-check eviction cannot kill every later GET. Default request timeout is 30s (`MOTET_VALKEY_GLIDE_TIMEOUT_MS`) and inflight limit is 128 (`MOTET_VALKEY_GLIDE_INFLIGHT`) so a SCAN or large HGETALL does not starve chat GET/EXISTS. Live set/get/hash/zset coverage is `tests/integration/core/distributed/test_glide_live.py` (needs the GLIDE wheels in the test-runner image).

#### Tenant keys (`tenant_keys.py`)
Leading `{tenant_id}:` prefix for application keys:
- **`tenant_key`**: write and read collapsed `{tenant_id}:{family}:…` (no inner tenant, no `imf:`)
- **`product_key`**: shared control-plane reads and writes `motet:{family}:…` only
- **`event_bus_channel`**: tenant EventBus `{tenant_id}:events:channel`; no usable tenant → platform `motet:events:channel` (issue #233). Workers `PSUBSCRIBE *:events:channel`; SSE subscribes to the caller tenant only
- **`task_response_stream` / `task_control_key` / `task_waiters_key` / `task_live_key` / `tasks_live_index_key`**: Motet task streams and join keys `{tenant}:task:…` (issue #228 slice B). No usable tenant → unprefixed shape. No dual-read. Celery / `_kombu` stay unprefixed.
- **`payload_aad_key_candidates` / `stable_aad_logical_key`**: encrypt AAD uses the collapsed logical name; decrypt retries older physical keys (including `imf:` names). Re-seal leftovers with `scripts/backfill_encrypted_payload_aad.py`.
- **Id-only lookups**: service-account tokens, device tokens, vault credential ids, and live task ids write a `motet:` locator at create time. Resolve is `GET` locator then the tenant key. Callers that have a tenant use `tenant_key`. Backfill missing locators with `scripts/backfill_valkey_locators.py` (dry-run; `--apply --confirm` to write).
- **Vault list index**: `{tid}:vault:index` / `motet:vault:index` is a SET of credential ids. List is `SMEMBERS` then exact metadata `HGETALL`. Store `SADD`s; delete `SREM`s. Empty index means empty list until `scripts/backfill_valkey_vault_index.py` (dry-run; `--apply --confirm`).
- **`cmd_key_scan_patterns` / `command_id_from_cmd_key` / `iter_cmd_keys_sync`**: debug/admin Tasks view must scan both `cmd:meta:*` and `*:cmd:meta:*` (writers store `{tenant}:cmd:meta:{id}`)
- **`TENANT_SCOPED_PREFIXES` / `SHARED_KEY_PREFIXES`**: live families only (`motet:…`, collapsed `{family}:…`, Celery / `worker:` / `lock:`). User workflows are `{tenant}:user_wf:{id}` / `{tenant}:user_wf:index` (issue #234). Vault KEKs (`encryption:tenant:{tid}`) prefix; platform vault stays shared as `motet:vault:…`. `imf` remains a reserved tenant id (issue #232).
- **`tenant_acl_access_string` / `tenant_acl_username`**: ElastiCache RBAC + local ACL user id (`~{tenant}:*`, `-@dangerous`)
- **`tenant_acl.py`**: `ACL SETUSER` on tenant create; `scripts/apply_valkey_tenant_acl.py` for bulk local/AWS sync. Default user stays unrestricted.
- Cutover: `docs/operations/valkey-9-tenant-prefix-cutover.md` and `scripts/rewrite_valkey_tenant_prefixes.py`

#### Worker Readiness (`worker_readiness.py`)
Worker health and capability management:
- **Worker Readiness Registry**: Redis-based registry for tracking worker state
- **Fleet index**: `SMEMBERS worker:registered` then `HGETALL` per `worker:registration:{id}` (no keyspace scan). `worker:ready` is ready-only.
- **Health Monitoring**: Comprehensive health checks and status reporting
- **Capability Tracking**: Dynamic worker capability registration and updates
- **Lifecycle Management**: Worker startup, heartbeat, and termination handling
- **Product version**: each registration stores ``motet_version`` for ``GET /api/v1/version`` (API also probes configured embedding-server and mcp-manager health endpoints)

#### Manager Status (`manager_status.py`)
MCP and local-inference manager health in Redis:
- **Status hashes**: `manager:status:{manager_id}:{type}` with a 30s TTL, refreshed on each publish
- **Fleet index**: `SMEMBERS manager:registered` then pipelined `HGETALL` (no keyspace scan). Set members are the full status keys. Expired hashes are skipped and dropped from the set.

#### Task Control (`task_control.py`)
Task-level cooperative cancel:
- **Sticky cancel**: `{tenant}:task:control:{scope_id}` via UnifiedRedisManager JSON blobs (task id, root command id, or workflow_run_id)
- **`cancel_scopes`**: each command inherits a small list and honors with one variadic `EXISTS`; key existence is the signal
- **Per-waiter BLPOP wake**: `{celery_id}:wake:cancel` / `{celery_id}:wake:result` (hash-tagged); cancel fans out via `{tenant}:task:waiters:{scope_id}`
- **Result signal**: store Motet `cmd:outcome:{command_id}` then `signal_command_result` (also `task_postrun` on `imf.commands.process`). Parents do not call Celery `ready` / `.result` / `.info`. Unary wait and gather/map fan-in load `cmd:outcome` after the wake (#229 / #242). Retrieve hydrates `cmd:result` pointers so large children are not joined as `{_redis_result_key}`. Leftover gather waits and hydrates run concurrently (`WorkerExecutor`; one hash-tagged BLPOP per waiter). `celery.group` is fan-out only; Celery may write a GroupResult/taskset key that Motet does not read. Events are observability, not the composition join.
- **Wait path**: 15s BLPOP chunks; Redis probe errors are tri-state (`unknown` backs off, honor points fail closed)
- **Live index**: ephemeral `{tenant}:task:live:{task_id}` + `{tenant}:tasks:live:{principal}` for `/api/v1/tasks`
- **Honor points**: `WorkerCommunicator` (pre-send + BLPOP wait) and `process_distributed_command` gate
- **Auto-writers**: a command that gives up cancels `own_cancel_scope` if set (roots also write `task_id`; nested leaves are a no-op)
- **Workflow bridge**: `request_task_cancel` cancels runs in `workflow_runs:by_task:{task_id}`; workflow cancel also writes the shared control key

### Usage Examples

#### Execution observability
```python
# Live views are HTTP APIs over Motet keys — not Flower / celery-task-meta-*.
# GET /api/v1/workers
# GET /api/v1/tasks
# GET /api/v1/debug/task-flow/{task_id}
```

#### State-Aware Routing
```python
from motet.core.distributed import get_state_aware_router

# Route command to optimal worker
router = get_state_aware_router
worker_candidate = await router.route_command(command)
print(f"Selected worker: {worker_candidate.worker_id}")
```

#### Worker State Management
```python
from motet.core.distributed import get_state_registry

# Register worker capabilities
registry = get_state_registry
await registry.register_worker_state(worker_id="worker-123",
 capabilities=["reasoning", "tool_execution"],
 state={"load": 0.3, "memory_usage": 0.5})
```

#### MCP Server Coordination

MCP tool registration is **event-driven**: `ensure_mcp_watcher_started(worker_id, tool_registry)` is called from `initialize_worker_unified`, and the watcher subscribes to Redis PUB/SUB for `service_ready` / `service_removed` events. No manual registration call is required—tools are discovered and registered as the MCP instance manager publishes lifecycle events.

### Key Features
- **Distributed-First**: All components designed for multi-worker distributed execution
- **State-Aware**: Dynamic routing and coordination based on real-time worker state
- **Fault Tolerant**: Graceful handling of worker failures and state inconsistencies
- **Observable**: Comprehensive monitoring and metrics for distributed operations
- **Scalable**: Efficient coordination across large numbers of workers

### Configuration

#### Environment Variables
- `MOTET_REDIS_URL`: Redis connection for state registry and coordination
- `MCP_INSTANCE_MANAGER_CONFIG`: Path to MCP instance manager YAML config (default: `/app/config/mcp_instance_manager.yaml`)

#### API Endpoints
- `GET /api/v1/workers` - Worker readiness and registration
- `GET /api/v1/tasks` - Live tasks for the current principal
- `GET /api/v1/debug` - Task-flow / debug inspection (`MOTET_DEBUG_MODE`)

### Integration Points
- **Eventing System**: Integrates with command invoker and routing infrastructure
- **Orchestration**: Provides routing and state management for distributed orchestrator
- **Tools**: Coordinates MCP server management for tool execution
- **Observability**: Provides metrics and monitoring data for system health

### Performance Considerations
- **Redis-Based State**: Efficient state synchronization using Redis data structures
- **Intelligent Routing**: Minimize command execution latency through optimal worker selection
- **State Caching**: Cache worker state and capabilities to reduce coordination overhead
- **Batch Operations**: Efficient batch processing of state updates and queries
