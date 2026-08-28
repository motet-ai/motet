"""
Motet - MCP Instance Supervisor

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Health monitor, idle cleanup, lag metric, restart budget, Redis process
    status, and retry of configured services that failed bootstrap with no
    live instance (HTTP port leftovers after manager restart).

Dependencies:
    - asyncio: per-instance locks and background loops
    - structlog: structured logging

Usage:
    Mixed into MCPInstanceManager in instance_manager.py. Do not instantiate alone.

Notes:
    - Public import path remains motet.core.tools.mcp_motet.proxy.mcp_instance_manager
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, Optional

import structlog

from motet.core.tools.mcp_motet.protocol import LifecycleDuration, parse_instance_key

logger = structlog.get_logger(__name__)

class SupervisorMixin:
    async def get_instance_health(self, instance_id: str) -> Dict[str, Any]:
        """
        Get health status of a specific instance.
        
        Args:
            instance_id: Instance identifier
            
        Returns:
            Health status dictionary
        """
        if instance_id not in self.instances:
            return {"status": "not_found"}
        
        instance = self.instances[instance_id]
        
        # Check if process is still running
        if instance.process and instance.process.returncode is not None:
            return {
                "status": "dead",
                "exit_code": instance.process.returncode
            }
        
        return {
            "status": "healthy" if instance.is_healthy else "unhealthy",
            "instance_id": instance_id,
            "service_id": instance.service_id,
            "context_id": instance.context_id,
            "created_at": instance.created_at,
            "last_used": instance.last_used,
            "uptime_seconds": time.time() - instance.created_at
        }

    def get_metrics(self) -> Dict[str, Any]:
        """Get manager and instance metrics."""
        start = self.stats.get("start_time") or time.time()
        return {
            "manager": {
                "running": self.running,
                "uptime": time.time() - start,
                "stats": self.stats,
                "streams_without_instance": self._streams_without_instance,
            },
            "services": {
                service_id: {
                    "instance_count": len([i for i in self.instances.values() if i.service_id == service_id]),
                    "status": self._service_status_label(service_id),
                }
                for service_id in self.service_configs.keys()
            },
            "instances": {
                instance_id: {
                    "service_id": inst.service_id,
                    "context_id": inst.context_id,
                    "uptime": time.time() - inst.created_at,
                    "healthy": inst.is_healthy
                }
                for instance_id, inst in self.instances.items()
            }
        }

    async def _startup_status_heartbeat(self) -> None:
        """Publish ``starting`` status on an interval until cancelled (see ``start()``)."""
        try:
            while True:
                await asyncio.sleep(5)
                await self._publish_status_to_redis(lifecycle_status="starting")
        except asyncio.CancelledError:
            raise

    async def _publish_status_to_redis(self, lifecycle_status: Optional[str] = None) -> None:
        """Publish manager status to Redis using ManagerStatusRegistry."""
        try:
            # Initialize registry if not done yet (first call from async context)
            if self.status_registry is None:
                from motet.core.distributed.manager_status import ManagerStatusRegistry
                self.status_registry = ManagerStatusRegistry()

            if lifecycle_status is not None:
                status = lifecycle_status
            else:
                status = "running" if self.running else "stopped"

            # Get manager process metrics
            import psutil
            manager_proc = psutil.Process(os.getpid())

            # Count healthy vs unhealthy instances
            healthy_instances = sum(1 for inst in self.instances.values()
                                   if inst.process and inst.process.returncode is None)
            unhealthy_instances = len(self.instances) - healthy_instances

            # Publish status using the registry. ADR-0105 §R3: surface
            # manager_id (canonical identity) and the worker(s) this manager
            # serves so the ops dashboard can show a top-level "Managers" view
            # decoupled from worker_id.
            self.status_registry.publish_status(
                worker_id=self.worker_id or "default",
                manager_type=self.manager_type,
                status=status,
                pid=os.getpid(),
                manager_id=self.manager_id or self.worker_id or "default",
                served_workers=[self.worker_id] if self.worker_id else [],
                instances_total=len(self.instances),
                instances_healthy=healthy_instances,
                instances_unhealthy=unhealthy_instances,
                total_requests=self.stats.get("instances_created", 0),
                active_requests=0,  # MCP doesn't track active requests the same way
                errors=self.stats.get("health_failures", 0),
                start_time=self.stats.get("start_time"),
                memory_mb=manager_proc.memory_info().rss / 1024 / 1024,
                cpu_percent=manager_proc.cpu_percent(),
                metadata={
                    "instances_created": self.stats.get("instances_created", 0),
                    "instances_destroyed": self.stats.get("instances_destroyed", 0),
                    "restarts": self.stats.get("restarts", 0),
                    "health_checks": self.stats.get("health_checks", 0),
                    "service_count": len(self.service_configs),
                    "instances_labeled": len(self.instances),
                }
            )
            
        except Exception as e:
            logger.error("Error publishing MCP manager status to Redis", error=str(e), exc_info=True)

    async def _health_monitor_loop(self) -> None:
        """Background task that monitors instance health."""
        logger.info("🏥 Health monitor started")
        
        while self.running:
            try:
                for instance_id, instance in list(self.instances.items()):
                    # Check if process is still alive
                    if instance.process and instance.process.returncode is not None:
                        logger.warning(f"⚠️ Instance {instance_id} died (exit code: {instance.process.returncode})")
                        
                        if instance.service_id in self._disabled_services:
                            await self.destroy_instance(instance_id, reason="health_check_disabled")
                            continue
                        service_config = self.service_configs.get(instance.service_id)
                        if not service_config:
                            await self.destroy_instance(instance_id, reason="health_check_unknown_service")
                            continue
                        
                        if service_config.restart_on_failure:
                            if self._restart_budget.is_exhausted(instance.service_id):
                                logger.error(
                                    "mcp_restart_budget_exhausted",
                                    service_id=instance.service_id,
                                    instance_id=instance_id,
                                )
                                self._service_last_error[instance.service_id] = (
                                    "restart budget exhausted"
                                )
                                await self.destroy_instance(
                                    instance_id, reason="health_check_budget_exhausted"
                                )
                                await self._publish_per_service_signal(
                                    instance.service_id, "service_removed"
                                )
                                await self._publish_one_service_status(
                                    instance.service_id, status_override="failed"
                                )
                                continue
                            if not self._restart_budget.record(instance.service_id):
                                continue
                            logger.info(
                                "mcp_instance_restarting",
                                instance_id=instance_id,
                                service_id=instance.service_id,
                            )
                            await self.destroy_instance(instance_id, reason="health_check_restart")
                            parsed = {}
                            try:
                                parsed = parse_instance_key(
                                    service_id=instance.service_id,
                                    visibility=self.service_configs[instance.service_id].visibility,
                                    instance_key=instance.context_id
                                )
                            except Exception as e:
                                logger.warning(
                                    "mcp_restart_parse_instance_key_failed",
                                    instance_id=instance_id,
                                    error=str(e),
                                )
                            try:
                                await asyncio.wait_for(
                                    self.create_instance(
                                        instance.service_id,
                                        tenant_id=parsed.get("tenant_id"),
                                        principal_id=parsed.get("principal_id"),
                                        conversation_id=parsed.get("conversation_id"),
                                        task_id=parsed.get("task_id"),
                                        motet_id=parsed.get("motet_id"),
                                        reason="health_check_restart",
                                        origin="_health_monitor_loop",
                                    ),
                                    timeout=self._create_timeout_seconds(),
                                )
                            except Exception as e:
                                self._service_last_error[instance.service_id] = str(e)[:500]
                                logger.error(
                                    "mcp_instance_restart_failed",
                                    service_id=instance.service_id,
                                    error=str(e),
                                    exc_info=True,
                                )
                                await self._publish_one_service_status(
                                    instance.service_id, status_override="failed"
                                )
                                continue
                            self.stats["restarts"] += 1
                            self._service_last_restarted_at[instance.service_id] = time.time()
                            await self._publish_per_service_signal(
                                instance.service_id, "service_restarted"
                            )
                            await self._publish_one_service_status(instance.service_id)
                        else:
                            await self.destroy_instance(instance_id, reason="health_check_destroy")
                    
                    self.stats["health_checks"] += 1

                await self._retry_failed_configured_services()
                
                await self._publish_status_to_redis()
                await self._publish_all_service_statuses()
                
                # Wait before next health check
                await asyncio.sleep(10)
                
            except Exception as e:
                logger.error(f"❌ Health monitor error: {e}", exc_info=True)
                self.stats["health_failures"] += 1
                await asyncio.sleep(10)

    async def _cleanup_loop(self) -> None:
        """
        Background task that cleans up unused instances.
        
        """
        logger.info("🧹 Cleanup task started")
        
        while self.running:
            try:
                current_time = time.time()
                
                for instance_id, instance in list(self.instances.items()):
                    service_config = self.service_configs[instance.service_id]
                    
                    # Idle timeout applies to conversation/task/session instances, not
                    # discovery-only or permanent shared services (instances=0 means
                    # discovery-only; omitted/1 is a single identity-keyed instance).
                    if service_config.lifecycle_duration == LifecycleDuration.PERMANENT:
                        continue
                    
                    timeout = service_config.instance_timeout
                    
                    # Check if instance has been idle too long
                    idle_time = current_time - instance.last_used
                    if idle_time > timeout:
                        logger.info(f"🗑️ Cleaning up idle instance {instance_id} (idle: {idle_time:.0f}s, timeout: {timeout}s)")
                        await self.destroy_instance(instance_id, reason="idle_timeout")
                
                # Wait before next cleanup
                await asyncio.sleep(60)
                
            except Exception as e:
                logger.error(f"❌ Cleanup error: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def _context_monitor_loop(self) -> None:
        """Lag metric only: count request streams with no matching instance.

        Does not create instances. Observer + MotetMCPClient wait are the
        create path (design note 2026-08-13 step 2).
        """
        logger.info(
            "mcp_context_monitor_metric_only",
            manager_id=self.manager_id,
        )
        prefix = self.manager_id or self.worker_id or "default"
        while self.running:
            try:
                from motet.core.distributed.redis_manager import get_redis_client
                from motet.core.tools.mcp_motet.protocol import (
                    mcp_io_stream_scan_patterns,
                    parse_stream_name,
                )

                redis_client = await get_redis_client(f"context_monitor_{prefix}")
                seen: set[str] = set()
                missing = 0
                for scan_pattern in mcp_io_stream_scan_patterns(
                    prefix, stream_type="requests"
                ):
                    cursor = 0
                    while True:
                        cursor, keys = await redis_client.scan(
                            cursor, match=scan_pattern, count=100
                        )
                        for stream_key in keys:
                            if isinstance(stream_key, bytes):
                                stream_key = stream_key.decode("utf-8")
                            if stream_key in seen:
                                continue
                            seen.add(stream_key)
                            try:
                                parsed = parse_stream_name(stream_key)
                            except ValueError:
                                continue
                            if not parsed or parsed.get("stream_type") != "requests":
                                continue
                            if parsed.get("visibility") == "global":
                                continue
                            instance_key = parsed.get("instance_key")
                            if instance_key and instance_key not in self.instances:
                                length = await redis_client.xlen(stream_key)
                                if length:
                                    missing += 1
                        if cursor == 0:
                            break
                self._streams_without_instance = missing
                if missing:
                    logger.info(
                        "mcp_streams_without_instance",
                        count=missing,
                        note="Observer should create; this loop does not spawn",
                    )
                await asyncio.sleep(20)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("mcp_context_monitor_error", error=str(e), exc_info=True)
                await asyncio.sleep(5)

    async def _retry_failed_configured_services(self) -> None:
        """
        Recreate configured services that failed bootstrap and have no instance.

        Health restart only walks live ``self.instances``. A leftover HTTP
        sidecar that blocked bind leaves Redis ``failed`` with zero instances,
        so MotetMCPClient waits 30s forever unless we retry here (budget applies).
        """
        live_services = {inst.service_id for inst in self.instances.values()}
        for service_id, service_config in list(self.service_configs.items()):
            if service_id in live_services:
                continue
            if service_id in self._disabled_services:
                continue
            if not service_config.restart_on_failure:
                continue
            if not self._service_last_error.get(service_id):
                continue
            if self._restart_budget.is_exhausted(service_id):
                continue
            if not self._restart_budget.record(service_id):
                continue
            logger.info(
                "mcp_failed_service_retry",
                service_id=service_id,
                last_error=self._service_last_error.get(service_id),
            )
            try:
                await asyncio.wait_for(
                    self._create_initial_instances(service_id, service_config),
                    timeout=self._create_timeout_seconds(),
                )
            except Exception as e:
                self._service_last_error[service_id] = str(e)[:500]
                logger.error(
                    "mcp_failed_service_retry_failed",
                    service_id=service_id,
                    error=str(e),
                    exc_info=True,
                )
                await self._publish_one_service_status(
                    service_id, status_override="failed"
                )
                continue
            # Discovery failure is swallowed inside _create_initial_instances.
            if self._service_last_error.get(service_id):
                await self._publish_one_service_status(
                    service_id, status_override="failed"
                )
                continue
            self.stats["restarts"] += 1
            self._service_last_restarted_at[service_id] = time.time()
            await self._publish_per_service_signal(service_id, "service_restarted")
            await self._publish_one_service_status(service_id)

    def _service_status_label(self, service_id: str) -> str:
        if service_id in self._disabled_services:
            return "disabled"
        live = [
            i for i in self.instances.values()
            if i.service_id == service_id and i.is_healthy
            and (i.process is None or i.process.returncode is None)
        ]
        if live:
            return "running"
        if self._restart_budget.is_exhausted(service_id):
            return "failed"
        if self._service_last_error.get(service_id):
            return "failed"
        if service_id in self.service_configs:
            return "not_started"
        return "not_started"
