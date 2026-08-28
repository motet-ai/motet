"""
Motet - Worker Utils

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Worker utils for the Motet distributed framework.

Dependencies:
    - typing: Type hints and annotations
    - Base interfaces and implementations

Usage:
    from motet.core.workers.worker_utils import WorkerUtils

Notes:
    - Provides core functionality
    - Integrates with distributed architecture
"""


import os
import sys
import socket
from typing import Optional, Literal


def get_worker_id() -> str:
    """
    Generate a consistent worker ID that works across containers and processes.
    
    This function ensures that the same worker ID is used across:
    - Worker registration (tasks.py)
    - Command processing (command_tasks.py)
    - Readiness service interactions
    - Celery routing and monitoring
    
    The default worker ID format is: cloud_{hostname}.
    For edge-device workers, preserve canonical edge IDs (`edge_{device}`).
    
    Returns:
        str: Consistent worker ID for this worker instance
    """
    hostname = socket.gethostname()
    
    # Use environment variable if available (set by Docker Compose)
    raw_worker_id = str(os.environ.get('MOTET_WORKER_ID', hostname) or "").strip()
    if not raw_worker_id:
        raw_worker_id = hostname

    # Keep canonical edge worker IDs stable across readiness/mcp paths.
    if raw_worker_id.startswith("edge_"):
        return raw_worker_id
    # Avoid accidental double-prefixing.
    if raw_worker_id.startswith("cloud_"):
        return raw_worker_id

    return f"cloud_{raw_worker_id}"


def get_lifecycle_worker_id() -> str:
    """
    Return the canonical worker ID of the lifecycle/deployer worker.

    The lifecycle worker is the one that runs worker lifecycle and deployment
    commands (ADR-0071). Its ID must match the format used by get_worker_id()
    (cloud_{MOTET_WORKER_ID}) so routing and capability checks agree.

    Returns:
        str: Worker ID of the lifecycle worker (e.g. cloud_cloud_lifecycle_management)
    """
    lifecycle_env = os.environ.get(
        "MOTET_WORKER_LIFECYCLE_WORKER_ID",
        "lifecycle_management",
    )
    return f"cloud_{lifecycle_env}"


def get_worker_hostname() -> str:
    """
    Get the raw hostname for this worker.
    
    Returns:
        str: The hostname of the current worker
    """
    return socket.gethostname()


def is_valid_worker_id(worker_id: str) -> bool:
    """
    Validate that a worker ID follows the expected format.
    
    Args:
        worker_id: The worker ID to validate
        
    Returns:
        bool: True if the worker ID is valid, False otherwise
    """
    if not worker_id or not isinstance(worker_id, str):
        return False
    
    if worker_id.startswith('cloud_') and len(worker_id) > len('cloud_'):
        return True
    if worker_id.startswith('edge_') and len(worker_id) > len('edge_'):
        return True
    return False


def extract_hostname_from_worker_id(worker_id: str) -> Optional[str]:
    """
    Extract the hostname portion from a worker ID.
    
    Args:
        worker_id: The worker ID (e.g., 'agent_hostname123')
        
    Returns:
        Optional[str]: The hostname portion, or None if invalid format
    """
    if not is_valid_worker_id(worker_id):
        return None
    
    if worker_id.startswith('cloud_'):
        return worker_id[len('cloud_'):]
    if worker_id.startswith('edge_'):
        return worker_id[len('edge_'):]
    return None


def get_worker_routing_key(worker_id: Optional[str] = None) -> str:
    """
    Generate a Celery routing key for a specific worker.
    
    Args:
        worker_id: Optional worker ID. If None, uses current worker ID.
        
    Returns:
        str: Celery routing key for the worker
    """
    if worker_id is None:
        worker_id = get_worker_id()
    
    return f"worker.{worker_id}"


def detect_worker_pool_type() -> Literal["eventlet", "gevent", "threads", "fork"]:
    """
    Detect which Celery worker pool type is being used (ADR-0033).

    Detection order (most reliable to least reliable):
    1. MOTET_CELERY_POOL / CELERY_POOL env vars set explicitly by the operator
    2. --pool CLI argument in sys.argv (set by Celery startup command)
    3. Monkey-patched modules (eventlet/gevent load at import time)
    4. os.fork capability (Unix default when nothing above matches)

    The pool type determines the worker's concurrency characteristics:
    - eventlet/gevent: High concurrency (1000s of I/O operations), shared memory
    - threads: Medium concurrency (10-100s of I/O operations), shared memory
    - fork: Process isolation, separate memory, best for CPU-heavy work

    Returns:
        str: Pool type - "eventlet", "gevent", "threads", or "fork"

    Example:
        >>> pool_type = detect_worker_pool_type()
        >>> print(f"Worker using {pool_type} pool")
    """
    _VALID = {"eventlet", "gevent", "threads", "prefork", "fork", "solo"}
    _MAP: dict[str, Literal["eventlet", "gevent", "threads", "fork"]] = {
        "eventlet": "eventlet",
        "gevent": "gevent",
        "threads": "threads",
        "thread": "threads",
        "prefork": "fork",
        "fork": "fork",
        "solo": "threads",  # solo runs in the main thread; treat as threads
    }

    # 1. Environment variable (highest priority — operator explicitly set this)
    for env_var in ("MOTET_CELERY_POOL", "CELERY_POOL"):
        env_val = os.environ.get(env_var, "").strip().lower()
        if env_val in _MAP:
            return _MAP[env_val]

    # 2. CLI argument — Celery passes --pool=<type> when starting the worker
    for i, arg in enumerate(sys.argv):
        if arg.startswith("--pool="):
            val = arg.split("=", 1)[1].strip().lower()
            if val in _MAP:
                return _MAP[val]
        elif arg == "--pool" and i + 1 < len(sys.argv):
            val = sys.argv[i + 1].strip().lower()
            if val in _MAP:
                return _MAP[val]

    # 3. Monkey-patched modules (eventlet/gevent patch at import; reliable signal)
    if "eventlet" in sys.modules:
        return "eventlet"
    if "gevent" in sys.modules:
        return "gevent"

    # 4. os.fork presence — only fall back to 'fork' when no explicit pool is set.
    #    Previously this check ran too early and always returned 'fork' on Linux
    #    even when --pool=threads was specified.
    if hasattr(os, "fork"):
        return "fork"

    return "threads"


