"""
Motet - MCP Instance Signals

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Redis ready/removed/restarted signals and per-service status publish.

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
from typing import Optional

import structlog

from motet.core.distributed.tenant_keys import product_key
from motet.core.tools.mcp_motet.manager.service_status import (
    MCPServiceStatus,
    publish_mcp_service_status,
)

logger = structlog.get_logger(__name__)

class SignalsMixin:
    async def _publish_per_service_signal(self, service_id: str, signal_type: str) -> None:
        """
        Publish per-service lifecycle signal to Redis (ADR-0069).
        PUBLISH to channel so all processes (parent + children) receive; optional LPUSH for legacy.
        signal_type: "service_ready" | "service_restarted" | "service_removed"

        ADR-0105 §R2: keyed on ``manager_id`` (the bus routing prefix), not
        ``worker_id``. ``worker_id`` is logged for telemetry only.
        """
        manager_id = self.manager_id or "default"
        key = product_key(f"mcp:{signal_type}:{manager_id}:{service_id}")
        channel = product_key(f"mcp:signals:{manager_id}")
        message = f"{signal_type}:{service_id}"

        def _do_publish() -> None:
            try:
                from motet.core.distributed.redis_manager import get_sync_redis_client
                redis_client = get_sync_redis_client("mcp_instance_manager")
                redis_client.lpush(key, service_id)
                redis_client.expire(key, 300)
                redis_client.publish(channel, message)
                # Durable set so watcher can catch up if it subscribes after we publish (PUB/SUB is fire-and-forget)
                ready_set_key = product_key(f"mcp:ready_services:{manager_id}")
                if signal_type == "service_ready":
                    redis_client.sadd(ready_set_key, service_id)
                    redis_client.expire(ready_set_key, 3600)
                elif signal_type == "service_removed":
                    redis_client.srem(ready_set_key, service_id)
                logger.info(
                    "mcp_per_service_signal_published",
                    manager_id=manager_id,
                    worker_id=self.worker_id,
                    service_id=service_id,
                    signal_type=signal_type,
                    key=key,
                    channel=channel,
                )
            except Exception as e:
                logger.warning(
                    "mcp_per_service_signal_failed",
                    manager_id=manager_id,
                    worker_id=self.worker_id,
                    service_id=service_id,
                    signal_type=signal_type,
                    error=str(e),
                )

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _do_publish)
        except Exception as e:
            logger.warning(
                "mcp_per_service_signal_executor_failed",
                manager_id=manager_id,
                worker_id=self.worker_id,
                service_id=service_id,
                signal_type=signal_type,
                error=str(e),
            )

    async def _publish_ready_signal(self) -> None:
        """
        Publish ready signal to Redis so workers can wait on bus event (BLPOP) instead of polling HTTP.
        Key: motet:mcp:instance_manager:ready:{manager_id}. Workers block on BLPOP until this is pushed.

        ADR-0105 §R2: keyed on ``manager_id``.
        """
        manager_id = self.manager_id or "default"
        key = product_key(f"mcp:instance_manager:ready:{manager_id}")

        def _do_publish() -> None:
            try:
                from motet.core.distributed.redis_manager import get_sync_redis_client
                redis_client = get_sync_redis_client("mcp_instance_manager")
                redis_client.lpush(key, "1")
                redis_client.expire(key, 120)
                logger.info(
                    "mcp_instance_manager_ready_signal_published",
                    manager_id=manager_id,
                    worker_id=self.worker_id,
                    key=key,
                )
            except Exception as e:
                logger.warning(
                    "mcp_instance_manager_ready_signal_failed",
                    manager_id=manager_id,
                    worker_id=self.worker_id,
                    error=str(e),
                )

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _do_publish)
        except Exception as e:
            logger.warning(
                "mcp_instance_manager_ready_signal_executor_failed",
                manager_id=manager_id,
                worker_id=self.worker_id,
                error=str(e),
            )

    async def _publish_one_service_status(
        self,
        service_id: str,
        status_override: Optional[str] = None,
    ) -> None:
        cfg = self.service_configs.get(service_id)
        live = [i for i in self.instances.values() if i.service_id == service_id]
        pids: List[int] = []
        for inst in live:
            if inst.process is not None and getattr(inst.process, "pid", None) and inst.process.returncode is None:
                pids.append(int(inst.process.pid))
        auth_type = "none"
        if cfg and cfg.auth:
            auth_type = cfg.auth.type.value if hasattr(cfg.auth.type, "value") else str(cfg.auth.type)
        label = status_override or self._service_status_label(service_id)
        status = MCPServiceStatus(
            service_id=service_id,
            manager_id=self.manager_id or self.worker_id or "default",
            status=label,
            healthy=bool(pids) or any(i.is_healthy and i.transport for i in live),
            transport=cfg.transport if cfg else "stdio",
            visibility=cfg.visibility.value if cfg else None,
            lifecycle_duration=cfg.lifecycle_duration.value if cfg else None,
            state_model=cfg.state_model.value if cfg else None,
            auth_type=auth_type,
            instance_count=len(live),
            instance_ids=[i.instance_id for i in live],
            pids=pids,
            restart_count_window=self._restart_budget.count(service_id),
            restart_budget_remaining=self._restart_budget.remaining(service_id),
            last_error=self._service_last_error.get(service_id),
            last_ready_at=self._service_last_ready_at.get(service_id),
            last_removed_at=self._service_last_removed_at.get(service_id),
            last_restarted_at=self._service_last_restarted_at.get(service_id),
            tool_names=list(self._service_tool_names.get(service_id, [])),
            updated_at=time.time(),
            disabled=service_id in self._disabled_services,
        )
        await asyncio.get_running_loop().run_in_executor(
            None, publish_mcp_service_status, status
        )

    async def _publish_all_service_statuses(self) -> None:
        for service_id in list(self.service_configs.keys()):
            try:
                await self._publish_one_service_status(service_id)
            except Exception as e:
                logger.warning(
                    "mcp_publish_service_status_failed",
                    service_id=service_id,
                    error=str(e),
                )
