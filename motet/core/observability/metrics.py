"""
Motet - Metrics Collection

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-05

Description:
    Metrics collection system for the Motet distributed framework.
    Provides Prometheus-compatible metrics with histogram, counter, and gauge support.

Dependencies:
    - prometheus_client: Metrics collection and export
    - typing: Type hints and annotations
    - Optional dependencies with graceful fallbacks

Usage:
    from motet.core.observability.metrics import get_histogram, get_counter
    
    # Create metrics
    hist = get_histogram("operation_duration", "Operation duration in seconds")
    counter = get_counter("operations_total", "Total number of operations")

Notes:
    - Supports Prometheus metrics with optional dependencies
    - Includes graceful fallbacks when prometheus_client is unavailable
    - Provides thread-safe metric collection
    - Integrates with distributed architecture
"""

from __future__ import annotations

from typing import Any, Dict, Optional, cast

_PROMETHEUS_AVAILABLE = False
try:
    from prometheus_client import CollectorRegistry, Histogram, Counter, Gauge

    _PROMETHEUS_AVAILABLE = True
except Exception:  # pragma: no cover
    CollectorRegistry = object  # type: ignore[misc, assignment]
    Histogram = object  # type: ignore[misc, assignment]
    Counter = object  # type: ignore[misc, assignment]
    Gauge = object  # type: ignore[misc, assignment]

_registry: Optional[Any] = None
_histograms_by_registry: Dict[int, Dict[str, Any]] = {}
_counters_by_registry: Dict[int, Dict[str, Any]] = {}
_gauges_by_registry: Dict[int, Dict[str, Any]] = {}


def set_registry(registry: Any) -> None:
    global _registry
    _registry = registry


def get_registry() -> Optional[Any]:
    return _registry


def _get_hist(name: str, documentation: str, labelnames: list[str]) -> Optional[Any]:
    if _registry is None or not _PROMETHEUS_AVAILABLE:
        return None
    reg_id = id(_registry)
    cache = _histograms_by_registry.setdefault(reg_id, {})
    if name in cache:
        return cache[name]
    hist = cast(Any, Histogram)(name, documentation, labelnames, registry=_registry)
    cache[name] = hist
    return hist


def _get_counter(name: str, documentation: str, labelnames: list[str]) -> Optional[Any]:
    if _registry is None or not _PROMETHEUS_AVAILABLE:
        return None
    reg_id = id(_registry)
    cache = _counters_by_registry.setdefault(reg_id, {})
    if name in cache:
        return cache[name]
    ctr = cast(Any, Counter)(name, documentation, labelnames, registry=_registry)
    cache[name] = ctr
    return ctr


def _get_gauge(name: str, documentation: str, labelnames: list[str]) -> Optional[Any]:
    if _registry is None or not _PROMETHEUS_AVAILABLE:
        return None
    reg_id = id(_registry)
    cache = _gauges_by_registry.setdefault(reg_id, {})
    if name in cache:
        return cache[name]
    g = cast(Any, Gauge)(name, documentation, labelnames, registry=_registry)
    cache[name] = g
    return g


def observe_tool_latency(tool_name: str, seconds: float) -> None:
    hist = _get_hist("imf_tool_latency_seconds", "Tool execution latency", ["tool"])
    if hist is not None:
        hist.labels(tool=tool_name).observe(seconds)
    
    try:
        import os
        from .distributed_metrics import push_tool_latency_metric
        
        worker_id = os.getenv('CELERY_WORKER_ID', f'worker_{os.getpid()}')
        push_tool_latency_metric(worker_id, tool_name, seconds)
    except Exception:
        pass  # distributed push optional; local metrics sufficient


def increment_tool_requests(tool_name: str) -> None:
    ctr = _get_counter("imf_tool_requests_total", "Tool requests by tool", ["tool"])
    if ctr is not None:
        ctr.labels(tool=tool_name).inc()
    
    try:
        import os
        from .distributed_metrics import push_tool_request_metric
        
        worker_id = os.getenv('CELERY_WORKER_ID', f'worker_{os.getpid()}')
        push_tool_request_metric(worker_id, tool_name)
    except Exception:
        pass  # distributed push optional; local metrics sufficient


def increment_tool_errors(tool_name: str, reason: str) -> None:
    ctr = _get_counter("imf_tool_errors_total", "Tool errors by reason", ["tool", "reason"])
    if ctr is None:
        return
    ctr.labels(tool=tool_name, reason=reason).inc()


def observe_model_latency(provider: str, model: str, seconds: float) -> None:
    hist = _get_hist("imf_model_latency_seconds", "Model completion latency", ["provider", "model"])
    if hist is not None:
        hist.labels(provider=provider, model=model).observe(seconds)
    
    try:
        import os
        from .distributed_metrics import push_model_latency_metric
        
        worker_id = os.getenv('CELERY_WORKER_ID', f'worker_{os.getpid()}')
        push_model_latency_metric(worker_id, provider, model, seconds)
    except Exception:
        pass  # distributed push optional; local metrics sufficient


def increment_model_errors(provider: str, model: str, reason: str) -> None:
    ctr = _get_counter("imf_model_errors_total", "Model errors by reason", ["provider", "model", "reason"])
    if ctr is None:
        return
    ctr.labels(provider=provider, model=model, reason=reason).inc()


def increment_summaries_created(count: int = 1) -> None:
    ctr = _get_counter("imf_summaries_created_total", "Number of summaries created", [])
    if ctr is None:
        return
    ctr.inc(count)


def observe_scheduler_queue_wait(seconds: float) -> None:
    hist = _get_hist("imf_scheduler_queue_wait_seconds", "Time tasks waited in the scheduler queue", [])
    if hist is None:
        return
    hist.observe(seconds)


def set_scheduler_queue_length(length: int) -> None:
    g = _get_gauge("imf_scheduler_queue_length", "Current orchestrator scheduler queue length", [])
    if g is None:
        return
    g.set(float(length))


__all__ = [
    "get_registry",
    "set_registry",
    "observe_tool_latency",
    "increment_tool_requests",
    "increment_tool_errors",
    "observe_model_latency",
    "increment_model_errors",
    "increment_summaries_created",
    "observe_scheduler_queue_wait",
    "set_scheduler_queue_length",
]


