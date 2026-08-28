"""
Motet - Circuit Breaker Implementation

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Implements a distributed circuit breaker pattern for resilience in the Motet
    distributed architecture. Provides fault tolerance and graceful degradation for
    distributed operations with configurable failure thresholds and timeouts.

Dependencies:
    - pydantic: For data validation and serialization
    - typing: For type hints and generic types
    - datetime: For time-based operations
    - collections: For deque-based sliding window

Usage:
    from motet.core.resilience.breaker import CircuitBreaker
    
    breaker = CircuitBreaker(failure_threshold=5, timeout=60)
    result = await breaker.call_async(some_operation)

Notes:
    - Thread-safe implementation with distributed lock support
    - Integrates with WorkerLock for distributed coordination
    - Supports both sync and async operations
    - Configurable failure thresholds and recovery timeouts
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable, Dict, Optional, TypeVar, Any, List, TYPE_CHECKING
from pydantic import BaseModel, Field
from datetime import datetime
from collections import deque
from abc import ABC, abstractmethod

if TYPE_CHECKING:
    from ..workers.concurrency_primitives import WorkerLock


T = TypeVar("T")

# Prometheus counters (optional); initialized lazily so imports without prometheus_client still work.
_CB_TRANSITIONS: Optional[Any] = None
_CB_BLOCKED: Optional[Any] = None


def _ensure_breaker_metrics() -> None:
    """Best-effort registration of breaker counters when prometheus_client is available."""
    global _CB_TRANSITIONS, _CB_BLOCKED
    if _CB_TRANSITIONS is not None:
        return
    try:
        from prometheus_client import Counter

        _CB_TRANSITIONS = Counter(
            "imf_breaker_transitions_total",
            "Circuit breaker transitions",
            ["to"],
        )
        _CB_BLOCKED = Counter(
            "imf_breaker_blocked_total",
            "Breaker blocked calls",
            ["reason"],
        )
    except Exception:
        _CB_TRANSITIONS = None
        _CB_BLOCKED = None


def _get_worker_lock():
    """Lazy import of WorkerLock to avoid circular dependencies."""
    from ..workers.concurrency_primitives import WorkerLock
    return WorkerLock()


class CircuitState:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"
class CircuitBreakerOpenException(RuntimeError):
    """Raised when a circuit breaker rejects a call due to OPEN state."""
    pass


class CallResult(BaseModel):
    """Result of a circuit breaker call"""
    success: bool
    result: Any = None
    error: Optional[Exception] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    duration_ms: float = 0.0
    
    model_config = {"arbitrary_types_allowed": True}


class FallbackStrategy(ABC):
    """Base class for fallback strategies when circuit breaker is open"""
    
    @abstractmethod
    def execute(self, service_name: str, original_error: Exception, *args, **kwargs) -> Any:
        """Execute fallback strategy"""
        pass


class DefaultValueFallback(FallbackStrategy):
    """Fallback to a default value"""
    
    def __init__(self, default_value: Any):
        self.default_value = default_value
    
    def execute(self, service_name: str, original_error: Exception, *args, **kwargs) -> Any:
        """Return default value"""
        return self.default_value


class CachedResponseFallback(FallbackStrategy):
    """Fallback to cached response"""
    
    def __init__(self, cache_ttl_seconds: int = 300):
        self.cache: Dict[str, Any] = {}
        self.cache_timestamps: Dict[str, float] = {}
        self.cache_ttl = cache_ttl_seconds
    
    def execute(self, service_name: str, original_error: Exception, *args, **kwargs) -> Any:
        """Return cached response if available"""
        cache_key = self._generate_cache_key(service_name, args, kwargs)
        current_time = time.time()
        
        if (cache_key in self.cache and 
            cache_key in self.cache_timestamps and
            current_time - self.cache_timestamps[cache_key] < self.cache_ttl):
            return self.cache[cache_key]
        
        # No valid cache entry
        raise RuntimeError(f"circuit_open_no_cache: {original_error}")
    
    def cache_response(self, service_name: str, response: Any, *args, **kwargs):
        """Cache a successful response"""
        cache_key = self._generate_cache_key(service_name, args, kwargs)
        self.cache[cache_key] = response
        self.cache_timestamps[cache_key] = time.time()
    
    def _generate_cache_key(self, service_name: str, args: tuple, kwargs: dict) -> str:
        """Generate cache key from service name and parameters"""
        return f"{service_name}:{hash((args, tuple(sorted(kwargs.items()))))}"


class CircuitBreaker:
    def __init__(self, 
                 *, 
                 failure_threshold: int = 5, 
                 reset_timeout_seconds: float = 30.0,
                 success_threshold: int = 1,
                 monitor_window_seconds: int = 300,
                 failure_rate_threshold: float = 0.5,
                 name: str = "unnamed") -> None:
        # Original parameters (backward compatibility)
        self.failure_threshold = max(1, int(failure_threshold))
        self.reset_timeout_seconds = max(1.0, float(reset_timeout_seconds))
        
        # Enhanced parameters
        self.success_threshold = max(1, int(success_threshold))
        self.monitor_window_seconds = max(60, int(monitor_window_seconds))
        self.failure_rate_threshold = max(0.0, min(1.0, float(failure_rate_threshold)))
        self.name = name
        
        # State management
        self._state: str = CircuitState.CLOSED
        self._failure_count: int = 0
        self._success_count: int = 0
        self._opened_at: Optional[float] = None
        self._half_open_probe_in_flight: bool = False
        # Pool-agnostic lock (WorkerLock from concurrency_primitives) for fork/threads/gevent
        self._lock = _get_worker_lock()
        
        # Enhanced tracking
        self.recent_calls: deque = deque(maxlen=100)
        self.total_calls = 0
        self.total_failures = 0
        self.total_successes = 0
        self.last_failure_time: Optional[float] = None
        self.last_success_time: Optional[float] = None
        self.state_changed_at = time.time()

        _ensure_breaker_metrics()

    @property
    def state(self) -> str:
        return self._state

    @property
    def failure_count(self) -> int:
        """Backward-compatible access to current failure count."""
        return self._failure_count

    def call(self, func: Callable[[], T]) -> T:
        """Enhanced call method with failure rate monitoring and detailed tracking"""
        with self._lock:
            self.total_calls += 1
            
            # Check failure rate
            self._check_failure_rate()
            
            now = time.monotonic()
            if self._state == CircuitState.OPEN:
                if self._opened_at is not None and (now - self._opened_at) >= self.reset_timeout_seconds:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_probe_in_flight = False
                    self.state_changed_at = now
                    self._emit_state_change_event("half_open")
                    try:
                        _CB_TRANSITIONS.labels("half_open").inc()  # type: ignore
                    except Exception:
                        pass  # metrics best-effort; don't fail circuit
                else:
                    try:
                        _CB_BLOCKED.labels("circuit_open").inc()  # type: ignore
                    except Exception:
                        pass  # metrics best-effort; don't fail circuit
                    self._emit_circuit_event("call_rejected")
                    raise CircuitBreakerOpenException("circuit_open")

            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_probe_in_flight:
                    try:
                        _CB_BLOCKED.labels("circuit_half_open_probe_in_flight").inc()  # type: ignore
                    except Exception:
                        pass  # metrics best-effort; don't fail circuit
                    raise RuntimeError("circuit_half_open_probe_in_flight")
                self._half_open_probe_in_flight = True

        # Execute function with timing
        start_time = time.time()
        call_result = self._execute_with_tracking(func)
        duration_ms = (time.time() - start_time) * 1000
        
        call_result.duration_ms = duration_ms
        self.recent_calls.append(call_result)

        # Handle result
        if call_result.success:
            self._handle_success()
            return call_result.result
        else:
            err = call_result.error
            if err is None:
                err = RuntimeError("circuit_call_failed_unknown_error")
            self._handle_failure(err)
            raise err

    async def call_async(self, func: Callable[[], T]) -> T:
        """Async variant of call(); use for async callables so failures are observed by the breaker.
        Uses same WorkerLock (concurrency_primitives) as call(); lock released before awaiting."""
        with self._lock:
            self.total_calls += 1
            self._check_failure_rate()
            now = time.monotonic()
            if self._state == CircuitState.OPEN:
                if self._opened_at is not None and (now - self._opened_at) >= self.reset_timeout_seconds:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_probe_in_flight = False
                    self.state_changed_at = now
                    self._emit_state_change_event("half_open")
                    try:
                        _CB_TRANSITIONS.labels("half_open").inc()  # type: ignore
                    except Exception:
                        pass  # metrics best-effort; don't fail circuit
                else:
                    try:
                        _CB_BLOCKED.labels("circuit_open").inc()  # type: ignore
                    except Exception:
                        pass  # metrics best-effort; don't fail circuit
                    self._emit_circuit_event("call_rejected")
                    raise RuntimeError("circuit_open")
            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_probe_in_flight:
                    try:
                        _CB_BLOCKED.labels("circuit_half_open_probe_in_flight").inc()  # type: ignore
                    except Exception:
                        pass  # metrics best-effort; don't fail circuit
                    raise RuntimeError("circuit_half_open_probe_in_flight")
                self._half_open_probe_in_flight = True

        start_time = time.time()
        try:
            result = func()
            # We're already in async context (call_async); await coroutines directly.
            # Do not use run_async_safe here—that is for sync callers running async code.
            if asyncio.iscoroutine(result):
                result = await result
            call_result = CallResult(success=True, result=result)
        except Exception as e:
            call_result = CallResult(success=False, error=e)
        duration_ms = (time.time() - start_time) * 1000
        call_result.duration_ms = duration_ms
        self.recent_calls.append(call_result)

        if call_result.success:
            self._handle_success()
            if self._state == CircuitState.HALF_OPEN:
                with self._lock:
                    self._half_open_probe_in_flight = False
            return call_result.result
        else:
            if self._state == CircuitState.HALF_OPEN:
                with self._lock:
                    self._half_open_probe_in_flight = False
            err = call_result.error
            if err is None:
                err = RuntimeError("circuit_async_call_failed_unknown_error")
            self._handle_failure(err)
            raise err

    def _execute_with_tracking(self, func: Callable[[], T]) -> CallResult:
        """Execute function and track the result"""
        try:
            result = func()
            return CallResult(success=True, result=result)
        except Exception as e:
            return CallResult(success=False, error=e)

    def _handle_success(self):
        """Handle a successful call"""
        with self._lock:
            self.total_successes += 1
            self.last_success_time = time.time()
            
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._transition_to_closed()
            elif self._state == CircuitState.CLOSED:
                # Reset failure count on success
                self._failure_count = 0

    def _handle_failure(self, error: Exception):
        """Handle a failed call"""
        with self._lock:
            self.total_failures += 1
            self._failure_count += 1
            self.last_failure_time = time.time()
            
            # Reset success count
            self._success_count = 0
            
            # Check if we should open the circuit
            if (self._state in [CircuitState.CLOSED, CircuitState.HALF_OPEN] and
                self._failure_count >= self.failure_threshold):
                self._transition_to_open()
            
            self._emit_circuit_event("call_failed", {"error": str(error)})

    def _check_failure_rate(self):
        """Check if failure rate exceeds threshold"""
        if len(self.recent_calls) < 10:  # Need minimum calls to calculate rate
            return
        
        current_time = time.time()
        window_start = current_time - self.monitor_window_seconds
        
        # Filter calls within the monitoring window
        recent_calls_in_window = [
            call for call in self.recent_calls
            if call.timestamp.timestamp() >= window_start
        ]
        
        if len(recent_calls_in_window) < 5:  # Need minimum calls
            return
        
        failure_rate = sum(1 for call in recent_calls_in_window if not call.success) / len(recent_calls_in_window)
        
        if (failure_rate >= self.failure_rate_threshold and 
            self._state == CircuitState.CLOSED):
            self._transition_to_open()
            self._emit_circuit_event("opened_by_failure_rate", {"failure_rate": failure_rate})

    def _transition_to_open(self):
        """Transition circuit to OPEN state"""
        old_state = self._state
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
        self.state_changed_at = time.time()
        self._success_count = 0
        
        self._emit_state_change_event("open", old_state)
        try:
            _CB_TRANSITIONS.labels("open").inc()  # type: ignore
        except Exception:
            pass  # metrics best-effort; don't fail circuit

    def _transition_to_closed(self):
        """Transition circuit to CLOSED state"""
        old_state = self._state
        self._state = CircuitState.CLOSED
        self.state_changed_at = time.time()
        self._success_count = 0
        self._failure_count = 0
        self._half_open_probe_in_flight = False
        
        self._emit_state_change_event("closed", old_state)
        try:
            _CB_TRANSITIONS.labels("closed").inc()  # type: ignore
        except Exception:
            pass  # metrics best-effort; don't fail circuit

    def _emit_state_change_event(
        self, new_state: str, old_state: Optional[str] = None
    ) -> None:
        """Emit state change event"""
        self._emit_circuit_event("state_changed", {
            "old_state": old_state or "unknown",
            "new_state": new_state,
            "failure_count": self._failure_count
        })

    def _emit_circuit_event(
        self, event_type: str, data: Optional[Dict[str, Any]] = None
    ) -> None:
        """Emit circuit breaker events"""
        try:
            from ..workers import global_bus
            global_bus.publish({
                "kind": f"circuit_breaker_{event_type}",
                "circuit_name": self.name,
                "state": self._state,
                "failure_count": self._failure_count,
                "success_count": self._success_count,
                **(data or {})
            })
        except Exception:
            pass  # Don't fail on event emission

    def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive circuit breaker statistics"""
        current_time = time.time()
        time_in_current_state = current_time - self.state_changed_at
        
        # Calculate recent failure rate
        recent_failures = sum(1 for call in self.recent_calls if not call.success)
        recent_failure_rate = recent_failures / len(self.recent_calls) if self.recent_calls else 0.0
        
        # Calculate average response time
        recent_durations = [call.duration_ms for call in self.recent_calls if call.success]
        avg_response_time = sum(recent_durations) / len(recent_durations) if recent_durations else 0.0
        
        return {
            "name": self.name,
            "state": self._state,
            "time_in_current_state_seconds": time_in_current_state,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "total_calls": self.total_calls,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
            "recent_failure_rate": recent_failure_rate,
            "avg_response_time_ms": avg_response_time,
            "last_failure_time": self.last_failure_time,
            "last_success_time": self.last_success_time,
            "config": {
                "failure_threshold": self.failure_threshold,
                "reset_timeout_seconds": self.reset_timeout_seconds,
                "success_threshold": self.success_threshold,
                "failure_rate_threshold": self.failure_rate_threshold
            }
        }

    def force_open(self):
        """Manually force circuit to OPEN state"""
        with self._lock:
            self._transition_to_open()
            self._emit_circuit_event("manually_opened")

    def force_close(self):
        """Manually force circuit to CLOSED state"""
        with self._lock:
            self._transition_to_closed()
            self._emit_circuit_event("manually_closed")

    def reset(self):
        """Reset circuit breaker statistics"""
        with self._lock:
            self._failure_count = 0
            self._success_count = 0
            self.last_failure_time = None
            self.last_success_time = None
            self.recent_calls.clear()
            self.total_calls = 0
            self.total_failures = 0
            self.total_successes = 0
            
            if self._state != CircuitState.CLOSED:
                self._transition_to_closed()
            
            self._emit_circuit_event("reset")


_breakers: Dict[str, CircuitBreaker] = {}


def get_breaker(name: str, *, failure_threshold: int, reset_timeout_seconds: float, **kwargs) -> CircuitBreaker:
    """Get or create a circuit breaker with enhanced options (backward compatible)"""
    br = _breakers.get(name)
    if br is None:
        br = CircuitBreaker(
            failure_threshold=failure_threshold, 
            reset_timeout_seconds=reset_timeout_seconds,
            name=name,
            **kwargs
        )
        _breakers[name] = br
    return br


def get_breaker_configured(
    name: str,
    *,
    default_failure_threshold: int,
    default_reset_timeout_seconds: float,
    override_failure_threshold: Optional[int] = None,
    override_reset_timeout_seconds: Optional[float] = None,
    **kwargs
) -> CircuitBreaker:
    """Enhanced get_breaker_configured with additional options"""
    failure_threshold = int(override_failure_threshold if override_failure_threshold is not None else default_failure_threshold)
    reset_timeout = float(override_reset_timeout_seconds if override_reset_timeout_seconds is not None else default_reset_timeout_seconds)
    return get_breaker(
        name, 
        failure_threshold=failure_threshold, 
        reset_timeout_seconds=reset_timeout,
        **kwargs
    )


def get_all_breaker_stats() -> Dict[str, Dict[str, Any]]:
    """Get statistics for all circuit breakers"""
    return {name: breaker.get_stats() for name, breaker in _breakers.items()}


def reset_all_breakers():
    """Reset all circuit breakers"""
    for breaker in _breakers.values():
        breaker.reset()


class ResilientServiceCaller:
    """Enhanced service caller with fallback strategies using existing circuit breakers"""
    
    def __init__(self):
        self.fallback_strategies: Dict[str, FallbackStrategy] = {}
        self.cached_fallback = CachedResponseFallback()
    
    def register_fallback(self, service_name: str, strategy: FallbackStrategy):
        """Register fallback strategy for a service"""
        self.fallback_strategies[service_name] = strategy
    
    def call_service(self, 
                          service_name: str, 
                          func: Callable[[], T], 
                          use_fallback: bool = True,
                          **circuit_config) -> T:
        """Call service with circuit breaker protection and fallback strategies"""
        
        # Get or create circuit breaker using existing infrastructure
        circuit_breaker = get_breaker_configured(
            service_name,
            default_failure_threshold=circuit_config.get('failure_threshold', 5),
            default_reset_timeout_seconds=circuit_config.get('reset_timeout_seconds', 30.0),
            success_threshold=circuit_config.get('success_threshold', 1),
            monitor_window_seconds=circuit_config.get('monitor_window_seconds', 300),
            failure_rate_threshold=circuit_config.get('failure_rate_threshold', 0.5)
        )
        
        try:
            result = circuit_breaker.call(func)
            
            # Cache successful response for fallback
            self.cached_fallback.cache_response(service_name, result)
            
            return result
            
        except RuntimeError as e:
            if "circuit_open" in str(e) and use_fallback:
                # Try fallback strategies
                return self._execute_fallback(service_name, e)
            raise
    
    def _execute_fallback(self, service_name: str, original_error: Exception) -> Any:
        """Execute fallback strategies in order"""
        # Try service-specific fallback first
        if service_name in self.fallback_strategies:
            try:
                return self.fallback_strategies[service_name].execute(
                    service_name, original_error
                )
            except Exception:
                pass  # Try next fallback
        
        # Try cached response fallback
        try:
            return self.cached_fallback.execute(service_name, original_error)
        except Exception:
            pass  # try next fallback or re-raise

        # No fallback available
        raise RuntimeError(
            f"Circuit breaker for '{service_name}' is open and no fallback available"
        ) from original_error
    
    def get_circuit_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get statistics for all circuit breakers"""
        return get_all_breaker_stats()


# Global resilient service caller instance
global_resilient_caller = ResilientServiceCaller()


__all__ = [
    "CircuitBreaker", 
    "CircuitState",
    "CircuitBreakerOpenException",
    "get_breaker", 
    "get_breaker_configured",
    "get_all_breaker_stats",
    "reset_all_breakers",
    "FallbackStrategy",
    "DefaultValueFallback",
    "CachedResponseFallback",
    "ResilientServiceCaller",
    "global_resilient_caller"
]


