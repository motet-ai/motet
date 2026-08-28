"""
Motet - Event Bus

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-19

Description:
    Event bus system for the Motet distributed framework.
    Provides distributed event publishing, subscription, and observer management.
    Tenant-attributed events publish to ``{tenant_id}:events:channel`` (issue
    #233). Events with no usable tenant use the platform channel
    ``motet:events:channel``.

Dependencies:
    - threading: Thread synchronization
    - json: JSON serialization
    - typing: Type hints and annotations
    - Redis: Distributed event storage

Usage:
    from motet.core.workers.events import EventBus, global_bus
    
    # Publish event
    await global_bus.publish(event)
    
    # Register observer
    global_bus.register_observer(observer)

Notes:
    - Tenant events publish to ``{tenant_id}:events:channel`` (issue #233)
    - Events with no usable tenant publish to ``motet:events:channel``
    - ``get_stats()`` reports the platform channel name only
    - Integrates with Redis pub/sub for distributed events
"""

from __future__ import annotations

import threading
import json
from typing import Any, Dict, List, Optional
import os

import structlog

# Import observer classes directly
from .observers import Event, Observer, EventFilter
from ..distributed.redis_manager import get_sync_redis_client
from ..distributed.tenant_keys import event_bus_channel

logger = structlog.get_logger(__name__)
DEBUG_MODE = os.getenv("MOTET_DEBUG_MODE", "false").lower() == "true"


class EventBus:
    """Modern event bus with sophisticated observer support and event filtering"""
    
    def __init__(self, *, durable_backend: Optional[str] = None, redis_url: Optional[str] = None) -> None:
        # Event tracking
        self._published: int = 0
        self._failures: int = 0

        # Event history and tracking
        self._event_history: List[Event] = []
        self._max_event_history = 1000
        
        # Redis pub/sub for event publishing
        self._durable_backend = durable_backend
        self._redis = None
        self._redis_channel = event_bus_channel(None)
        if durable_backend == "redis":
            try:
                # Always use the shared Redis manager so TLS / pool settings stay consistent
                self._redis = get_sync_redis_client("event_bus")
            except Exception as exc:
                try:
                    import structlog
                    structlog.get_logger().warning("event_bus_durable_init_failed", error=str(exc))
                except Exception:
                    pass  # logging fallback; must not re-raise
                self._redis = None

    def publish(self, event: Dict[str, Any]) -> None:
        """Publish event to Redis pub/sub channel for observer consumption"""
        # Convert to Event object for processing
        event_obj = Event.from_dict(event) if isinstance(event, dict) else event
        
        # Add to event history
        self._event_history.append(event_obj)
        if len(self._event_history) > self._max_event_history:
            self._event_history = self._event_history[-self._max_event_history:]
        
        # Publish to Redis pub/sub channel for observer consumption
        if self._redis is not None:
            try:
                event_dict = event_obj.to_dict()
                data = event_dict.get("data") if isinstance(event_dict.get("data"), dict) else {}
                tenant_id = str(data.get("tenant_id") or "").strip() or None
                channel = event_bus_channel(tenant_id)
                self._redis.publish(channel, json.dumps(event_dict))  # type: ignore
                if DEBUG_MODE:
                    logger.debug(
                        "event_bus_published",
                        event_type=event_obj.event_type,
                        channel=channel,
                        tenant_id=tenant_id,
                    )
                
                # Store event in Redis for task visualization (debug mode only)
                self._store_event_for_task(event_dict)
                
            except Exception as exc:
                self._failures += 1
                logger.warning(
                    "event_bus_publish_failed",
                    event_type=event_obj.event_type,
                    channel=self._redis_channel,
                    error=str(exc),
                    exc_info=True,
                )
                try:
                    import structlog
                    structlog.get_logger().warning("event_enqueue_failed", error=str(exc))
                except Exception:
                    pass  # logging fallback; must not re-raise
        else:
            logger.warning(
                "event_bus_redis_unavailable",
                event_type=event_obj.event_type,
            )
        
        self._published += 1
    
    def _store_event_for_task(self, event_dict: Dict[str, Any]) -> None:
        """Store event in Redis indexed by task_id for debugging (debug mode only)"""
        try:
            # Only store events if debug mode is enabled
            debug_mode = os.getenv("MOTET_DEBUG_MODE", "false").lower() == "true"
            if not debug_mode or not self._redis:
                return
            
            # Extract task_id from event data
            task_id = None
            if "data" in event_dict and isinstance(event_dict["data"], dict):
                task_id = event_dict["data"].get("task_id")
            
            # Skip events without task_id
            if not task_id:
                return
            
            # Store event in a Redis list indexed by task_id
            # Key format: task:events:{task_id}
            event_key = f"task:events:{task_id}"
            
            # Add timestamp if not present
            if "timestamp" not in event_dict:
                from datetime import datetime
                event_dict["timestamp"] = datetime.utcnow().isoformat()
            
            # Store event as JSON in Redis list
            self._redis.rpush(event_key, json.dumps(event_dict))
            
            # Set TTL based on debug mode (extended TTL for debugging)
            ttl_hours = int(os.getenv("MOTET_DEBUG_TTL_HOURS", "6"))
            self._redis.expire(event_key, ttl_hours * 3600)
            
        except Exception as exc:
            # Silently fail - event storage is non-critical
            pass

    def publish_event(self, event: Event) -> None:
        """Publish Event object directly"""
        self.publish(event.to_dict())

    def consume_durable(self, *, max_items: int = 100) -> List[Dict[str, Any]]:
        """Best-effort pop from durable queue. Returns parsed events.
        Requires durable_backend=redis.
        """
        out: List[Dict[str, Any]] = []
        if self._redis is None:
            return out
        try:
            for _ in range(max_items):
                # Note: consume_durable is deprecated with pub/sub - this will not work
                val = None  # Pub/sub doesn't support durable consumption
                if not val:
                    break
                try:
                    out.append(json.loads(val))
                except Exception as exc:
                    continue
        except Exception as exc:
            return out
        return out

    def get_recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent events"""
        recent = self._event_history[-limit:] if limit > 0 else self._event_history
        return [event.to_dict() for event in recent]

    def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive statistics"""
        return {
            "events_published": self._published,
            "event_history_size": len(self._event_history),
            "max_event_history": self._max_event_history,
            "durable_backend": self._durable_backend,
            "redis_available": self._redis is not None,
            "redis_channel": self._redis_channel,
        }

    @property
    def published_count(self) -> int:
        """Get published count"""
        return self._published

    @property
    def failure_count(self) -> int:
        """Count of Redis publish failures (best-effort; publish still increments _published)."""
        return self._failures

    # Note: Observer management is now handled by EventObserverManager
    # Use register_event_observer() and unregister_event_observer() instead


_redis_url_env = os.getenv("MOTET_REDIS_URL") or os.getenv("MOTET_TRACE_REDIS_URL")
global_bus = EventBus(
    durable_backend=("redis" if _redis_url_env else None),
    redis_url=_redis_url_env,
)


__all__ = ["EventBus", "global_bus", "safe_publish"]

# Convenience helper: safe, best-effort publish that never raises
def safe_publish(event: Dict[str, Any]) -> None:
    """Publish using the global bus; swallow/log errors to avoid breaking call sites."""
    try:
        if event.get("kind") == "reasoning_step":
            if DEBUG_MODE:
                logger.debug("safe_publish_start", event_kind="reasoning_step")
        # Direct sync call since global_bus.publish is now synchronous
        global_bus.publish(event)
        if event.get("kind") == "reasoning_step":
            if DEBUG_MODE:
                logger.debug("safe_publish_success", event_kind="reasoning_step")
    except Exception as exc:
        if event.get("kind") == "reasoning_step":
            logger.warning(
                "safe_publish_failed",
                event_kind="reasoning_step",
                error=str(exc),
                exc_info=True,
            )

