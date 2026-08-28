"""
Motet - MCP Instance Control Plane

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    HTTP /metrics /health /ready plus Redis register/unregister/restart/disable/enable.
    ``/health`` includes the Motet product version of this manager process.

Dependencies:
    - asyncio: per-instance locks and background loops
    - structlog: structured logging
    - motet._version: Product version stamped on /health

Usage:
    Mixed into MCPInstanceManager in instance_manager.py. Do not instantiate alone.

Notes:
    - Public import path remains motet.core.tools.mcp_motet.proxy.mcp_instance_manager
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict

import structlog
from aiohttp import web

from motet._version import get_version
from motet.core.tools.mcp_motet.manager.config import (
    InstanceManagerConfig,
    MCPInstanceConfig,
    normalize_server_config_dict,
)
from motet.core.tools.mcp_motet.manager.control_commands import (
    ack_mcp_control_command,
    read_mcp_control_commands,
)
from motet.core.tools.mcp_motet.manager.service_status import delete_mcp_service_status

logger = structlog.get_logger(__name__)

class ControlPlaneMixin:
    async def reload_config(self) -> None:
        """Reload YAML by register/unregister of changed service_ids only."""
        logger.info("mcp_reload_config_begin")
        config = await self._load_configuration()
        desired = {s.service_id: s for s in config.services}
        current = set(self.service_configs.keys())
        for sid in current - set(desired):
            await self.unregister_server_config(sid)
        for sid, cfg in desired.items():
            if sid not in current:
                await self.register_server_config(sid, cfg.model_dump())
        logger.info("mcp_reload_config_done", services=list(desired.keys()))

    async def _start_http_endpoints(self, config: InstanceManagerConfig) -> None:
        """Start HTTP endpoints for metrics and health."""
        try:
            # Use instance variables for ports (from constructor), not config file
            # This allows command-line arguments to override config file values
            metrics_port = self.metrics_port
            health_port = self.health_port
            
            # Create metrics app
            self.metrics_app = web.Application()
            self.metrics_app.router.add_get('/metrics', self._handle_metrics)
            
            # Create health app
            self.health_app = web.Application()
            self.health_app.router.add_get('/health', self._handle_health)
            self.health_app.router.add_get('/ready', self._handle_ready)
            
            # Start metrics server
            self.metrics_runner = web.AppRunner(self.metrics_app)
            await self.metrics_runner.setup()
            metrics_site = web.TCPSite(self.metrics_runner, '0.0.0.0', metrics_port)
            await metrics_site.start()
            logger.info(f"📊 Metrics endpoint started on http://0.0.0.0:{metrics_port}/metrics")
            
            # Start health server
            self.health_runner = web.AppRunner(self.health_app)
            await self.health_runner.setup()
            health_site = web.TCPSite(self.health_runner, '0.0.0.0', health_port)
            await health_site.start()
            logger.info(f"🏥 Health endpoint started on http://0.0.0.0:{health_port}/health")
            
        except Exception as e:
            logger.error(f"❌ Failed to start HTTP endpoints: {e}", exc_info=True)
            raise

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        """Handle /metrics endpoint - returns Prometheus-compatible metrics."""
        try:
            metrics = self.get_metrics()
            
            # Format as Prometheus metrics
            lines = []
            
            # Manager metrics
            lines.append("# HELP mcp_manager_running Manager running status (1=running, 0=stopped)")
            lines.append("# TYPE mcp_manager_running gauge")
            lines.append(f"mcp_manager_running {1 if self.running else 0}")
            
            lines.append("# HELP mcp_instances_created_total Total instances created")
            lines.append("# TYPE mcp_instances_created_total counter")
            lines.append(f"mcp_instances_created_total {self.stats['instances_created']}")
            
            lines.append("# HELP mcp_instances_destroyed_total Total instances destroyed")
            lines.append("# TYPE mcp_instances_destroyed_total counter")
            lines.append(f"mcp_instances_destroyed_total {self.stats['instances_destroyed']}")
            
            lines.append("# HELP mcp_instance_restarts_total Total instance restarts")
            lines.append("# TYPE mcp_instance_restarts_total counter")
            lines.append(f"mcp_instance_restarts_total {self.stats['restarts']}")
            
            # Service metrics
            for service_id, service_metrics in metrics['services'].items():
                lines.append(f"# HELP mcp_service_instances Instance count per service")
                lines.append(f"# TYPE mcp_service_instances gauge")
                lines.append(f'mcp_service_instances{{service="{service_id}"}} {service_metrics["instance_count"]}')
            
            # Instance metrics
            for instance_id, inst_metrics in metrics['instances'].items():
                lines.append(f"# HELP mcp_instance_healthy Instance health status (1=healthy, 0=unhealthy)")
                lines.append(f"# TYPE mcp_instance_healthy gauge")
                lines.append(f'mcp_instance_healthy{{instance="{instance_id}",service="{inst_metrics["service_id"]}"}} {1 if inst_metrics["healthy"] else 0}')
            
            return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")
            
        except Exception as e:
            logger.error(f"Error handling metrics request: {e}", exc_info=True)
            return web.Response(text=f"Error: {str(e)}", status=500)

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Handle /health endpoint - returns overall health status."""
        try:
            health_data = {
                "status": "healthy" if self.running else "unhealthy",
                "motet_version": get_version(),
                "timestamp": time.time(),
                "services": len(self.service_configs),
                "instances": {
                    "total": len(self.instances),
                    "healthy": sum(1 for i in self.instances.values() if i.is_healthy),
                    "unhealthy": sum(1 for i in self.instances.values() if not i.is_healthy)
                },
                "stats": self.stats
            }
            
            status_code = 200 if self.running else 503
            return web.Response(
                text=json.dumps(health_data, indent=2),
                content_type="application/json",
                status=status_code
            )
            
        except Exception as e:
            logger.error(f"Error handling health request: {e}", exc_info=True)
            return web.Response(
                text=json.dumps({"status": "error", "error": str(e)}),
                content_type="application/json",
                status=500
            )

    async def _handle_ready(self, request: web.Request) -> web.Response:
        """Handle /ready endpoint - returns readiness status."""
        try:
            # Ready if running and at least one instance is healthy
            ready = self.running and any(i.is_healthy for i in self.instances.values())
            
            ready_data = {
                "ready": ready,
                "timestamp": time.time(),
                "instances_healthy": sum(1 for i in self.instances.values() if i.is_healthy)
            }
            
            status_code = 200 if ready else 503
            return web.Response(
                text=json.dumps(ready_data, indent=2),
                content_type="application/json",
                status=status_code
            )
            
        except Exception as e:
            logger.error(f"Error handling ready request: {e}", exc_info=True)
            return web.Response(
                text=json.dumps({"ready": False, "error": str(e)}),
                content_type="application/json",
                status=503
            )

    async def _stop_http_endpoints(self) -> None:
        """Stop HTTP endpoints."""
        try:
            if self.metrics_runner:
                await self.metrics_runner.cleanup()
            if self.health_runner:
                await self.health_runner.cleanup()
            logger.info("🛑 HTTP endpoints stopped")
        except Exception as e:
            logger.error(f"Error stopping HTTP endpoints: {e}")

    async def register_server_config(self, service_id: str, server_conf: Dict[str, Any]) -> None:
        """Add or replace one service and start discovery (control plane)."""
        data = normalize_server_config_dict(service_id, server_conf)
        cfg = MCPInstanceConfig(**data)
        self._disabled_services.discard(service_id)
        if service_id in self.service_configs:
            await self.unregister_server_config(service_id)
        self.service_configs[service_id] = cfg
        await self._create_initial_instances(service_id, cfg)
        logger.info("mcp_server_config_registered", service_id=service_id)

    async def unregister_server_config(self, service_id: str) -> None:
        """Stop all instances for a service and drop its config."""
        ids = [iid for iid, inst in self.instances.items() if inst.service_id == service_id]
        for iid in ids:
            await self.destroy_instance(iid, reason="unregister")
        self.service_configs.pop(service_id, None)
        self._disabled_services.discard(service_id)
        self._service_last_removed_at[service_id] = time.time()
        try:
            await self._publish_per_service_signal(service_id, "service_removed")
        except Exception as e:
            logger.warning(
                "mcp_unregister_remove_signal_failed",
                service_id=service_id,
                error=str(e),
            )
        manager_id = self.manager_id or self.worker_id or "default"
        await asyncio.get_running_loop().run_in_executor(
            None, delete_mcp_service_status, manager_id, service_id
        )
        logger.info("mcp_server_config_unregistered", service_id=service_id)

    async def disable_server(self, service_id: str) -> None:
        """Keep config, stop instances, mark disabled."""
        self._disabled_services.add(service_id)
        ids = [iid for iid, inst in self.instances.items() if inst.service_id == service_id]
        for iid in ids:
            await self.destroy_instance(iid, reason="disable")
        try:
            await self._publish_per_service_signal(service_id, "service_removed")
        except Exception as e:
            logger.warning(
                "mcp_disable_remove_signal_failed",
                service_id=service_id,
                error=str(e),
            )
        await self._publish_one_service_status(service_id, status_override="disabled")

    async def enable_server(self, service_id: str) -> None:
        """Re-enable a disabled service and bootstrap it."""
        self._disabled_services.discard(service_id)
        cfg = self.service_configs.get(service_id)
        if not cfg:
            raise ValueError(f"Service {service_id} not registered")
        await self._create_initial_instances(service_id, cfg)

    async def restart_server(self, service_id: str) -> None:
        """Destroy live instances and bootstrap again (under per-key locks)."""
        if service_id not in self.service_configs:
            raise ValueError(f"Service {service_id} not registered")
        if service_id in self._disabled_services:
            raise RuntimeError(f"MCP service {service_id} is disabled")
        ids = [iid for iid, inst in self.instances.items() if inst.service_id == service_id]
        for iid in ids:
            await self.destroy_instance(iid, reason="operator_restart")
        await self._create_initial_instances(service_id, self.service_configs[service_id])

    async def _control_command_loop(self) -> None:
        """Consume Redis control commands (register/unregister/restart/disable/enable)."""
        manager_id = self.manager_id or self.worker_id or "default"
        logger.info("mcp_control_loop_started", manager_id=manager_id)
        while self.running:
            try:
                entries = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: read_mcp_control_commands(manager_id, block_ms=2000)
                )
                for entry_id, payload in entries:
                    op = str(payload.get("op") or "")
                    sid = str(payload.get("service_id") or "")
                    try:
                        if op == "register":
                            await self.register_server_config(sid, payload.get("config") or {})
                        elif op == "unregister":
                            await self.unregister_server_config(sid)
                        elif op == "restart":
                            await self.restart_server(sid)
                        elif op == "disable":
                            await self.disable_server(sid)
                        elif op == "enable":
                            await self.enable_server(sid)
                        else:
                            logger.warning("mcp_control_unknown_op", op=op, entry_id=entry_id)
                    except Exception as e:
                        logger.error(
                            "mcp_control_command_failed",
                            op=op,
                            service_id=sid,
                            error=str(e),
                            exc_info=True,
                        )
                    await asyncio.get_running_loop().run_in_executor(
                        None, ack_mcp_control_command, manager_id, entry_id
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("mcp_control_loop_error", error=str(e), exc_info=True)
                await asyncio.sleep(2)
