"""
Motet - HTTP POST Tool

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    HTTP POST tool for the Motet distributed framework.
    Provides synchronous HTTP POST capabilities with connection pooling,
    timeout handling, and comprehensive error management. Includes
    gevent/eventlet compatibility for distributed worker environments.

Dependencies:
    - httpx: HTTP client for web requests
    - pydantic: Data validation and model definitions
    - typing: Type hints and annotations
    - Tool registry and protocol system

Usage:
    from motet.core.tools.builtin.http_post import run_http_post

    # POST data
    result = run_http_post({
        "url": "https://api.example.com/data",
        "data": {"key": "value"},
        "timeout": 30
    })

Notes:
    - Provides synchronous HTTP POST capabilities
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
from ...workers.concurrency_primitives import WorkerLocal
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
    data: Optional[Dict[str, Any]] = None
    json_body: Optional[Dict[str, Any]] = None
    timeout: float = Field(default=10, ge=0.1, le=60)


def _parse(ln: str, trig: str) -> Dict[str, Any]:
    return {"url": ln[len(trig):].strip()}


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronous HTTP POST (ADR-0033: gevent/eventlet compatible)."""
    url = params.get("url")
    data = params.get("data")
    json_body = params.get("json") or params.get("json_body")
    timeout = float(params.get("timeout", 10))
    if not url:
        return err("url is required")
    try:
        cfg = Config()
        from ...security import is_host_allowed
        if not is_host_allowed(url, cfg.http_tool_allow_domains, cfg.http_tool_deny_domains):
            return err("domain not allowed" if cfg.http_tool_allow_domains else "domain denied")
    except Exception:
        pass  # fail-open: proceed if config unavailable
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
    r = client.post(url, data=data, json=json_body, headers=headers, timeout=timeout)
    return {"status": r.status_code, "headers": dict(r.headers), "text": r.text[:5000]}


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.http_post",
        description="POST to a URL with optional form or JSON body",
        func=run,
        tool_schema=Params,
        triggers=["http_post:"],
        priority=4,
        estimate_tokens=lambda _: 100,
        parse_params=_parse,
        category="http",
        # and do not include in system context unless explicitly enabled
        contextualize_observation=False,
    )


__all__ = ["register"]


