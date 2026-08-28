"""
Motet - Celery App

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-23

Description:
    Worker celery app for the Motet distributed framework.

Dependencies:
    - typing: Type hints and annotations
    - Base interfaces and implementations

Usage:
    from motet.core.workers.celery_app import CeleryApp

Notes:
    - Provides core functionality
    - Integrates with distributed architecture
    - Global ``task_time_limit`` is 600s; ``WorkerCommunicator.send_task``
      still overrides per command from ``timeout_seconds``
"""


import os
from celery import Celery
from celery.signals import worker_process_init, worker_ready
from typing import Optional

import structlog

from motet.core.constants import DEFAULT_REDIS_URL

_celery_app: Optional[Celery] = None

logger = structlog.get_logger(__name__)


def get_celery_app() -> Celery:
    """
    Get or create the global Celery application.
    
    This function ensures a single Celery app instance is used throughout
    the application, with consistent configuration.
    
    Returns:
        Celery: The configured Celery application instance
    """
    global _celery_app
    if _celery_app is not None:
        return _celery_app
    
    try:
        from celery import Celery  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("Celery not available - install with: pip install celery[redis]") from exc
    
    # Get configuration from environment.
    # Priority: MOTET_CELERY_BROKER_URL → CELERY_BROKER_URL (docker-compose workers) →
    #           MOTET_REDIS_URL → default.  For rediss:// URLs that lack ssl_cert_reqs,
    #           append it from MOTET_REDIS_SSL_CERT_REQS so processes with
    #           MOTET_REDIS_URL=rediss://... (without the param) can send Celery tasks.
    def _resolve_broker_url() -> str:
        raw = (
            os.getenv("MOTET_CELERY_BROKER_URL") or
            os.getenv("CELERY_BROKER_URL") or
            os.getenv("MOTET_REDIS_URL") or
            DEFAULT_REDIS_URL
        )
        if raw.startswith("rediss://") and "ssl_cert_reqs" not in raw:
            cert_reqs = (os.getenv("MOTET_REDIS_SSL_CERT_REQS") or "").upper()
            if cert_reqs in ("NONE", "CERT_NONE"):
                sep = "&" if "?" in raw else "?"
                raw = f"{raw}{sep}ssl_cert_reqs=CERT_NONE"
            elif cert_reqs in ("OPTIONAL", "CERT_OPTIONAL"):
                sep = "&" if "?" in raw else "?"
                raw = f"{raw}{sep}ssl_cert_reqs=CERT_OPTIONAL"
        return raw

    broker_url = _resolve_broker_url()
    result_backend = (
        os.getenv("MOTET_CELERY_RESULT_BACKEND") or
        os.getenv("CELERY_RESULT_BACKEND") or
        broker_url
    )
    
    # Create Celery app with descriptive name
    app = Celery("motet_distributed_system", broker=broker_url, backend=result_backend)
    
    # Configure Celery for optimal distributed performance
    app.conf.update(
        # Task modules to import on worker startup — ensures imf.commands.process and friends
        # are registered BEFORE the consumer starts receiving tasks.  Without this, gevent's
        # cooperative scheduling can let the consumer run before @celery_app.task decorators
        # execute, causing "Received unregistered task" errors.
        include=[
            'motet.core.workers.command_tasks',
            'motet.core.workers.worker_tasks',
            'motet.core.workers.schedule_tasks',
        ],

        # Serialization
        task_serializer='json',
        accept_content=['json'],
        result_serializer='json',
        
        # Timezone
        timezone='UTC',
        enable_utc=True,
        
        # Task execution
        task_track_started=True,
        task_time_limit=600,  # 10 minutes hard limit (per-command send_task may override)
        task_soft_time_limit=540,  # 9 minutes soft limit
        task_acks_late=True,  # Acknowledge after task completion
        
        # Worker configuration
        worker_prefetch_multiplier=1,  # Disable prefetching for better load balancing
        worker_disable_rate_limits=False,
        worker_max_tasks_per_child=1000,  # Restart workers after 1000 tasks
        worker_max_memory_per_child=500000,  # Restart workers after 500MB memory usage
        
        # Performance optimizations
        task_compression='gzip',
        result_compression='gzip',
        result_expires=3600,  # Results expire after 1 hour
        
        # Routing and queues
        task_default_queue='default',
        task_default_exchange='default',
        task_default_exchange_type='direct',
        task_default_routing_key='default',
        
        # Monitoring and debugging
        worker_send_task_events=True,
        task_send_sent_event=True,
        
        # Error handling
        task_reject_on_worker_lost=True,
        # Global default stays False. ``imf.commands.process`` sets
        # ignore_result=True so command wait uses Motet ``cmd:outcome`` (#229).
        task_ignore_result=False,
    )
    
    # Define task routes for better organization
    app.conf.task_routes = {
        
        # Worker management tasks
        'imf.worker.*': {'queue': 'worker_management'},
        
        # Event delivery tasks
        'imf.events.*': {'queue': 'event_delivery'},
        
        # Command processing tasks (high priority)
        'imf.commands.*': {'queue': 'command_processing'},


        'imf.distributed.*': {'queue': 'command_processing'},
        
        # Default queue for everything else
        '*': {'queue': 'default'},
    }
    
    # Configure Celery Beat schedule for periodic tasks
    # NOTE: Removed centralized heartbeat and health checks - each worker manages its own
    # Individual workers run their own background threads for heartbeats and health checks
    app.conf.beat_schedule = {
        # Schedule cleanup task - runs every hour
        'cleanup-expired-schedules': {
            'task': 'imf.schedules.cleanup',
            'schedule': 3600.0,  # 1 hour
        },
        # Delayed schedule check - runs every 5 seconds
        'check-delayed-schedules': {
            'task': 'imf.schedules.delayed_check',
            'schedule': 5.0,  # 5 seconds
        },
        # Conditional schedule check - runs every minute
        'check-conditional-schedules': {
            'task': 'imf.schedules.condition_check',
            'schedule': 60.0,  # 1 minute
        },
        # Recurring schedule check - runs every 2 seconds
        'check-recurring-schedules': {
            'task': 'imf.schedules.recurring_check',
            'schedule': 2.0,  # 2 seconds
        },
    }
    
    _celery_app = app
    return app


# Create the global app instance
celery_app = get_celery_app()


def configure_celery_for_testing():
    """Configure Celery for testing environments"""
    global _celery_app
    
    if _celery_app is None:
        _celery_app = get_celery_app()
    
    # Use eager execution for tests
    _celery_app.conf.update(
        task_always_eager=True,
        task_eager_propagates=True,
        broker_connection_retry_on_startup=True,
    )
    
    return _celery_app


def get_celery_worker_info():
    """Get information about the current Celery worker context"""
    try:
        from celery import current_task
        from celery.worker.request import Request
        
        info = {
            'in_worker_context': current_task is not None,
            'task_id': getattr(current_task, 'request', {}).get('id') if current_task else None,
            'worker_id': None,
            'queue': None
        }
        
        if current_task and hasattr(current_task, 'request'):
            request = current_task.request
            if isinstance(request, Request):
                info['worker_id'] = getattr(request, 'hostname', None)
                info['queue'] = getattr(request, 'delivery_info', {}).get('routing_key')
        
        return info
        
    except Exception:
        return {
            'in_worker_context': False,
            'task_id': None,
            'worker_id': None,
            'queue': None
        }


# Backward compatibility - maintain the old function name
def get_celery_app_legacy():
    """Legacy function name - use get_celery_app() instead"""
    import warnings
    warnings.warn(
        "get_celery_app_legacy() is deprecated, use get_celery_app() instead",
        DeprecationWarning,
        stacklevel=2
    )
    return get_celery_app()


@worker_ready.connect
def cleanup_redis_on_worker_startup(**kwargs):
    """
    Clean up this worker's Redis entries when this Celery worker starts.

    Only clears keys belonging to THIS worker so other workers' registration
    and utilization data are preserved. Does not delete this worker's
    registration key; instead sets state to STARTING so the worker stays
    visible in the UI (matching the state set by the lifecycle service when
    Start was clicked). Parent coordinator / heartbeat will overwrite with
    full registration once ready.

    This signal fires once per Celery worker (main process), before fork pool
    workers are spawned.
    """
    try:
        from .worker_utils import get_worker_id
        from ..distributed.redis_manager import get_redis_manager

        worker_id = get_worker_id()
        logger.info("celery_worker_startup_redis_cleanup_start", worker_id=worker_id)

        redis_manager = get_redis_manager()
        redis_client = redis_manager.get_sync_client()
        cleanup_count = 0

        # 1. Remove only this worker from ready set (do not clear entire set)
        ready_workers_key = "worker:ready"
        if redis_client.sismember(ready_workers_key, worker_id):
            redis_client.srem(ready_workers_key, worker_id)
            cleanup_count += 1

        # 2. Register worker in STARTING state so the key exists for heartbeat/health threads.
        #    Heartbeat requires the worker key to exist; initialize_parent_coordination will
        #    overwrite with full capabilities and mark_worker_ready when context is ready.
        from ..distributed.worker_readiness import get_readiness_service
        readiness_service = get_readiness_service()
        readiness_service.register_worker(worker_id, capabilities=[])

        # 3. Clear only this worker's utilization (stale from before stop)
        util_key = f"worker:utilization:{worker_id}"
        if redis_client.exists(util_key):
            redis_client.delete(util_key)
            cleanup_count += 1

        # 4. Clear only this worker's process health (pattern may vary; try worker-scoped key)
        health_key = f"process_health:{worker_id}"
        if redis_client.exists(health_key):
            redis_client.delete(health_key)
            cleanup_count += 1

        # 5. Do not clear monitor locks on single-worker startup; they are shared
        logger.info("celery_worker_startup_redis_cleanup_complete", worker_id=worker_id, cleanup_count=cleanup_count)

    except Exception as e:
        logger.warning(
            "celery_worker_startup_redis_cleanup_failed_continuing",
            error=str(e),
            exc_info=True,
        )


# NOTE (ADR-0105 §R0, 2026-04-20): the @worker_ready handler that previously
# spawned ``MCPInstanceManager`` as a worker subprocess
# (start_mcp_instance_manager_in_parent_process) has been deleted. The MCP
# manager now runs as a sibling deployment — ``mcp-manager`` compose
# service in dev / edge, sidecar pod in cloud k8s — discovered by workers via
# ``MOTET_MCP_MANAGER_ENDPOINT`` and addressed by ``MOTET_MCP_MANAGER_ID``.
# See docs/architecture/decisions/ADR-0105-decouple-mcp-instance-manager-from-worker-lifecycle.md.


# NOTE (ADR-0105, 2026-06-03): the @worker_ready handler that previously
# spawned ``LocalInferenceManager`` as a worker subprocess
# (start_local_inference_manager_in_parent_process) has been deleted. Mirroring the
# MCP hoist above, the local inference manager now runs as an independent supervised
# sibling service — the ``local-inference`` compose service in dev / edge, a sibling
# Deployment in cloud k8s. It is decoupled from any single worker's lifecycle, so it
# survives worker restarts while keeping models warm. Workers reach it via the shared
# ``MOTET_LOCAL_INFERENCE_MANAGER_ID`` Redis Streams routing prefix; the in-worker
# ``LocalInferenceClient`` (built in workers/tasks.py) publishes on that prefix.
# See docs/architecture/decisions/ADR-0105-decouple-mcp-instance-manager-from-worker-lifecycle.md
# §"Same pattern applies to LocalInferenceManager".


@worker_ready.connect
def initialize_worker_unified(**kwargs):
    """
    Unified worker initialization for all pool types (ADR-0033).
    
    This signal fires in the parent process for ALL pool types.
    
    Behavior by pool type:
    - fork: Initialize parent coordination only (children use worker_process_init)
    - threads/eventlet/gevent: Initialize BOTH parent coordination AND worker context
    
    NOTE: worker_process_init signal DOES NOT FIRE on eventlet/gevent/threads pools!
    Those pools run in a single process, so we initialize everything here.
    
    See: docs/architecture/decisions/ADR-0033-io-worker-support.md
    """
    import os
    from .worker_utils import get_worker_id, detect_worker_pool_type
    from .parent_coordinator import (
        get_celery_concurrency_from_args,
        initialize_parent_coordination,
        is_celery_parent_process,
    )
    from .worker_initialization import initialize_worker_context
    
    pool_type = detect_worker_pool_type()
    worker_id = get_worker_id()
    pid = os.getpid()
    
    logger.info("celery_worker_ready", pool_type=pool_type, pid=pid, worker_id=worker_id)

    try:
        from motet.core.distributed.redis_manager import (
            get_redis_manager,
            warn_if_redis_pool_below_concurrency,
        )

        warn_if_redis_pool_below_concurrency(
            max_connections=get_redis_manager().config.max_connections,
            concurrency=get_celery_concurrency_from_args(),
        )
    except Exception as e:
        logger.warning(
            "redis_pool_concurrency_check_failed",
            worker_id=worker_id,
            error=str(e),
            error_type=type(e).__name__,
        )

    # ADR-0069: Start MCP watcher as early as possible so it subscribes before
    # the MCP subprocess publishes service_ready signals.  The watcher only needs
    # worker_id + a ToolRegistry instance; it runs in a daemon thread and will
    # poll/subscribe independently of the heavy _create_worker_context() flow.
    try:
        from ..distributed.worker_mcp_startup import ensure_mcp_watcher_started
        from ..tools import registry as _tool_registry_singleton
        # Use the module-level singleton ToolRegistry (same instance as MotetStack.tool_registry)
        ensure_mcp_watcher_started(worker_id, _tool_registry_singleton)
    except Exception as e:
        logger.warning("mcp_watcher_early_start_failed", worker_id=worker_id, error=str(e))

    # Optional: seed model profiles from config file into Redis (ADR-0064).
    # This is guarded by a distributed lock so it's safe across multiple workers.
    try:
        from ..config import Config
        cfg = Config()
        if bool(getattr(cfg, "seed_model_profiles_on_startup", False)):
            from ..models.profile_seeding import seed_model_profiles_if_configured_sync
            result = seed_model_profiles_if_configured_sync(cfg)
            logger.info("model_profile_seed_worker_ready_result", result=result, worker_id=worker_id)
    except Exception as e:
        logger.error("model_profile_seed_worker_ready_failed", error=str(e), exc_info=True, worker_id=worker_id)
        # Log loud but don't crash the entire signal handler — downstream init
        # (parent coordination, worker context, MCP watcher) must still proceed.
    
    # Step 1: Initialize parent coordination (ALL pool types)
    # This starts heartbeat, health check, and cleanup threads in parent process
    if is_celery_parent_process():
        # Give MCP instance manager subprocess a head start on cold start (e.g. first docker up).
        # The MCP start thread just spawned the subprocess; the subprocess needs time to load and
        # ADR-0069: No fixed startup delay; parent uses watcher thread and per-service BLPOP signals.
        logger.debug("celery_parent_coordination_init_start", worker_id=worker_id)
        result = initialize_parent_coordination(worker_id)
        if result.get('status') == 'initialized':
            logger.info("celery_parent_coordination_initialized", worker_id=worker_id)
        else:
            logger.error(
                "celery_parent_coordination_init_failed",
                worker_id=worker_id,
                result=result,
            )
    else:
        logger.debug("celery_child_process_skipping_parent_coordination", worker_id=worker_id, pid=pid)
    
    # Step 2: Initialize worker context (ONLY for single-process pools)
    # Fork pool uses worker_process_init for per-child initialization
    if pool_type in ["threads", "eventlet", "gevent"]:
        # These pools run in single process - initialize worker context now
        logger.info("celery_single_process_pool_init_worker_context", pool_type=pool_type, worker_id=worker_id)
        result = initialize_worker_context(pool_type)
        
        if result['status'] == 'success':
            logger.info(
                "celery_worker_context_initialized",
                worker_id=result.get("worker_id"),
                pool_type=result.get("pool_type"),
                tool_count=(result.get("context", {}) or {}).get("tool_count", 0),
                capabilities_count=len((result.get("context", {}) or {}).get("capabilities", [])),
            )
        else:
            logger.warning(
                "celery_worker_context_init_failed_lazy_init",
                pool_type=pool_type,
                worker_id=worker_id,
                error=result.get("error"),
            )
    else:
        # Fork pool: worker_process_init will handle worker context initialization
        logger.info("celery_fork_pool_child_init_deferred", pool_type=pool_type, worker_id=worker_id)


@worker_process_init.connect
def initialize_fork_pool_worker(**kwargs):
    """
    Initialize fork pool child processes (ADR-0033).
    
    This signal ONLY fires for fork pool workers (multi-process).
    For threads/eventlet/gevent, initialization happens in worker_ready.
    
    Background thread spawn ensures we stay under the 4-second signal timeout.
    
    See: docs/architecture/decisions/ADR-0033-io-worker-support.md
    """
    import os
    import threading
    from .worker_utils import detect_worker_pool_type, get_worker_id
    from .worker_initialization import initialize_worker_context
    
    pool_type = detect_worker_pool_type()
    pid = os.getpid()
    worker_id = get_worker_id()
    
    logger.debug("celery_fork_pool_child_starting", pid=pid, worker_id=worker_id)
    
    # Verify this is actually a fork pool (defensive programming)
    if pool_type != "fork":
        logger.warning(
            "celery_worker_process_init_non_fork_pool_skipping",
            pool_type=pool_type,
            pid=pid,
            worker_id=worker_id,
        )
        return
    
    # Initialize in background thread to stay under 4-second signal timeout
    def _background_init():
        """Background thread for worker context initialization"""
        result = initialize_worker_context(pool_type)
        
        if result['status'] == 'success':
            logger.info(
                "celery_fork_pool_child_initialized",
                pid=pid,
                worker_id=result.get("worker_id"),
                tool_count=(result.get("context", {}) or {}).get("tool_count", 0),
                capabilities_count=len((result.get("context", {}) or {}).get("capabilities", [])),
                parent_registered=True,
            )
        else:
            logger.warning(
                "celery_fork_pool_child_init_failed_lazy_init",
                pid=pid,
                worker_id=worker_id,
                error=result.get("error"),
            )
    
    thread = threading.Thread(
        target=_background_init,
        daemon=True,
        name=f"WorkerInit-{pid}"
    )
    thread.start()
    logger.debug("celery_fork_pool_child_init_started_background", pid=pid, worker_id=worker_id)
