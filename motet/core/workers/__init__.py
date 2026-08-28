"""
Motet - Workers Module

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Distributed worker system for the Motet framework.
    Provides event bus, observers, command execution, and distributed coordination.

Dependencies:
    - Event bus and messaging system
    - Observer pattern for monitoring
    - Command execution and routing
    - Distributed coordination primitives

Usage:
    from motet.core.workers import global_bus, Event, Observer
    
    # Publish events
    await global_bus.publish(Event(...))
    
    # Register observers
    observer = MyObserver()
    global_bus.register_observer(observer)

Notes:
    - Provides distributed event bus with Redis support
    - Includes comprehensive observer system
    - Supports command execution and routing
    - Integrates with distributed coordination
"""

from .events import EventBus, global_bus, safe_publish
from .observers import (
    Event, EventFilter, EventPriority, Observer,
    MemoryModuleObserver, PerformanceObserver,
    # Distributed observers
    WorkerObserver, CommandRoutingObserver, DistributedExecutionObserver,
    # Streaming observers
    StreamingObserver
)
from .event_observer_manager import (
    EventObserverManager, get_event_observer_manager,
    start_event_observers, stop_event_observers,
    register_event_observer, unregister_event_observer
)
# IMPORTANT:
# Keep command_invoker imports lazy to avoid circular-import chains during early
# startup (e.g., registry modules importing concurrency primitives).
# Command routing is WorkerRouter; dispatch is direct Celery send_task.

__all__ = [
    # Core event bus (Redis-only)
    "EventBus", 
    "global_bus",
    "safe_publish",
    
    # Enhanced observer system
    "Event",
    "EventFilter", 
    "EventPriority",
    "Observer",
    
    # Pre-built observers
    "MemoryModuleObserver",
    "PerformanceObserver",
    
    # Distributed observers
    "WorkerObserver",
    "CommandRoutingObserver", 
    "DistributedExecutionObserver",
    
    # Streaming observers
    "StreamingObserver",
    
    # Event observer system
    "EventObserverManager",
    "get_event_observer_manager",
    "start_event_observers",
    "stop_event_observers",
    "register_event_observer",
    "unregister_event_observer",
    
    # Command invocation (new system)
    "DistributedCommandInvoker",
    "DistributedInvokerNode", 
    "new_global_invoker",
    "global_invoker",
]


def __getattr__(name: str):
    """
    Lazily resolve heavy command invoker symbols.

    This keeps `import motet.core.workers` lightweight so importing
    `motet.core.workers.concurrency_primitives` does not pull command
    orchestration modules during module initialization.
    """
    if name in {
        "DistributedCommandInvoker",
        "DistributedInvokerNode",
        "new_global_invoker",
        "global_invoker",
    }:
        from .command_invoker import (
            DistributedCommandInvoker,
            DistributedInvokerNode,
            new_global_invoker,
        )
        mapping = {
            "DistributedCommandInvoker": DistributedCommandInvoker,
            "DistributedInvokerNode": DistributedInvokerNode,
            "new_global_invoker": new_global_invoker,
            "global_invoker": new_global_invoker,  # legacy alias
        }
        return mapping[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


