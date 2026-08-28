"""
Motet - MCP Instance Manager

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Sibling MCP process owner: wires lifecycle, credentials, supervisor, signals, and control-plane mixins. Replica pooling is not implemented.

Dependencies:
    - asyncio: per-instance locks and background loops
    - structlog: structured logging

Usage:
    python -m motet.core.tools.mcp_motet.proxy.mcp_instance_manager --manager-id mcp-local-default

Notes:
    - Public import path remains motet.core.tools.mcp_motet.proxy.mcp_instance_manager
    - This module is wire-up + CLI; mixins own create/destroy, health, and control
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

import structlog
import yaml

from motet.core.tools.mcp_motet.manager.config import (
    InstanceManagerConfig,
    MCPInstanceConfig,
    normalize_server_config_dict,
)
from motet.core.tools.mcp_motet.manager.control_plane import ControlPlaneMixin
from motet.core.tools.mcp_motet.manager.credentials import CredentialsMixin
from motet.core.tools.mcp_motet.manager.lifecycle import LifecycleMixin
from motet.core.tools.mcp_motet.manager.restart_budget import ServiceRestartBudget
from motet.core.tools.mcp_motet.manager.signals import SignalsMixin
from motet.core.tools.mcp_motet.manager.supervisor import SupervisorMixin

logger = structlog.get_logger(__name__)


class MCPInstanceManager(
    LifecycleMixin,
    CredentialsMixin,
    SupervisorMixin,
    SignalsMixin,
    ControlPlaneMixin,
):
    """
    Supervises MCP server instances for one sibling manager process.

    Per-service isolation: a hung or crashed child cannot take down other
    services or this process. Create/destroy/restart are serialized per
    instance_id. Replica pooling is not supported (ADR-0058 identity keys).
    """
    def __init__(
        self,
        config_path: Optional[str] = None,
        config_dict: Optional[Dict[str, Any]] = None,
        log_level: str = "INFO",
        metrics_port: int = 9090,
        health_port: int = 9091,
        worker_id: Optional[str] = None,
        manager_id: Optional[str] = None,
        startup_command_context: Optional[Any] = None
    ):
        """
        Initialize the MCP Instance Manager.

        Args:
            config_path: Path to YAML/JSON configuration file
            config_dict: Configuration dictionary (alternative to file)
            log_level: Logging level
            metrics_port: Port for metrics endpoint
            health_port: Port for health check endpoint
            worker_id: Worker identifier for stream filtering / telemetry. Under
                ADR-0105 this is purely an observability tag identifying which
                worker bootstrapped the manager (sidecar topology) or which
                worker is being served by a shared compose-level manager. It is
                **not** the bus routing key — see ``manager_id``.
            manager_id: Stable identifier for this manager process (ADR-0105
                §R2/§R3). Used as the prefix on the Redis Streams MCP request /
                response streams, the PUB-SUB lifecycle channel, and the
                readiness set. When omitted, falls back to ``worker_id`` for
                back-compat with single-process tests.
            startup_command_context: Optional CommandContext for vault credential prefetch at startup
        """
        self.config_path = config_path
        self.config_dict = config_dict
        self.log_level = log_level
        self.metrics_port = metrics_port
        self.health_port = health_port
        self.worker_id = worker_id
        # ADR-0105 §R2: manager_id is the bus prefix. Fall back to worker_id
        # when the caller is single-process (tests / one-off scripts).
        self.manager_id = manager_id or worker_id
        self.startup_command_context = startup_command_context
        
        # Instance tracking
        self.instances: Dict[str, MCPInstance] = {}
        self.service_configs: Dict[str, MCPInstanceConfig] = {}
        # service_id -> owning instance_id for fixed-port local HTTP singleton mode
        self._http_singleton_owner_by_service: Dict[str, str] = {}
        self._instance_locks: Dict[str, asyncio.Lock] = {}
        self._restart_budget = ServiceRestartBudget()
        self._disabled_services: Set[str] = set()
        self._service_last_error: Dict[str, str] = {}
        self._service_last_ready_at: Dict[str, float] = {}
        self._service_last_removed_at: Dict[str, float] = {}
        self._service_last_restarted_at: Dict[str, float] = {}
        self._service_tool_names: Dict[str, List[str]] = {}
        self._streams_without_instance: int = 0
        
        # State management
        self.running = False
        self.shutdown_event = asyncio.Event()
        
        # Metrics
        self.stats = {
            "instances_created": 0,
            "instances_destroyed": 0,
            "restarts": 0,
            "health_checks": 0,
            "health_failures": 0,
            "start_time": time.time()
        }
        
        # Manager status registry for Redis-based status publishing
        from motet.core.distributed.manager_status import ManagerStatusRegistry, ManagerType
        self.status_registry = None  # Will be initialized in start()
        self.manager_type = ManagerType.MCP
        
        # Background tasks
        self.health_monitor_task: Optional[asyncio.Task] = None
        self.cleanup_task: Optional[asyncio.Task] = None
        self.context_monitor_task: Optional[asyncio.Task] = None
        self._control_command_task: Optional[asyncio.Task] = None
        self._startup_status_task: Optional[asyncio.Task] = None
        
        # HTTP servers for metrics and health
        self.metrics_app: Optional[web.Application] = None
        self.health_app: Optional[web.Application] = None
        self.metrics_runner: Optional[web.AppRunner] = None
        self.health_runner: Optional[web.AppRunner] = None
        
        # Event loop reference for async proxy creation from events
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        
        # Event-driven observer for fast proxy creation
        self._proxy_observer: Optional[Any] = None
        
        # Event-driven observer for auth credential refresh (ADR-0057)
        self._auth_observer: Optional[Any] = None
        
        # Event observer manager for MainProcess (to receive events)
        self._event_observer_manager: Optional[Any] = None
        self._observer_consumer_task: Optional[asyncio.Task] = None
        
        logger.info(
            "mcp_instance_manager_initializing",
            manager_id=self.manager_id,
            worker_id=self.worker_id,
        )

    def _instance_lock(self, instance_id: str) -> asyncio.Lock:
        """Return the asyncio lock for one instance key (event-loop local)."""
        lock = self._instance_locks.get(instance_id)
        if lock is None:
            lock = asyncio.Lock()
            self._instance_locks[instance_id] = lock
        return lock

    def _create_timeout_seconds(self) -> float:
        """Timeout for create/restart (same knob as bootstrap per-service wait)."""
        raw = (os.getenv("MOTET_MCP_PER_SERVICE_INIT_TIMEOUT_SECONDS") or "240").strip()
        try:
            return max(5.0, float(raw))
        except ValueError:
            return 240.0

    async def start(self) -> None:
        """
        Start the Instance Manager and create all configured instances.
        """
        try:
            logger.info("🚀 Starting MCP Instance Manager...")
            
            # Load configuration
            config = await self._load_configuration()
            
            # Store service configurations
            for service_config in config.services:
                self.service_configs[service_config.service_id] = service_config
                logger.info(f"📝 Registered service: {service_config.service_id}")

            # Docker MCP containers survive manager process death; sweep orphans for this
            # manager before spawning new ones. ADR-0105 §R2: keyed on manager_id; falls
            # back to worker_id when running single-process (tests / one-off scripts).
            # Sweep matches both the raw id and the cloud_ form (ADR-0135 HTTP leftovers).
            sweep_id = self.manager_id or self.worker_id or ""
            try:
                from motet.core.execution.mcp_docker_cleanup import sweep_mcp_containers_for_worker
                sweep_mcp_containers_for_worker(sweep_id)
            except Exception as e:
                logger.warning(
                    "mcp_docker_startup_sweep_failed",
                    error=str(e),
                    manager_id=self.manager_id,
                    worker_id=self.worker_id,
                )

            # Ops dashboard: show manager immediately while instances are created (can take many minutes).
            await self._publish_status_to_redis(lifecycle_status="starting")
            self._startup_status_task = asyncio.create_task(self._startup_status_heartbeat())
            try:
                # Pre-fetch vault credentials before creating instances
                await self._prefetch_vault_credentials()

                # Create initial instances for each service.
                # Each service gets its own timeout so one slow/hung service (e.g. npx
                # downloading packages, xvfb startup) doesn't block the rest.
                # Docker MCP + cold image pulls / Playwright / npx routinely exceed 90s; publishing
                # service_ready requires create_instance (incl. transport.start + handshake) to finish.
                per_service_timeout = int(os.getenv("MOTET_MCP_PER_SERVICE_INIT_TIMEOUT_SECONDS", "240"))
                await self._bootstrap_configured_services(per_service_timeout)

                self.running = True
            finally:
                if self._startup_status_task:
                    self._startup_status_task.cancel()
                    try:
                        await self._startup_status_task
                    except asyncio.CancelledError:
                        pass
                    self._startup_status_task = None

            # Store event loop reference for event-driven proxy creation
            self._loop = asyncio.get_running_loop()
            
            # Register event-driven observer for fast proxy creation
            try:
                from motet.core.tools.mcp_motet.proxy.mcp_proxy_observer import (
                    register_mcp_proxy_observer,
                )
                self._proxy_observer = register_mcp_proxy_observer(self)
                logger.info("✅ Event-driven proxy creation enabled (fast path)")
                
                # Register auth observer for OAuth credential refresh (ADR-0057)
                from motet.core.tools.mcp_motet.proxy.mcp_auth_observer import (
                    register_mcp_auth_observer,
                )
                self._auth_observer = register_mcp_auth_observer(self)
                logger.info("✅ Event-driven auth refresh enabled (ADR-0057)")
                
                # Start EventObserverManager in MainProcess to receive events
                # This is needed because EventObserverManager normally runs in worker processes,
                # but our MCPProxyCreationObserver needs to run in the MainProcess where
                # MCPInstanceManager lives
                from motet.core.workers.event_observer_manager import get_event_observer_manager
                self._event_observer_manager = get_event_observer_manager()
                
                # Start consuming events in the background
                self._observer_consumer_task = asyncio.create_task(
                    self._event_observer_manager.start_consuming()
                )
                logger.info("✅ Started EventObserverManager in MainProcess for event-driven proxy creation")
                
            except Exception as e:
                logger.warning(
                    "mcp_event_observer_register_failed",
                    error=str(e),
                    note="On-demand instances still created by MotetMCPClient stream wait + observer retry",
                )
            
            await self._publish_status_to_redis()
            self.health_monitor_task = asyncio.create_task(self._health_monitor_loop())
            self.cleanup_task = asyncio.create_task(self._cleanup_loop())
            self.context_monitor_task = asyncio.create_task(self._context_monitor_loop())
            self._control_command_task = asyncio.create_task(self._control_command_loop())

            try:
                await self._start_http_endpoints(config)
            except Exception as e:
                logger.error(
                    "mcp_http_endpoints_start_failed",
                    error=str(e),
                    exc_info=True,
                    note="Manager continues; /metrics and /health unavailable",
                )

            await self._publish_ready_signal()
            await self._publish_all_service_statuses()

            logger.info(
                "mcp_instance_manager_started",
                instance_count=len(self.instances),
                service_count=len(self.service_configs),
            )

        except Exception as e:
            logger.error(f"❌ Failed to start Instance Manager: {e}", exc_info=True)
            raise

    async def shutdown(self) -> None:
        """
        Gracefully shutdown the Instance Manager and all instances.
        """
        logger.info("🛑 Shutting down MCP Instance Manager...")
        
        self.running = False
        self.shutdown_event.set()
        
        # Stop EventObserverManager in MainProcess
        if self._event_observer_manager:
            try:
                logger.info("🛑 Stopping EventObserverManager in MainProcess...")
                await self._event_observer_manager.stop_consuming()
                if self._observer_consumer_task and not self._observer_consumer_task.done():
                    self._observer_consumer_task.cancel()
                    try:
                        await self._observer_consumer_task
                    except asyncio.CancelledError:
                        pass
                logger.info("✅ EventObserverManager stopped")
            except Exception as e:
                logger.warning(f"⚠️ Failed to stop EventObserverManager: {e}")
        
        # Unregister event-driven observer
        if self._proxy_observer:
            try:
                from motet.core.workers import unregister_event_observer
                unregister_event_observer(self._proxy_observer)
                logger.info("✅ Event-driven observer unregistered")
            except Exception as e:
                logger.warning(f"⚠️ Failed to unregister observer: {e}")
        
        # Cancel background tasks
        for task in [
            self.health_monitor_task,
            self.cleanup_task,
            self.context_monitor_task,
            self._control_command_task,
        ]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        # Stop HTTP endpoints
        await self._stop_http_endpoints()
        
        # Destroy all instances
        instance_ids = list(self.instances.keys())
        for instance_id in instance_ids:
            await self.destroy_instance(instance_id, reason="shutdown")
        
        logger.info("✅ MCP Instance Manager shutdown complete")

    async def _load_configuration(self) -> InstanceManagerConfig:
        """Load configuration from file or dictionary."""
        if self.config_dict:
            config_data = dict(self.config_dict)
        else:
            if not self.config_path:
                raise ValueError("No configuration provided (config_path or config_dict required)")
            config_file = Path(self.config_path)
            if not config_file.exists():
                raise FileNotFoundError(f"Configuration file not found: {self.config_path}")
            with open(config_file, "r") as f:
                if config_file.suffix in [".yaml", ".yml"]:
                    config_data = yaml.safe_load(f) or {}
                elif config_file.suffix == ".json":
                    import json
                    config_data = json.load(f) or {}
                else:
                    raise ValueError(f"Unsupported configuration format: {config_file.suffix}")

        config_data = dict(config_data)
        config_data.pop("enable_on_demand_creator", None)
        raw_services = config_data.get("services") or []
        normalized = []
        for svc in raw_services:
            if isinstance(svc, MCPInstanceConfig):
                normalized.append(svc)
            elif isinstance(svc, dict) and svc.get("service_id"):
                normalized.append(
                    MCPInstanceConfig(
                        **normalize_server_config_dict(str(svc["service_id"]), svc)
                    )
                )
        config_data["services"] = normalized
        return InstanceManagerConfig(**config_data)



# Global instance
_instance_manager: Optional[MCPInstanceManager] = None


def get_instance_manager() -> Optional[MCPInstanceManager]:
    """Get the global instance manager (if running)."""
    return _instance_manager


def set_instance_manager(manager: MCPInstanceManager) -> None:
    """Set the global instance manager."""
    global _instance_manager
    _instance_manager = manager


def get_service_config(service_id: str) -> Optional[MCPInstanceConfig]:
    """
    Get the full service configuration from the global instance manager.
    
    Args:
        service_id: Service identifier
        
    Returns:
        MCPInstanceConfig or None if service not found.
    """
    manager = get_instance_manager()
    if not manager:
        return None
    
    return manager.service_configs.get(service_id)


def main() -> None:
    """CLI entry for the sibling MCP instance manager process."""
    parser = argparse.ArgumentParser(description="MCP Instance Manager - Standalone Process")
    parser.add_argument(
        "--worker-id",
        type=str,
        default=os.getenv("MOTET_WORKER_ID", "default"),
        help=(
            "Worker ID for logging / telemetry. Under ADR-0105 this is observability "
            "metadata (which worker bootstrapped this manager); it is NOT the bus "
            "routing key. Use --manager-id for routing."
        ),
    )
    parser.add_argument(
        "--manager-id",
        type=str,
        default=os.getenv("MOTET_MCP_MANAGER_ID"),
        help=(
            "Manager ID — the prefix on Redis Streams MCP request/response streams, the "
            "PUB-SUB lifecycle channel, and the readiness set (ADR-0105 §R2). Required "
            "when running as a sibling deployment serving real workers; falls back to "
            "--worker-id for tests / single-process invocations."
        ),
    )
    parser.add_argument("--metrics-port", type=int, default=9090, help="Port for metrics endpoint")
    parser.add_argument("--health-port", type=int, default=9091, help="Port for health endpoint")
    parser.add_argument("--config-path", type=str, default=None, help="Path to configuration file")
    args = parser.parse_args()

    resolved_manager_id = args.manager_id or args.worker_id
    logger.info(
        "mcp_instance_manager_standalone_starting",
        worker_id=args.worker_id,
        manager_id=resolved_manager_id,
        manager_id_explicit=bool(args.manager_id),
    )

    config_path = args.config_path or os.getenv(
        "MCP_INSTANCE_MANAGER_CONFIG", "/app/config/mcp_instance_manager.yaml"
    )

    try:
        from motet.core.commands.base import CommandContext

        startup_command_context = CommandContext(
            task_id="worker_startup",
            conversation_id="",
            principal_id="",
            tenant_id=os.getenv("MOTET_TENANT_ID", "default"),
            motet_id=os.getenv("MOTET_MOTET_ID", "default"),
        )
        logger.info("Created startup CommandContext for vault credential prefetch")
    except Exception as e:
        logger.warning(
            "Failed to create startup CommandContext",
            error=str(e),
            traceback=traceback.format_exc(),
        )
        startup_command_context = None

    manager = MCPInstanceManager(
        config_path=config_path,
        worker_id=args.worker_id,
        manager_id=resolved_manager_id,
        metrics_port=args.metrics_port,
        health_port=args.health_port,
        startup_command_context=startup_command_context,
    )
    set_instance_manager(manager)

    async def run_manager() -> None:
        """Run the instance manager until SIGINT/SIGTERM or fatal error."""
        import sys

        loop = asyncio.get_running_loop()
        stop = asyncio.Event()

        def _request_stop() -> None:
            stop.set()

        if sys.platform != "win32":
            try:
                loop.add_signal_handler(signal.SIGINT, _request_stop)
                loop.add_signal_handler(signal.SIGTERM, _request_stop)
            except (NotImplementedError, RuntimeError) as e:
                logger.warning("mcp_instance_manager_signal_handler_unavailable", error=str(e))

        try:
            await manager.start()

            try:
                from motet.core.security.oauth_token_refresher import start_token_refresher
                logger.info("Starting OAuth token refresher")
                asyncio.create_task(start_token_refresher())
            except Exception as e:
                logger.warning("Failed to start OAuth token refresher", error=str(e))

            await stop.wait()
        except asyncio.CancelledError:
            logger.info("MCPInstanceManager cancelled")
            raise
        finally:
            await manager.shutdown()

    try:
        asyncio.run(run_manager())
    except KeyboardInterrupt:
        logger.info("MCPInstanceManager interrupted by user")
    except Exception:
        logger.error("MCPInstanceManager failed", exc_info=True)
        raise


if __name__ == "__main__":
    main()


