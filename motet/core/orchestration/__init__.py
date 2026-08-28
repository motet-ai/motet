"""
Motet - Orchestration Module

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-06

Description:
    Orchestration system for the Motet distributed framework.
    Owns the agent turn lifecycle, context preparation, and scheduling.
    Workflow runtime lives in peer package ``motet.core.workflow``.

Dependencies:
    - Distributed command system
    - Event bus and coordination

Usage:
    from motet.core.orchestration import Orchestrator
    
    # Create orchestrator
    orchestrator = Orchestrator()
    
    # Execute distributed commands
    result = await orchestrator.run(stack, messages)

Notes:
    - Turn lifecycle is orchestration's primary job
    - Import workflows from ``motet.core.workflow``, not this package
    - Integrates with distributed architecture
"""

from typing import Any
import importlib

__all__ = [
    # Main components  
    "Orchestrator", "TurnState", "OrchestrationConfig",
    
    # Command pattern (base classes only - use distributed commands for execution)
    "Command", "CommandContext", "CommandStatus",
    # "CommandInvoker" removed - use DistributedCommandInvoker instead
    
    # Enhanced existing circuit breaker (from resilience package)
    "CircuitBreaker", "get_breaker_configured", "ResilientServiceCaller", "global_resilient_caller",
    "FallbackStrategy", "DefaultValueFallback", "CachedResponseFallback",
    
    # Enhanced existing event bus (from eventing package)
    "global_bus", "Event", "EventFilter", "EventPriority", "Observer",
    "MemoryModuleObserver", "PerformanceObserver"
]

_LAZY_IMPORTS = {
    "Orchestrator": ("motet.core.orchestration.orchestrator", "DistributedOrchestrator"),
    "TurnState": ("motet.core.orchestration.orchestrator", "TurnState"),
    "OrchestrationConfig": ("motet.core.orchestration.orchestrator", "OrchestrationConfig"),
    "Command": ("motet.core.commands", "Command"),
    "CommandContext": ("motet.core.commands", "CommandContext"),
    "CommandStatus": ("motet.core.commands", "CommandStatus"),
    "CircuitBreaker": ("motet.core.resilience", "CircuitBreaker"),
    "get_breaker_configured": ("motet.core.resilience", "get_breaker_configured"),
    "ResilientServiceCaller": ("motet.core.resilience", "ResilientServiceCaller"),
    "global_resilient_caller": ("motet.core.resilience", "global_resilient_caller"),
    "FallbackStrategy": ("motet.core.resilience", "FallbackStrategy"),
    "DefaultValueFallback": ("motet.core.resilience", "DefaultValueFallback"),
    "CachedResponseFallback": ("motet.core.resilience", "CachedResponseFallback"),
    "global_bus": ("motet.core.workers", "global_bus"),
    "Event": ("motet.core.workers", "Event"),
    "EventFilter": ("motet.core.workers", "EventFilter"),
    "EventPriority": ("motet.core.workers", "EventPriority"),
    "Observer": ("motet.core.workers", "Observer"),
    "MemoryModuleObserver": ("motet.core.workers", "MemoryModuleObserver"),
    "PerformanceObserver": ("motet.core.workers", "PerformanceObserver"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_IMPORTS:
        module_name, attr_name = _LAZY_IMPORTS[name]
        module = importlib.import_module(module_name)
        return getattr(module, attr_name)
    raise AttributeError(f"module 'motet.core.orchestration' has no attribute '{name}'")


