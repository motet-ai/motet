"""
Motet - Event Observer Manager

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-19

Description:
    Worker EventObserverManager for the Motet distributed framework.
    Consumes EventBus pub/sub via ``PSUBSCRIBE *:events:channel`` so workers
    see every tenant channel plus the platform ``motet:events:channel``.

Dependencies:
    - typing: Type hints and annotations
    - Base interfaces and implementations

Usage:
    from motet.core.workers.event_observer_manager import EventObserverManager

Notes:
    - ``PSUBSCRIBE *:events:channel`` so one connection sees every tenant
      channel plus the platform ``motet:events:channel`` (issue #233)
    - Handles both ``message`` and ``pmessage`` Redis pub/sub types
"""


import asyncio
import json
import time
from typing import Dict, Any, List, Optional, Set
from collections import deque

from .observers import Observer, Event, EventFilter, EventPriority
from ..distributed.redis_manager import get_pubsub_redis_client
from ..distributed.tenant_keys import EVENT_BUS_PSUBSCRIBE_PATTERN
from .concurrency_primitives import WorkerLock


class EventObserverManager:
    """
    Manages observers that consume events from Redis queue instead of direct notification.
    
    This provides:
    - Reliable event delivery with persistence
    - FIFO ordering guarantees
    - Better performance than Celery events
    - Simpler architecture
    
    ADR-0033 Phase 0: Uses WorkerLock for pool-agnostic thread safety.
    """
    
    def __init__(self, redis_url: Optional[str] = None):
        self.redis_client = get_pubsub_redis_client("observer_manager")
        self.observers: List[Observer] = []
        self.consumer_task: Optional[asyncio.Task] = None
        self.running = False
        self._lock = WorkerLock()  # ADR-0033: Pool-aware lock
        self.redis_pattern = EVENT_BUS_PSUBSCRIBE_PATTERN
        
        # Performance tracking
        self.events_consumed = 0
        self.events_delivered = 0
        self.delivery_failures = 0
        self.start_time = time.time()
    
    def register_observer(self, observer: Observer) -> None:
        """Register an observer to receive events from the queue."""
        with self._lock:
            if observer not in self.observers:
                self.observers.append(observer)
                print(f"🎯 EventObserverManager: Registered observer '{observer.name}'")
    
    def unregister_observer(self, observer: Observer) -> None:
        """Unregister an observer."""
        with self._lock:
            if observer in self.observers:
                self.observers.remove(observer)
                print(f"🎯 EventObserverManager: Unregistered observer '{observer.name}'")
    
    async def start_consuming(self) -> None:
        """Start consuming events from Redis queue and notifying observers."""
        if self.running:
            print("⚠️ EventObserverManager: Already consuming events")
            return
            
        self.running = True
        self.consumer_task = asyncio.create_task(self._consume_events())
        print(f"🚀 EventObserverManager: Started consuming events from {self.redis_pattern}")
    
    async def stop_consuming(self) -> None:
        """Stop consuming events."""
        if not self.running:
            return
            
        self.running = False
        if self.consumer_task:
            self.consumer_task.cancel()
            try:
                await self.consumer_task
            except asyncio.CancelledError:
                pass
        print("🛑 EventObserverManager: Stopped consuming events")
    
    async def _consume_events(self) -> None:
        """Continuously consume events from Redis pub/sub channel."""
        consecutive_errors = 0
        max_consecutive_errors = 10
        pubsub = None
        
        try:
            # Create pub/sub connection
            pubsub = self.redis_client.pubsub()
            await pubsub.psubscribe(self.redis_pattern)
            print(f"📡 EventObserverManager: PSubscribed to {self.redis_pattern}")
            
            while self.running:
                try:
                    # Listen for messages with timeout
                    message = await pubsub.get_message(timeout=1.0)
                    
                    if message and message['type'] in ('message', 'pmessage'):
                        event_json = message['data']
                        event_data = json.loads(event_json)
                        
                        # Convert to Event object
                        event = Event.from_dict(event_data)
                        
                        # Notify all observers
                        await self._notify_observers(event)
                        
                        self.events_consumed += 1
                        consecutive_errors = 0  # Reset error counter on success
                        
                        # Log progress every 100 events
                        if self.events_consumed % 100 == 0:
                            print(f"📊 EventObserverManager: Consumed {self.events_consumed} events")
                
                except Exception as e:
                    consecutive_errors += 1
                    self.delivery_failures += 1
                    print(f"❌ EventObserverManager: Error consuming events: {e}")
                    
                    if consecutive_errors >= max_consecutive_errors:
                        print(f"💥 EventObserverManager: Too many consecutive errors ({consecutive_errors}), stopping")
                        self.running = False
                        break
                    
                    # Exponential backoff on errors
                    await asyncio.sleep(min(2 ** consecutive_errors, 30))
        
        finally:
            # Clean up pub/sub connection
            if pubsub:
                try:
                    await pubsub.punsubscribe(self.redis_pattern)
                    await pubsub.close()
                    print(f"📡 EventObserverManager: Punsubscribed from {self.redis_pattern}")
                except Exception as e:
                    print(f"⚠️ EventObserverManager: Error closing pub/sub connection: {e}")
    
    async def _notify_observers(self, event: Event) -> None:
        """Notify all registered observers of an event."""
        with self._lock:
            observers_to_notify = list(self.observers)
        
        successful_deliveries = 0
        failed_deliveries = 0
        
        for observer in observers_to_notify:
            try:
                # Check if observer should receive this event
                if event.matches_filter(observer.get_event_filter()):
                    # Handle event safely (includes error tracking)
                    success = observer.handle_event_safely(event)
                    if success:
                        successful_deliveries += 1
                    else:
                        failed_deliveries += 1
                        
            except Exception as e:
                failed_deliveries += 1
                observer_name = getattr(observer, 'name', 'unknown')
                print(f"❌ EventObserverManager: Observer '{observer_name}' failed: {e}")
                import traceback
                traceback.print_exc()
        
        self.events_delivered += successful_deliveries
        
        # Log delivery stats for reasoning events
        if event.event_type in ["reasoning_step", "reasoning_meta", "reasoning"]:
            print(f"🎯 EventObserverManager: Delivered {event.event_type} to {successful_deliveries} observers")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get performance statistics."""
        uptime = time.time() - self.start_time
        events_per_second = self.events_consumed / uptime if uptime > 0 else 0
        
        return {
            "running": self.running,
            "observers_count": len(self.observers),
            "events_consumed": self.events_consumed,
            "events_delivered": self.events_delivered,
            "delivery_failures": self.delivery_failures,
            "uptime_seconds": uptime,
            "events_per_second": events_per_second,
            "success_rate": (self.events_delivered / self.events_consumed * 100) if self.events_consumed > 0 else 0
        }


# Global instance for the event observer manager
_event_observer_manager: Optional[EventObserverManager] = None


def get_event_observer_manager() -> EventObserverManager:
    """Get the global event observer manager instance."""
    global _event_observer_manager
    if _event_observer_manager is None:
        _event_observer_manager = EventObserverManager()
    return _event_observer_manager


async def start_event_observers() -> None:
    """Start the event observer system."""
    manager = get_event_observer_manager()
    await manager.start_consuming()


async def stop_event_observers() -> None:
    """Stop the event observer system."""
    manager = get_event_observer_manager()
    await manager.stop_consuming()


def register_event_observer(observer: Observer) -> None:
    """Register an observer with the event system."""
    manager = get_event_observer_manager()
    manager.register_observer(observer)


def unregister_event_observer(observer: Observer) -> None:
    """Unregister an observer from the event system."""
    manager = get_event_observer_manager()
    manager.unregister_observer(observer)
