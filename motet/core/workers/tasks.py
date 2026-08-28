"""
Motet - Tasks

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Worker tasks for the Motet distributed framework, including Celery worker
    context construction, function-discovery index adoption/rebuild, and
    registry reconciliation for tools, workflows, and commands.

Dependencies:
    - typing: Type hints and annotations
    - Base interfaces and implementations
    - motet.core.commands.distributed: eager built-in command registration
      before discovery indexing (avoids stale-command wipe)

Usage:
    from motet.core.workers.tasks import Tasks

Notes:
    - Provides core functionality
    - Integrates with distributed architecture
    - Lifecycle management worker is capability-restricted
    - Built-in command types must be registered before
      ``ensure_shared_index`` / ``reconcile_registry_state``; otherwise
      reconcile treats missing registry entries as stale and deletes them
      from the shared discovery index (breaking ``core.help`` command hits).
"""


from __future__ import annotations

import os
import time
import socket
import uuid
from typing import Dict, Any, Optional
import structlog

# Import centralized Celery app
from .celery_app import get_celery_app

# Canonical capability enum (source of truth for advertised capability strings)
from motet.core.commands.capabilities import WorkerCapability
from motet.core.constants import CELERY_PROCESS_COMMAND_TASK

# Structured logger
logger = structlog.get_logger(__name__)

# Debug gating for high-volume worker startup logs.
DEBUG_WORKER_STARTUP = os.getenv("MOTET_DEBUG_WORKER_STARTUP", "false").lower() == "true"


def _startup_log(message: str, **kwargs: Any) -> None:
    """
    Log worker-startup diagnostics without spamming production logs.

    When MOTET_DEBUG_WORKER_STARTUP=true, logs at INFO; otherwise logs at DEBUG.
    """
    if DEBUG_WORKER_STARTUP:
        logger.info(message, **kwargs)
    else:
        logger.debug(message, **kwargs)

# Process-aware worker context cache - each process gets its own cached context
# This avoids expensive re-initialization of MotetStack, MCP servers, and tool registry on every command
_worker_context_cache = {}  # PID -> context mapping

# Import all task modules to ensure they're registered
from . import worker_tasks
from . import command_tasks
from . import schedule_tasks  # Register schedule tasks (recurring, conditional, cleanup)

# Re-export commonly used tasks for convenience
from .worker_tasks import worker_shutdown  # Only keep shutdown for manual API use
from .command_tasks import (
    process_distributed_command, 
    batch_process_commands, 
    retry_failed_command,
    command_processor_health_check
)

# Note: Event tasks removed - events are now handled by Redis queue system


def _create_worker_context() -> Dict[str, Any]:
    """
    Worker context creation for Celery processes (ADR-0038, ADR-0069).
    
    Creates a cached worker context with:
    - MotetStack initialization with tool registry (built-in tools only at creation time)
    - MCP tools added by watcher thread (same path for parent and children; no blocking discovery)
    - State registry and distributed metrics initialization
    - Distributed invoker creation for worker-to-worker calls
    
    ADR-0069: Every process (parent + fork/thread/eventlet/gevent children) starts the MCP watcher
    via ensure_mcp_watcher_started(); the watcher SUBSCRIBEs to Redis and registers tools as events arrive.
    
    Returns:
        Dict containing worker context information
    """
    global _worker_context_cache
    
    # Get current process ID for process-aware caching
    current_pid = os.getpid()
    
    # Return cached context if available for this process
    if current_pid in _worker_context_cache:
        cached_context = _worker_context_cache[current_pid]
        logger.debug("Using cached worker context", pid=current_pid, worker_id=cached_context.get('worker_id', 'unknown'))
        return cached_context
    
    logger.info("Creating fresh worker context", pid=current_pid)
    
    try:
        # Import here to avoid circular dependencies
        from ...core import MotetStack, Config
        from ...core.tools import registry as tool_registry
        
        # Create worker-specific configuration
        config = Config()

        # Create unique worker ID that works across containers
        from .worker_utils import get_worker_id, get_lifecycle_worker_id, detect_worker_pool_type
        worker_id = get_worker_id()
        is_lifecycle_worker = worker_id == get_lifecycle_worker_id()
        
        # Detect worker pool type for concurrency model tracking (ADR-0033)
        pool_type = detect_worker_pool_type()

        embedding_service = None
        if is_lifecycle_worker:
            # Lifecycle/deploy workers are control-plane workers. They should not
            # require embedding-server availability or in-process SentenceTransformer
            # just to advertise deployment capability or execute lifecycle commands.
            config.enable_vector_memory = False
            stack = MotetStack(config=config)
        else:
            # Embedding service must exist before MotetStack when vector memory is enabled:
            # ValkeyVectorStore otherwise falls back to SentenceTransformer, which is not
            # shipped in the worker image after ADR-0107 M3.
            from ..embedding import create_embedding_service

            embedding_service = create_embedding_service(
                default_model=config.embedding_text_model or config.embedding_model,
                topology=config.embedding_topology,
                endpoint=config.embedding_endpoint,
                request_timeout_seconds=config.embedding_request_timeout_seconds,
                max_attempts=config.embedding_request_max_attempts,
                retry_backoff_seconds=config.embedding_retry_backoff_seconds,
                circuit_breaker_failure_threshold=config.embedding_circuit_breaker_failure_threshold,
                circuit_breaker_recovery_timeout_seconds=config.embedding_circuit_breaker_recovery_timeout_seconds,
            )

            stack = MotetStack(
                config=config,
                embedding_fn=embedding_service.embed,
                embedding_dim=embedding_service.get_embedding_dimension(),
            )
        
        logger.info("Creating worker context", 
                   worker_id=worker_id, 
                   pool_type=pool_type,
                   note="ADR-0038: no monitor election")
        
        # ADR-0069: Start MCP watcher immediately so it subscribes before subprocess publishes service_ready
        try:
            from ...core.distributed.worker_mcp_startup import ensure_mcp_watcher_started
            ensure_mcp_watcher_started(worker_id, stack.tool_registry)
        except Exception as e:
            logger.debug("MCP watcher start skipped or failed", worker_id=worker_id, error=str(e))
        
        # NOTE: Monitor election removed (ADR-0038)
        # Parent process now owns coordination threads via parent_coordinator.py
        # All workers are equal - no monitor role needed
        
        # Get available tools (before MCP initialization)
        _startup_log("Getting initial tool list", worker_id=worker_id)
        available_tools = stack.tool_registry.list_items()
        initial_tool_count = len(available_tools)
        
        _startup_log("Initial tool count before MCP registration", tool_count=initial_tool_count, worker_id=worker_id)
        
        # ADR-0069: No blocking MCP registration; watcher thread (started below) adds tools via PUB/SUB
        tool_count = initial_tool_count
        available_tools = stack.tool_registry.list_items()
        _startup_log("MCP tools will be added by watcher (event-driven)", worker_id=worker_id)
        
        # Set worker ID in environment for metrics collection
        os.environ['CELERY_WORKER_ID'] = worker_id
        
        # Initialize state registry for state-aware routing (sync)
        try:
            from ...core.distributed.state_registry import initialize_state_registry
            from ...core.distributed.redis_manager import get_redis_client
            
            # Use unified Redis manager for state registry
            state_redis_client = get_redis_client("state_registry")
            initialize_state_registry(redis_client=state_redis_client)
            logger.info("State registry initialized", worker_id=worker_id)
            
        except Exception as e:
            logger.warning("State registry initialization failed", error=str(e), worker_id=worker_id)
            # Continue without state registry
        
        # Initialize distributed metrics collection (sync)
        try:
            from ...core.observability.distributed_metrics import initialize_distributed_metrics
            from ...core.distributed.redis_manager import get_redis_client
            
            # Use unified Redis manager for distributed metrics
            metrics_redis_client = get_redis_client("distributed_metrics")
            initialize_distributed_metrics(redis_client=metrics_redis_client)
            logger.info("Distributed metrics initialized", worker_id=worker_id)
            
        except Exception as e:
            logger.warning("Distributed metrics initialization failed", error=str(e), worker_id=worker_id)
            # Continue without distributed metrics
        
        # Create distributed invoker for worker-to-worker calls (sync)
        try:
            from .command_invoker import DistributedInvokerNode
            
            distributed_invoker = DistributedInvokerNode(
                node_id=f"{worker_id}_invoker",
                enable_circuit_breakers=True,
                default_routing_strategy="least_loaded"
            )
            
            # Note: Initialization will happen lazily on first use to avoid event loop conflicts
            # The invoker will auto-initialize when execute_command is called
            logger.info("Distributed invoker created", worker_id=worker_id, note="will initialize on first use")
            
        except Exception as e:
            logger.warning("Distributed invoker creation failed", error=str(e), worker_id=worker_id)
            distributed_invoker = None
        
        _startup_log("Detecting worker capabilities", worker_id=worker_id)
        # Get worker capabilities
        capabilities = _detect_worker_capabilities(
            stack,
            available_tools,
            embedding_service=embedding_service,
        )
        _startup_log("Worker capabilities detected", capabilities_count=len(capabilities), worker_id=worker_id)
        
        # Get worker_router from distributed_invoker for Gather/Dispatch routing (ADR-0023)
        worker_router = None
        primary_node = getattr(distributed_invoker, 'primary_node', None)
        if primary_node is not None:
            worker_router = getattr(primary_node, 'worker_router', None)
        if not worker_router:
            try:
                from .routing.worker_router import WorkerRouter
                from ..distributed.worker_readiness import get_readiness_service

                routing_strategy = os.getenv("MOTET_ROUTING_STRATEGY", "least_loaded")
                worker_router = WorkerRouter(
                    readiness_service=get_readiness_service(),
                    default_strategy=routing_strategy,
                    enable_caching=True,
                    cache_ttl_seconds=30,
                )
                logger.info(
                    "Worker router initialized in context",
                    worker_id=worker_id,
                    strategy=routing_strategy,
                )
            except Exception as e:
                logger.warning(
                    "worker_router_init_failed",
                    error=str(e),
                    worker_id=worker_id,
                    exc_info=True,
                )
        
        _startup_log("About to import global_bus", worker_id=worker_id)
        # Import global_bus for event publishing
        from .events import global_bus
        _startup_log("global_bus imported", worker_id=worker_id)
        
        # Import global observer manager for event observation
        from .event_observer_manager import get_event_observer_manager
        observer_manager = get_event_observer_manager()
        
        # Initialize local inference client if local inference is enabled (ADR-0042)
        local_inference_client = None
        # Support both new (MOTET_LOCAL_INFERENCE_ENABLED) and legacy (MOTET_GPU_ENABLED) variable names
        local_inference_enabled = (
            os.getenv('MOTET_LOCAL_INFERENCE_ENABLED', 'false').lower() == 'true' or
            os.getenv('MOTET_GPU_ENABLED', 'false').lower() == 'true'
        )
        if local_inference_enabled:
            try:
                from ..models.local import LocalInferenceClient
                from ..distributed.redis_manager import UnifiedRedisManager
                
                redis_manager = UnifiedRedisManager()
                redis_client = redis_manager.get_sync_client()
                local_inference_client = LocalInferenceClient(redis_client)
                # ADR-0105: the worker no longer spawns a LocalInferenceManager subprocess.
                # The client routes requests to the sibling ``local-inference`` service over
                # the shared manager_id-keyed Redis Streams. If that service isn't running,
                # inference calls will time out (not crash the worker) — surface the contract
                # here so a missing sibling manager is diagnosable from worker logs.
                logger.info(
                    "Local inference client initialized in worker context",
                    manager_id=local_inference_client.manager_id,
                    note="connects to the sibling 'local-inference' manager service (ADR-0105); "
                         "ensure it is running with a matching MOTET_LOCAL_INFERENCE_MANAGER_ID",
                )
                # ADR-0105 §R3: record which sibling LocalInferenceManager this worker
                # routes to, so /managers/status can invert it into the manager's
                # served_workers set (mirrors the mcp_manager_id binding). Best-effort:
                # never fail worker startup over an observability binding.
                try:
                    from ..distributed.worker_readiness import WorkerReadinessService
                    WorkerReadinessService().update_worker_local_inference_manager_binding(
                        worker_id=worker_id,
                        local_inference_manager_id=local_inference_client.manager_id,
                    )
                except Exception as bind_err:
                    logger.debug(
                        "local_inference_manager_binding_publish_failed",
                        error=str(bind_err),
                    )
            except Exception as e:
                logger.warning("Failed to initialize local inference client", error=str(e))
                # Continue without local inference support - worker can still do other tasks
        
        # Set runtime stack for tools (enables memory/tool operations)
        stack.tool_registry.set_runtime_stack(stack)
        logger.debug("Runtime stack set for tools", has_memory=hasattr(stack, 'memory') and stack.memory is not None)

        if is_lifecycle_worker:
            logger.info(
                "function_discovery_skipped_lifecycle_worker",
                worker_id=worker_id,
                reason="lifecycle worker only needs deployment/lifecycle capabilities",
            )
            _startup_log("Getting sync Redis client", worker_id=worker_id)
            from ..distributed.redis_manager import get_sync_redis_client
            redis_client = get_sync_redis_client()
            _startup_log("Sync Redis client obtained", worker_id=worker_id)

            worker_context = {
                "worker_id": worker_id,
                "pool_type": pool_type,
                "stack": stack,
                "tool_registry": stack.tool_registry,
                "redis": redis_client,
                "embedding_service": None,
                "local_inference_client": local_inference_client,
                "function_discovery_store": None,
                "memory_manager": getattr(stack, 'memory_manager', None),
                "event_bus": global_bus,
                "observer_manager": observer_manager,
                "capabilities": capabilities,
                "distributed_invoker": distributed_invoker,
                "worker_router": worker_router,
                "process_id": current_pid,
                "hostname": socket.gethostname(),
                "created_at": time.time(),
                "tool_count": len(available_tools),
            }
            _worker_context_cache[current_pid] = worker_context
            logger.info(
                "Lifecycle worker context created successfully",
                worker_id=worker_id,
                pid=current_pid,
                tools_count=len(available_tools),
                capabilities_count=len(capabilities),
            )
            return worker_context
        
        # Initialize FunctionDiscoveryVectorStore for hybrid tool discovery (ADR-0051)
        # NOTE: Hybrid search is required; failures should be loud (no fallback).
        function_discovery_store = None
        assert embedding_service is not None
        try:
            from ..tools.function_discovery_vector_store import FunctionDiscoveryVectorStore
            from ..workflow import WorkflowRegistry
            
            # ADR-0075 hard cutover: use one shared discovery persistence directory.
            # Workers no longer use per-PID isolated indices.
            persist_dir = (
                getattr(config, "function_discovery_persist_dir", None)
                or os.getenv("MOTET_FUNCTION_DISCOVERY_PERSIST_DIR")
                or "/tmp/imf_function_discovery_shared"
            )

            # Use stack embedding model (L12) for consistent vector indexing quality.
            embedding_model = getattr(config, "embedding_model", "sentence-transformers/all-MiniLM-L12-v2")

            # Create vector store
            function_discovery_store = FunctionDiscoveryVectorStore(
                persist_dir=persist_dir,
                embedding_model=embedding_model,
                embedding_fn=embedding_service.embed,
                embedding_dim=embedding_service.get_embedding_dimension(),
                enable_embedding_cache=True,
                enable_result_cache=True,
            )
            
            # Single-writer indexing for the shared discovery index (#156).
            # A rebuild drops the index and repopulates it from this worker's
            # registry, so it must happen at most once across all workers;
            # ensure_shared_index() adopts a published index when one exists and
            # otherwise serializes the rebuild on the writer lock.
            #
            # Built-in commands register lazily via DistributedCommand. Indexing
            # or reconcile before that import side effect runs leaves the
            # registry almost empty, so reconcile_registry_state deletes the
            # rest of the shared command catalog as "stale" (observed as
            # stale_commands_removed≈140 with commands_in_registry=1).
            from motet.core.commands.distributed import DistributedCommand

            DistributedCommand._ensure_commands_registered()
            from ..distributed.redis_manager import acquire_distributed_lock_sync
            lock_key = getattr(
                config,
                "function_discovery_writer_lock_key",
                "motet:function_discovery:index_writer",
            )
            lock_ttl = int(getattr(config, "function_discovery_writer_lock_ttl_seconds", 120) or 120)
            force_reindex = os.getenv('MOTET_FORCE_REINDEX_FUNCTIONS', 'false').lower() == 'true'
            index_outcome = function_discovery_store.ensure_shared_index(
                stack.tool_registry,
                WorkflowRegistry,
                lock_factory=lambda: acquire_distributed_lock_sync(
                    "function_discovery_startup",
                    lock_key,
                    ttl_seconds=lock_ttl,
                ),
                include_commands=True,  # Command types feed the help tool and discovery
                force_reindex=force_reindex,
                wait_timeout_seconds=float(
                    getattr(config, "function_discovery_index_wait_seconds", 180) or 180
                ),
            )
            logger.info(
                "function_discovery_vector_store_initialized",
                worker_id=worker_id,
                tool_count=len(available_tools),
                index_outcome=index_outcome,
            )

            # ADR-0069: Register incremental-index callbacks so the MCP watcher
            # can add/remove tools from the hybrid index as services come and go,
            # without a full re-index.
            _tool_reg = stack.tool_registry  # capture for closure
            _fd_lock_key = getattr(
                config,
                "function_discovery_writer_lock_key",
                "motet:function_discovery:index_writer",
            )
            _fd_lock_ttl = int(getattr(config, "function_discovery_writer_lock_ttl_seconds", 120) or 120)

            # Every worker's MCP services become ready at roughly the same moment
            # after a parallel restart, so contention on the writer lock is the
            # normal case, not the exception. Returning on a missed acquire (the
            # previous behaviour) silently dropped that worker's MCP tools from
            # the index for the lifetime of the process (#156).
            _fd_callback_wait_seconds = float(
                getattr(config, "function_discovery_index_wait_seconds", 180) or 180
            )

            def _under_writer_lock(operation: str, service_id: str, fn) -> None:
                from ..distributed.redis_manager import acquire_distributed_lock_sync
                from .concurrency_primitives import worker_sleep

                deadline = time.monotonic() + _fd_callback_wait_seconds
                attempt = 0
                while True:
                    attempt += 1
                    lock = acquire_distributed_lock_sync(
                        "function_discovery_callbacks",
                        _fd_lock_key,
                        ttl_seconds=_fd_lock_ttl,
                    )
                    if lock:
                        try:
                            fn()
                        finally:
                            try:
                                lock.release_sync()
                            except Exception:
                                pass  # lock release best-effort
                        return
                    if time.monotonic() >= deadline:
                        logger.error(
                            "function_discovery_writer_lock_unavailable",
                            operation=operation,
                            service_id=service_id,
                            attempts=attempt,
                            waited_seconds=_fd_callback_wait_seconds,
                            note="Index update abandoned; these tools will not be discoverable.",
                        )
                        return
                    worker_sleep(1.0)

            def _on_mcp_tools_added(service_id: str, tool_names: list) -> None:
                def _index() -> None:
                    count = function_discovery_store.index_tools_incremental(tool_names, _tool_reg)
                    if count > 0:
                        logger.info(
                            "function_discovery_incremental_callback",
                            service_id=service_id,
                            tools_indexed=count,
                        )

                try:
                    _under_writer_lock("index_tools_incremental", service_id, _index)
                except Exception as idx_err:
                    logger.warning(
                        "function_discovery_incremental_callback_failed",
                        service_id=service_id,
                        error=str(idx_err),
                    )

            def _on_mcp_tools_removed(service_id: str) -> None:
                def _remove() -> None:
                    count = function_discovery_store.remove_tools_for_service(service_id)
                    if count > 0:
                        logger.info(
                            "function_discovery_removal_callback",
                            service_id=service_id,
                            tools_removed=count,
                        )

                try:
                    _under_writer_lock("remove_tools_for_service", service_id, _remove)
                except Exception as rm_err:
                    logger.warning(
                        "function_discovery_removal_callback_failed",
                        service_id=service_id,
                        error=str(rm_err),
                    )

            from ..distributed.worker_mcp_startup import set_discovery_index_callbacks
            set_discovery_index_callbacks(
                on_added=_on_mcp_tools_added,
                on_removed=_on_mcp_tools_removed,
            )

            # Reconciliation: ensure tool/workflow/command docs converge to registry
            # state using a shared vector-store helper (minimal churn, no full rebuild).
            current_tools = stack.tool_registry.list_items()
            all_tool_names = list(current_tools.keys())
            all_workflow_ids = WorkflowRegistry.list_workflow_ids_used_for_tool()
            from motet.core.commands.command_type_registry import command_type_registry
            all_command_types = list(command_type_registry.get_all_registrations().keys())

            reconciliation_stats = function_discovery_store.reconcile_registry_state(
                tool_names=all_tool_names,
                workflow_ids=all_workflow_ids,
                command_types=all_command_types,
                tool_registry=stack.tool_registry,
                workflow_registry=WorkflowRegistry,
            )
            logger.info(
                "function_discovery_reconciliation",
                worker_id=worker_id,
                tools_in_registry=len(all_tool_names),
                workflows_in_registry=len(all_workflow_ids),
                commands_in_registry=len(all_command_types),
                **reconciliation_stats,
            )

            # Register callback so workflows added at runtime (e.g. per-motet bundles)
            # are immediately indexed without a full re-index.
            _wf_store_ref = function_discovery_store  # capture for closure

            def _on_workflow_registered(workflow_id: str) -> None:
                from motet.core.workflow.user_catalog import is_user_workflow_id

                if is_user_workflow_id(str(workflow_id or "")):
                    return
                try:
                    from ..distributed.redis_manager import acquire_distributed_lock_sync
                    lock = acquire_distributed_lock_sync(
                        "function_discovery_callbacks",
                        _fd_lock_key,
                        ttl_seconds=_fd_lock_ttl,
                    )
                    if not lock:
                        return
                    try:
                        count = _wf_store_ref.index_workflows_incremental([workflow_id], WorkflowRegistry)
                        if count > 0:
                            logger.info(
                                "function_discovery_workflow_registered_callback",
                                workflow_id=workflow_id,
                            )
                    finally:
                        try:
                            lock.release_sync()
                        except Exception:
                            pass  # lock release best-effort
                except Exception as cb_err:
                    logger.warning(
                        "function_discovery_workflow_callback_failed",
                        workflow_id=workflow_id,
                        error=str(cb_err),
                    )

            WorkflowRegistry.set_on_registered_callback(_on_workflow_registered)

            def _on_workflow_unregistered(workflow_id: str) -> None:
                from motet.core.workflow.user_catalog import is_user_workflow_id

                if is_user_workflow_id(str(workflow_id or "")):
                    return
                try:
                    from ..distributed.redis_manager import acquire_distributed_lock_sync
                    lock = acquire_distributed_lock_sync(
                        "function_discovery_callbacks",
                        _fd_lock_key,
                        ttl_seconds=_fd_lock_ttl,
                    )
                    if not lock:
                        return
                    try:
                        count = _wf_store_ref.remove_workflows_incremental([workflow_id])
                        if count > 0:
                            logger.info(
                                "function_discovery_workflow_unregistered_callback",
                                workflow_id=workflow_id,
                            )
                    finally:
                        try:
                            lock.release_sync()
                        except Exception:
                            pass  # lock release best-effort
                except Exception as cb_err:
                    logger.warning(
                        "function_discovery_workflow_unregister_callback_failed",
                        workflow_id=workflow_id,
                        error=str(cb_err),
                    )

            WorkflowRegistry.set_on_unregistered_callback(_on_workflow_unregistered)
            logger.info(
                "function_discovery_workflow_callback_registered",
                worker_id=worker_id,
                workflows_indexed=len(all_workflow_ids),
            )

        except Exception as e:
            logger.error(
                "function_discovery_vector_store_startup_failed",
                error=str(e),
                error_type=type(e).__name__,
                worker_id=worker_id,
                note="Hybrid function discovery is required; failing worker startup",
                exc_info=True
            )
            raise
        
        # Get sync Redis client for streaming and distributed commands
        _startup_log("Getting sync Redis client", worker_id=worker_id)
        from ..distributed.redis_manager import get_sync_redis_client
        redis_client = get_sync_redis_client()
        _startup_log("Sync Redis client obtained", worker_id=worker_id)
        
        # Create worker context (ADR-0038: no monitor fields, ADR-0033: added pool_type and embedding_service, ADR-0042: added local_inference_client, ADR-0051: added function_discovery_store)
        _startup_log("Creating worker context dict", worker_id=worker_id)
        event_bus_for_context = global_bus
        worker_context = {
            "worker_id": worker_id,
            "pool_type": pool_type,  # ADR-0033: Concurrency model tracking
            "stack": stack,
            "tool_registry": stack.tool_registry,
            "redis": redis_client,  # Add Redis client for streaming and distributed operations
            "embedding_service": embedding_service,  # ADR-0033: Synchronous embedding generation
            "local_inference_client": local_inference_client,  # ADR-0042: Local inference client (pure I/O, any pool type)
            "function_discovery_store": function_discovery_store,  # ADR-0051: Semantic search for tool/workflow discovery
            "memory_manager": getattr(stack, 'memory_manager', None),  # Add memory manager for memory operations
            "event_bus": event_bus_for_context,  # Add event bus for custom event publishing (ADR-0030, ADR-0065)
            "observer_manager": observer_manager,  # Add observer manager for event observation (ADR-0030)
            "capabilities": capabilities,
            "distributed_invoker": distributed_invoker,
            "worker_router": worker_router,  # Add worker_router for intelligent routing (ADR-0023)
            "process_id": current_pid,
            "hostname": socket.gethostname(),
            "created_at": time.time(),
            "tool_count": len(available_tools)
        }
        _startup_log("Worker context dict created", worker_id=worker_id)
        
        # Cache the context for this process
        _startup_log("Caching worker context", worker_id=worker_id)
        _worker_context_cache[current_pid] = worker_context
        _startup_log("Worker context cached", worker_id=worker_id)
        
        logger.info("Worker context created successfully",
                   worker_id=worker_id,
                   pid=current_pid,
                   tools_count=len(available_tools),
                   capabilities_count=len(capabilities),
                   note="Parent coordination managed by parent_coordinator.py (ADR-0038)")
        
        _startup_log("About to return worker context", worker_id=worker_id)
        return worker_context
        
    except Exception as e:
        logger.error("Failed to create worker context", error=str(e), exc_info=True)
        
        # Return minimal fallback context
        from .worker_utils import get_worker_id
        try:
            fallback_worker_id = get_worker_id()
        except Exception:
            fallback_worker_id = f"celery_worker_fallback_{current_pid}"
        
        # Detect pool type even in fallback scenario (ADR-0033)
        from .worker_utils import detect_worker_pool_type
        try:
            fallback_pool_type = detect_worker_pool_type()
        except Exception:
            fallback_pool_type = "unknown"
        
        fallback_context = {
            "worker_id": fallback_worker_id,
            "pool_type": fallback_pool_type,  # ADR-0033: Include pool type in fallback
            "stack": None,
            "tool_registry": None,
            "agent": None,
            "memory_manager": None,  # Add memory manager to fallback context
            "event_bus": None,  # Add event bus to fallback context (ADR-0030)
            "observer_manager": None,  # Add observer manager to fallback context (ADR-0030)
            "capabilities": [],
            "distributed_invoker": None,
            "worker_router": None,  # Add worker_router to fallback context (ADR-0023)
            "process_id": current_pid,
            "hostname": socket.gethostname(),
            "created_at": time.time(),
            "error": str(e)
        }
        
        # Cache the fallback context
        _worker_context_cache[current_pid] = fallback_context
        return fallback_context




def _clear_worker_context_cache(pid: Optional[int] = None) -> None:
    """
    Clear worker context cache for a specific PID or all PIDs.
    
    Args:
        pid: Process ID to clear cache for. If None, clears all cached contexts.
    """
    global _worker_context_cache
    
    if pid is not None:
        if pid in _worker_context_cache:
            del _worker_context_cache[pid]
            logger.debug("Cleared worker context cache", pid=pid)
        else:
            logger.debug("No cached context found", pid=pid)
    else:
        cleared_count = len(_worker_context_cache)
        _worker_context_cache.clear()
        logger.info("Cleared all worker context cache", entries=cleared_count)


def get_worker_context_cache_stats() -> Dict[str, Any]:
    """
    Get statistics about the worker context cache.
    
    Returns:
        Dict containing cache statistics
    """
    global _worker_context_cache
    
    stats = {
        "cached_contexts": len(_worker_context_cache),
        "cached_pids": list(_worker_context_cache.keys()),
        "cache_details": {}
    }
    
    for pid, context in _worker_context_cache.items():
        stats["cache_details"][pid] = {
            "worker_id": context.get("worker_id", "unknown"),
            "initialized_at": context.get("initialized_at", 0),
            "capabilities_count": len(context.get("capabilities", [])),
            "tools_count": len(context.get("available_tools", [])),
            "has_stack": context.get("stack") is not None,
        }
    
    return stats


def _local_inference_enabled() -> bool:
    """Whether local inference is turned on for this worker (env flag).

    Supports both the current ``MOTET_LOCAL_INFERENCE_ENABLED`` and the legacy
    ``MOTET_GPU_ENABLED`` variable names (ADR-0042).
    """
    return (
        os.getenv('MOTET_LOCAL_INFERENCE_ENABLED', 'false').lower() == 'true' or
        os.getenv('MOTET_GPU_ENABLED', 'false').lower() == 'true'
    )


def _available_local_models() -> list:
    """Registered local model names whose model file is actually present on disk.

    Resolves the local model registry (``MODEL_REGISTRY['local']`` paths, overridable
    via ``MOTET_LOCAL_MODEL_PATHS`` / ``MOTET_LOCAL_MODEL_DIR``) and keeps only the
    entries whose GGUF file exists. Used to advertise ``local_inference`` based on
    real availability rather than just an env flag (ADR-0104 Open Q10).
    """
    try:
        from ..models.local.inference_manager import get_model_registry
    except Exception as exc:  # local inference module optional
        logger.debug("local_model_registry_unavailable", error=str(exc))
        return []

    available = []
    for name, path in get_model_registry().items():
        try:
            if path and os.path.exists(path):
                available.append(name)
        except Exception:
            continue  # unreadable path entry; skip
    return sorted(available)


def _should_advertise_local_inference() -> bool:
    """Advertise ``local_inference`` only when a local model is truly usable.

    ADR-0104 Open Q10: a worker should not claim ``local_inference`` just because the
    env flag is set — routing to it would then fail at request time. We advertise when
    local inference is enabled AND either:
      - at least one registered local model file is present on disk (GGUF / llama.cpp),
      - or a GPU is present (vLLM/transformers fetch weights on demand on GPU hosts).
    Applies to cloud and edge workers alike, so edge devices with a local model
    advertise the capability too.
    """
    if not _local_inference_enabled():
        return False

    if _available_local_models():
        return True

    # GPU hosts (vLLM/transformers) can materialize weights on first request.
    try:
        from .hardware_detection import has_gpu
        if has_gpu():
            return True
    except Exception as exc:
        logger.debug("local_inference_gpu_probe_failed", error=str(exc))

    return False


def _detect_worker_capabilities(
    stack: Any,
    available_tools: list,
    *,
    embedding_service: Any = None,
) -> list:
    """
    Detect worker capabilities based on available components.
    
    Args:
        stack: Motet runtime instance
        available_tools: List of available tools
        embedding_service: Worker-local embedding service, when available
        
    Returns:
        List of capability strings
    """
    from .worker_utils import get_worker_id, get_lifecycle_worker_id

    if get_worker_id() == get_lifecycle_worker_id():
        # Lifecycle worker also handles bundle deploy orchestration (ADR-0071)
        return ["worker_lifecycle_management", "deployment"]

    capabilities = []

    # ADR-0095: Edge workers are focused compute endpoints — tool execution
    # (edge MCP tools) and LLM inference (external API calls). They advertise
    # a restricted capability set so the cloud WorkerRouter does not route
    # orchestration, memory, or scheduling commands to them.
    is_edge = bool(os.getenv("MOTET_EDGE_WORKER_ID", "").strip())

    if is_edge:
        capabilities.append("edge_execution")
        capabilities.append("model_inference")
        capabilities.append("text_generation")
        shell_on = os.getenv("MOTET_ENABLE_SHELL_EXEC", "").lower() in (
            "1",
            "true",
            "yes",
        )
        shell_url = (os.getenv("MOTET_SHELL_BRIDGE_URL") or "").strip()
        if shell_on and shell_url:
            capabilities.append("edge_shell_exec")

        pc_on = os.getenv("MOTET_ENABLE_PROCESS_CONTROL", "").lower() in (
            "1",
            "true",
            "yes",
        )
        pc_url = (os.getenv("MOTET_PROCESS_CONTROL_BRIDGE_URL") or "").strip()
        if pc_on and pc_url:
            capabilities.append("edge_process_control")

        if available_tools:
            capabilities.append("tool_execution")
            capabilities.append("worker_shell_exec")
            mcp_tools = [tool for tool in available_tools if tool.startswith("mcp.")]
            if mcp_tools:
                capabilities.append("mcp_tools")
                capabilities.append("external_integrations")
            tools_set = set(available_tools)
            # Clipboard capability mirrors registered edge tools; runtime bridge/pyclip
            # availability is validated when the tool executes.
            if "core.clipboard_read" in tools_set or "core.clipboard_write" in tools_set:
                capabilities.append("edge_clipboard")
            if "core.file_read" in tools_set:
                capabilities.append("edge_file_read")
            # ADR-0122: file_edit shares EDGE_FILE_WRITE; file_grep shares EDGE_FILE_SEARCH
            if "core.file_write" in tools_set or "core.file_edit" in tools_set:
                capabilities.append("edge_file_write")
            if "core.file_search" in tools_set or "core.file_grep" in tools_set:
                capabilities.append("edge_file_search")
            if any("file" in tool.lower() for tool in available_tools):
                capabilities.append("file_operations")
            if any("web" in tool.lower() or "http" in tool.lower() for tool in available_tools):
                capabilities.append("web_operations")
                capabilities.append("http_operations")
            if any("code" in tool.lower() or "python" in tool.lower() for tool in available_tools):
                capabilities.append("code_execution")
            if any("playwright" in tool.lower() or "browser" in tool.lower() or "screenshot" in tool.lower() for tool in available_tools):
                capabilities.append("browser_operations")

        # Local inference on edge (ADR-0104 Q10): a personal/edge device that hosts a
        # local model advertises it so the router can prefer co-located inference.
        if _should_advertise_local_inference():
            capabilities.append(WorkerCapability.LOCAL_INFERENCE.value)

        return sorted(capabilities)

    # --- Cloud worker capabilities (full set) ---

    capabilities.append("distributed_command_processing")
    capabilities.append("reasoning")
    capabilities.append("model_inference")
    capabilities.append("text_generation")

    # Additional model capabilities if models component is available
    if hasattr(stack, 'models') and stack.models:
        try:
            models = stack.models.list_models()
            if any("gpt" in model.lower() for model in models):
                capabilities.append("openai_models")
            if any("claude" in model.lower() for model in models):
                capabilities.append("anthropic_models")
        except Exception:
            pass  # optional model-list enrichment

    # Memory capabilities
    if hasattr(stack, 'memory') and stack.memory:
        capabilities.append("memory_operations")
        capabilities.append("conversation_memory")

    # Vector capabilities - needed for chat context (cloud pgvector)
    capabilities.append("vector_operations")
    if embedding_service is not None:
        capabilities.append("embeddings")

    # Tool capabilities
    if available_tools:
        capabilities.append("tool_execution")
        capabilities.append("worker_shell_exec")

        mcp_tools = [tool for tool in available_tools if tool.startswith("mcp.")]
        if mcp_tools:
            capabilities.append("mcp_tools")
            capabilities.append("external_integrations")

        if any("file" in tool.lower() for tool in available_tools):
            capabilities.append("file_operations")
        if any("web" in tool.lower() or "http" in tool.lower() for tool in available_tools):
            capabilities.append("web_operations")
            capabilities.append("http_operations")
        if any("code" in tool.lower() or "python" in tool.lower() for tool in available_tools):
            capabilities.append("code_execution")
        if any("playwright" in tool.lower() or "browser" in tool.lower() or "screenshot" in tool.lower() for tool in available_tools):
            capabilities.append("browser_operations")

    # Reasoning capabilities
    if hasattr(stack, 'reasoning') and stack.reasoning:
        capabilities.append("reasoning_strategies")

    # Event capabilities
    capabilities.append("event_delivery")
    capabilities.append("async_processing")

    capabilities.append("schedule_management")

    # Local inference capability (ADR-0042). Advertised only when a local model is
    # actually usable on this worker, not just when the env flag is set (ADR-0104 Q10).
    if _should_advertise_local_inference():
        capabilities.append(WorkerCapability.LOCAL_INFERENCE.value)

    # Video derivation (ADR-0118): ffmpeg-equipped workers advertise MEDIA_PROCESSING.
    import shutil

    cfg = getattr(stack, "config", None)
    force_media = bool(getattr(cfg, "worker_media_processing", False)) if cfg else False
    env_force = os.getenv("MOTET_WORKER_MEDIA_PROCESSING", "").lower() in ("1", "true", "yes")
    if force_media or env_force or (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        capabilities.append(WorkerCapability.MEDIA_PROCESSING.value)

    return sorted(capabilities)


def get_worker_info() -> Dict[str, Any]:
    """
    Get information about the current worker.
    
    Returns:
        Dict containing worker information
    """
    try:
        from .celery_app import get_celery_worker_info
        
        celery_info = get_celery_worker_info()
        
        return {
            "celery_worker_info": celery_info,
            "task_modules_loaded": {
                "worker_tasks": "worker_tasks" in globals(),
                "command_tasks": "command_tasks" in globals()
                # event_tasks removed - using EventObserverManager instead
            },
            "available_tasks": [
                # NOTE: imf.worker.warmup_with_coordination REMOVED (ADR-0038 Enhanced)
                #       Parent process handles registration automatically via parent_coordinator.py
                "imf.worker.shutdown",  # Delegates to _perform_worker_shutdown (manual/API use only)
                "imf.events.deliver",
                "imf.events.broadcast",
                "imf.events.cleanup",
                "imf.events.batch_deliver",
                CELERY_PROCESS_COMMAND_TASK,
                "imf.commands.batch_process",
                "imf.commands.retry",
                "imf.commands.health_check"
                # Note (ADR-0038): Background threads (heartbeat, health check, cleanup) are now
                # managed by the parent process via parent_coordinator.py for stability.
                # Child processes no longer run coordination threads.
            ]
        }
        
    except Exception as e:
        return {
            "error": str(e),
            "celery_worker_info": None
        }


# Ensure all task modules are properly imported and registered
def _ensure_tasks_registered():
    """Ensure all task modules are imported and their tasks are registered with Celery."""
    try:
        # Import all task modules to trigger task registration
        import motet.core.workers.worker_tasks
        import motet.core.workers.command_tasks
        import motet.core.workers.schedule_tasks  # ADR-0025: Scheduled commands
        # event_tasks removed - using EventObserverManager for event delivery
        
        logger.info("All task modules imported and registered")
        
    except Exception as e:
        logger.error("Error registering task modules", error=str(e), exc_info=True)


# Register tasks on module import
_ensure_tasks_registered()


# ============================================================================
# Celery Signal Handlers for Parent Process Coordination (ADR-0038)
# ============================================================================

from celery.signals import worker_ready, worker_shutdown as worker_shutdown_signal

# NOTE (ADR-0033): Worker ready handler moved to celery_app.py for pool-aware initialization
# The new initialize_worker_unified() handler handles:
# - Parent coordination (all pool types)
# - Worker context initialization (single-process pools: threads/eventlet/gevent)
# - Pool-specific EventObserver consumer spawning
# 
# This replaces the old on_worker_ready() handler which only did parent coordination.
# See: motet/core/eventing/celery_app.py - initialize_worker_unified()
# See: docs/architecture/decisions/ADR-0033-io-worker-support.md


@worker_shutdown_signal.connect
def on_worker_shutdown(sender, **kwargs):
    """
    Called when Celery worker is shutting down (ADR-0038).
    
    Stop parent coordination threads gracefully.
    """
    logger.info("Celery worker shutdown signal received")

    # ADR-0105 §R0 (2026-04-20): the worker no longer spawns or owns the MCP
    # manager subprocess. The sibling ``mcp-manager`` service is
    # supervised by docker compose / k8s; SIGTERM is delivered by the
    # orchestrator on stack teardown. No worker-side shutdown call is needed.

    from .parent_coordinator import is_celery_parent_process, shutdown_parent_coordination

    if is_celery_parent_process():
        worker_id = getattr(sender, 'hostname', 'unknown_worker')

        logger.info(f"🛑 Shutting down parent coordination for {worker_id}")

        result = shutdown_parent_coordination(worker_id)

        if result.get('status') == 'shutdown':
            logger.info(f"✅ Parent coordination shutdown complete")
        else:
            logger.error(f"❌ Parent coordination shutdown failed: {result}")


# Export the Celery app for external use
celery_app = get_celery_app()

# Export commonly used functions
__all__ = [
    # Celery app
    'celery_app',
    'get_celery_app',
    
    # Worker context
    '_create_worker_context',
    '_clear_worker_context_cache',
    'get_worker_context_cache_stats',
    'get_worker_info',
    
    # Worker tasks
    'worker_shutdown',  # Manual shutdown only - heartbeat/health_check now via background threads
    
    # Command tasks
    'process_distributed_command',
    'batch_process_commands',
    'retry_failed_command',
    'command_processor_health_check'
]