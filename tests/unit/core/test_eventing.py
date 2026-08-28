from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from motet.core.distributed.tenant_keys import EVENT_BUS_PSUBSCRIBE_PATTERN
from motet.core.workers.event_observer_manager import EventObserverManager
from motet.core.workers.events import EventBus


@pytest.mark.skipif(not os.getenv("MOTET_TEST_REDIS_URL"), reason="requires Redis URL in MOTET_TEST_REDIS_URL")
@pytest.mark.asyncio
async def test_redis_event_roundtrip():
    """Test Redis-based event publishing and consumption."""
    url = os.getenv("MOTET_TEST_REDIS_URL")
    bus = EventBus(durable_backend="redis", redis_url=url)
    
    # Publish event; should enqueue to Redis regardless of observers
    ev = {"event_type": "step", "name": "math_eval", "duration": 0.01, "result": {"status": "success"}}
    await bus.publish(ev)
    
    # Consume events from Redis
    items = await bus.consume_durable(max_items=10)
    assert items and items[-1].get("event_type") == "step"


@pytest.mark.asyncio
async def test_event_observer_manager():
    """Test the new EventObserverManager functionality."""
    event_manager = EventObserverManager()
    
    # Create test observer that implements the Observer interface
    class TestObserver:
        def __init__(self):
            self.name = "test_observer"
            self.events = []
        
        def get_event_filter(self):
            """Return event filter for this observer."""
            from motet.core.workers.observers import EventFilter
            return EventFilter(event_types={"test"})
        
        def handle_event_safely(self, event):
            """Handle event safely - required by Observer interface."""
            try:
                self.events.append(event)
                return True
            except Exception:
                return False
    
    observer = TestObserver()
    
    # Register observer
    event_manager.register_observer(observer)
    assert observer in event_manager.observers
    
    # Test event notification using the private method (for testing purposes)
    from motet.core.workers.events import Event
    test_event = Event(
        event_type="test",
        data={"test_data": "test_value"},
        source="test_source"
    )
    await event_manager._notify_observers(test_event)
    
    # Verify observer received the event
    assert len(observer.events) == 1
    assert observer.events[0].event_type == "test"
    
    # Unregister observer
    event_manager.unregister_observer(observer)
    assert observer not in event_manager.observers


@pytest.mark.asyncio
async def test_streaming_observer_integration():
    """Test StreamingObserver deactivates on matching stop event."""
    from motet.core.workers.observers import StreamingObserver, Event

    streaming_observer = StreamingObserver(
        name="test_streaming",
        task_id="test_task_123",
        stream_until_event="command_completed",
        target_command_type="agent_turn"
    )

    assert streaming_observer._active

    test_event = Event(
        event_type="command_completed",
        data={"command_type": "agent_turn", "task_id": "test_task_123"},
        source="test_source"
    )

    # Patch the observers logger to avoid structlog conflict where
    # logger.debug(..., event=...) collides with structlog's positional `event` arg.
    with patch("motet.core.workers.observers.logger"):
        streaming_observer.on_event(test_event)

    assert not streaming_observer._active


def test_event_bus_publish_routes_tenant_vs_platform() -> None:
    """Tenant-attributed events use {tid}:events:channel; others stay platform."""
    bus = EventBus()
    fake_redis = MagicMock()
    bus._redis = fake_redis

    bus.publish({
        "kind": "command_started",
        "data": {"tenant_id": "acme", "command_id": "c1"},
    })
    channel, payload = fake_redis.publish.call_args[0]
    assert channel == "acme:events:channel"
    assert json.loads(payload)["data"]["tenant_id"] == "acme"

    fake_redis.reset_mock()
    bus.publish({"kind": "circuit_breaker", "state": "open"})
    channel, payload = fake_redis.publish.call_args[0]
    assert channel == "motet:events:channel"

    fake_redis.reset_mock()
    bus.publish({"kind": "command_started", "data": {"command_id": "c2"}})
    channel, _payload = fake_redis.publish.call_args[0]
    assert channel == "motet:events:channel"

    stats = bus.get_stats()
    assert stats["redis_channel"] == "motet:events:channel"


def test_event_observer_manager_uses_psubscribe_pattern() -> None:
    manager = EventObserverManager()
    assert manager.redis_pattern == EVENT_BUS_PSUBSCRIBE_PATTERN


@pytest.mark.asyncio
async def test_event_observer_manager_psubscribes_and_handles_pmessage() -> None:
    manager = EventObserverManager()
    received = []

    class _Observer:
        name = "pattern_observer"

        def get_event_filter(self):
            from motet.core.workers.observers import EventFilter
            return EventFilter()

        def handle_event_safely(self, event):
            received.append(event)
            return True

    manager.register_observer(_Observer())
    payload = json.dumps({
        "kind": "command_started",
        "data": {"tenant_id": "acme"},
    })
    messages = [
        {
            "type": "pmessage",
            "pattern": EVENT_BUS_PSUBSCRIBE_PATTERN,
            "channel": "acme:events:channel",
            "data": payload,
        },
        None,
    ]

    async def _get_message(timeout=1.0):
        if messages:
            return messages.pop(0)
        manager.running = False
        return None

    pubsub = AsyncMock()
    pubsub.get_message = _get_message
    manager.redis_client = MagicMock()
    manager.redis_client.pubsub = MagicMock(return_value=pubsub)
    manager.running = True
    await manager._consume_events()

    pubsub.psubscribe.assert_awaited_with(EVENT_BUS_PSUBSCRIBE_PATTERN)
    pubsub.punsubscribe.assert_awaited_with(EVENT_BUS_PSUBSCRIBE_PATTERN)
    assert len(received) == 1
    assert received[0].event_type == "command_started"
    assert received[0].data["tenant_id"] == "acme"


