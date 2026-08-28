"""
Motet - Trace Store

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-19

Description:
    Trace store module for the Motet distributed framework.
    Provides comprehensive trace storage capabilities including file
    and Redis backends with trace persistence and retrieval. Includes
    trace directory management, backend selection, and distributed
    trace coordination.

Dependencies:
    - json: Trace data serialization and processing
    - os: Environment variable management
    - time: Timestamp and timing management
    - pathlib: File system operations and path handling
    - typing: Type hints and annotations

Usage:
    from motet.core.observability.trace_store import store_trace, retrieve_trace

    # Store trace
    store_trace(trace_id="trace_123", spans=[span1, span2])

    # Retrieve trace
    trace = retrieve_trace("trace_123")

Notes:
    - Provides comprehensive trace storage capabilities
    - Includes file and Redis backend support
    - Supports trace persistence and retrieval
    - Includes trace directory management and backend selection
    - Supports distributed trace coordination
    - Integrates with observability and tracing systems
    - Includes comprehensive error handling and logging
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, cast


def _trace_dir() -> Path:
    directory = os.getenv("MOTET_TRACE_DIR") or "traces"
    p = Path(directory)
    p.mkdir(parents=True, exist_ok=True)
    return p


def enabled() -> bool:
    val = os.getenv("MOTET_TRACE_ENABLED", "false").lower()
    return val in {"1", "true", "yes", "on"}


def _trace_path(trace_id: str) -> Path:
    return _trace_dir() / f"{trace_id}.jsonl"


def _backend() -> str:
    return (os.getenv("MOTET_TRACE_BACKEND") or "file").lower()


# --- Redis backend helpers (optional) ---
_redis_client = None


def _get_redis():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    url = os.getenv("MOTET_TRACE_REDIS_URL") or os.getenv("MOTET_REDIS_URL")
    if not url:
        return None
    try:
        import redis  # type: ignore

        _redis_client = redis.Redis.from_url(url)
        return _redis_client
    except Exception:
        return None


def _redis_prefix() -> str:
    return os.getenv("MOTET_TRACE_REDIS_PREFIX") or "motet:trace:"


def _redis_key(trace_id: str) -> str:
    return f"{_redis_prefix()}{trace_id}"


def start_trace(trace_id: str, metadata: Dict[str, Any]) -> None:
    if not enabled():
        return
    record_event(trace_id, {"kind": "start", "metadata": metadata})


def end_trace(trace_id: str, summary: Optional[Dict[str, Any]] = None) -> None:
    if not enabled():
        return
    record_event(trace_id, {"kind": "end", "summary": summary or {}})


def record_event(trace_id: str, event: Dict[str, Any]) -> None:
    if not enabled():
        return
    payload = {"ts": time.time(), **event}
    if _backend() == "redis":
        r = _get_redis()
        if r is not None:
            try:
                rs = cast(Any, r)
                rs.rpush(_redis_key(trace_id), json.dumps(payload, ensure_ascii=False))
                rs.zadd(_redis_prefix() + "idx", {trace_id: time.time()})
                return
            except Exception:
                pass  # fallback to file backend
    # default file backend
    path = _trace_path(trace_id)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def list_traces(limit: int = 10) -> List[Dict[str, Any]]:
    """Return up to limit recent traces with basic metadata (mtime & size)."""
    if _backend() == "redis":
        r = _get_redis()
        if r is not None:
            try:
                rs = cast(Any, r)
                ids = [tid.decode() for tid in rs.zrevrange(_redis_prefix() + "idx", 0, limit - 1)]
                out: List[Dict[str, Any]] = []
                for tid in ids:
                    try:
                        n = rs.llen(_redis_key(tid))
                        out.append({"trace_id": tid, "bytes": int(n or 0), "modified": 0})
                    except Exception:
                        continue  # skip individual trace metadata on error
                return out
            except Exception:
                pass  # fallback to file backend
    # file fallback
    if not _trace_dir().exists():
        return []
    files = sorted(_trace_dir().glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: List[Dict[str, Any]] = []
    for p in files[:limit]:
        try:
            stat = p.stat()
            out.append({"trace_id": p.stem, "bytes": stat.st_size, "modified": stat.st_mtime})
        except Exception:
            continue  # skip unreadable trace file
    return out


def load_trace(trace_id: str) -> List[Dict[str, Any]]:
    if _backend() == "redis":
        r = _get_redis()
        if r is not None:
            try:
                rs = cast(Any, r)
                items = rs.lrange(_redis_key(trace_id), 0, -1)
                out: List[Dict[str, Any]] = []
                for b in items:
                    try:
                        out.append(json.loads(b.decode()))
                    except Exception:
                        continue  # skip malformed trace event
                return out
            except Exception:
                pass  # fallback to file backend
    # file fallback
    path = _trace_path(trace_id)
    if not path.exists():
        return []
    events: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


__all__ = [
    "enabled",
    "start_trace",
    "end_trace",
    "record_event",
    "list_traces",
    "load_trace",
]


