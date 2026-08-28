"""
Motet - HTTP GET Tool

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-23

Description:
    Builtin HTTP GET tool for the Motet distributed framework.
    Provides synchronous HTTP GET capabilities with connection pooling,
    timeout handling, and comprehensive error management. Includes
    gevent/eventlet compatibility for distributed worker environments.

Dependencies:
    - httpx: HTTP client for web requests
    - pydantic: Data validation and model definitions
    - typing: Type hints and annotations
    - Tool registry and protocol system

Usage:
    from motet.core.tools.builtin.http_get import run_http_get

    # Get HTTP content
    result = run_http_get({
        "url": "https://example.com",
        "timeout": 30,
        "headers": {"User-Agent": "Motet"}
    })

Notes:
    - Provides synchronous HTTP GET capabilities
    - Includes connection pooling and timeout handling
    - Supports gevent/eventlet compatibility for distributed workers
    - Includes comprehensive error handling and logging
    - Supports custom headers and timeout configuration
    - Integrates with tool registry and protocol system
    - Includes comprehensive observability and monitoring
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx
from pydantic import BaseModel, Field

from ...config import Config
from ...constants import HTTP_MAX_CONNECTIONS, HTTP_MAX_KEEPALIVE_CONNECTIONS
from ...workers.observers import EventPriority
from ...workers.concurrency_primitives import WorkerLocal
from ..cache_control import attach_snapshot_cache_control
from ..protocol import ok, err
from ..registry import ToolRegistry


HTTP_LIMITS = httpx.Limits(max_connections=HTTP_MAX_CONNECTIONS, max_keepalive_connections=HTTP_MAX_KEEPALIVE_CONNECTIONS)
_http_client_local = WorkerLocal()


def _get_http_client() -> httpx.Client:
    """
    Get or create synchronous HTTP client per worker/greenlet (thread-safe).
    
    Uses WorkerLocal to ensure each worker/thread/greenlet has its own
    isolated HTTP client, preventing race conditions in concurrent environments.
    """
    if not hasattr(_http_client_local, 'client') or _http_client_local.client is None:
        _http_client_local.client = httpx.Client(limits=HTTP_LIMITS)
    elif _http_client_local.client.is_closed:
        # Recreate if closed
        try:
            _http_client_local.client.close()
        except Exception:
            pass  # best-effort cleanup before recreate
        _http_client_local.client = httpx.Client(limits=HTTP_LIMITS)
    return _http_client_local.client


class Params(BaseModel):
    url: str
    timeout: float = Field(default=10, ge=0.1, le=60)


def _parse(ln: str, trig: str) -> Dict[str, Any]:
    if trig in ("http:", "https:"):
        return {"url": ln}
    return {"url": ln[len(trig):].strip()}


def _fmt(res: Dict[str, Any]) -> str:
    status = res.get("status") if "status" in res else res.get("error", "unknown")
    return f"http_get(status={status})"


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronous HTTP GET (ADR-0033: gevent/eventlet compatible)."""
    url = params.get("url")
    timeout = float(params.get("timeout", 10))
    if not url:
        return err("url is required")
    try:
        cfg = Config()
        from ...security import is_host_allowed
        if not is_host_allowed(url, cfg.http_tool_allow_domains, cfg.http_tool_deny_domains):
            return err("domain not allowed" if cfg.http_tool_allow_domains else "domain denied")
    except Exception:
        pass  # fail-open: proceed if config/security unavailable
    headers = {"User-Agent": "imf-bot/0.1"}
    try:
        from structlog.contextvars import get_contextvars  # type: ignore
        ctx = get_contextvars() or {}
        trace_id = ctx.get("trace_id")
    except Exception:
        trace_id = None
    if trace_id:
        headers["X-Trace-Id"] = str(trace_id)
    client = _get_http_client()
    import time as _t
    _t0 = _t.perf_counter()
    try:
        r = client.get(url, headers=headers, timeout=timeout)
        _dur = round((_t.perf_counter() - _t0) * 1000, 2)
        try:
            import structlog
            structlog.get_logger().info("http_get_done", url=url, status=r.status_code, ms=_dur)
        except Exception:
            pass  # best-effort: logging must not fail
        return attach_snapshot_cache_control(
            "core.http_get",
            {"status": r.status_code, "headers": dict(r.headers), "text": r.text},
        )
    except Exception as e:
        _dur = round((_t.perf_counter() - _t0) * 1000, 2)
        try:
            import structlog
            structlog.get_logger().error("http_get_failed", url=url, error=str(e), ms=_dur)
        except Exception:
            pass  # best-effort: logging must not fail
        
        # Handle client closure issues
        error_msg = str(e)
        if "closed" in error_msg.lower():
            # Reset the worker-local client to force recreation
            if hasattr(_http_client_local, 'client') and _http_client_local.client is not None:
                try:
                    if not _http_client_local.client.is_closed:
                        _http_client_local.client.close()
                except Exception:
                    pass  # best-effort cleanup on reset
                _http_client_local.client = None
            return err("HTTP request failed due to client closure - client reset for next request")
        
        return err(f"HTTP request failed: {error_msg}")


def register(registry: ToolRegistry) -> None:
    # Import here to avoid circular dependencies
    try:
        from ..context_manager import ContextRequirement, ContextStrategy
        
        context_req = ContextRequirement(
            max_tokens=12000,           # Large context for web content
            preferred_tokens=6000,      # Preferred size
            overflow_strategy=ContextStrategy.PRIORITIZE,
            priority_fields=["text", "status", "error"],  # Keep important fields
            content_types=["text", "json", "html", "xml"]
        )
    except ImportError:
        context_req = None
    
    registry.register(
        name="core.http_get",
        description="Fetch a URL and return status, headers, and text snippet",
        func=run,
        tool_schema=Params,
        triggers=["http_get:"],
        priority=EventPriority.LOW,
        estimate_tokens=lambda _: 100,
        parse_params=_parse,
        observation_formatter=_fmt,
        breaker_failure_threshold=3,
        breaker_reset_timeout_seconds=20.0,
        max_retries=1,
        retry_backoff_seconds=0.5,
        category="http",
        data_types=["api", "web", "github", "rest", "json", "xml", "html"],
        keywords=["api", "github", "user", "profile", "repository", "http", "url", "fetch", "get", "request"],
        context_requirement=context_req,
    )


__all__ = ["register"]


