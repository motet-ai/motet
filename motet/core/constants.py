"""
Motet - Shared Constants

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Centralized constants and default values for the Motet runtime.
    Eliminates magic numbers and hardcoded strings scattered across modules.

Usage:
    from motet.core.constants import DEFAULT_REDIS_URL, REDIS_MAX_CONNECTIONS

Notes:
    - All defaults can be overridden via Config (environment variables)
    - Constants here are compile-time defaults, not runtime config
    - REDIS_MAX_CONNECTIONS default (1250) targets worker processes with high
      Celery concurrency + BLPOP wait headroom. Size as roughly
      concurrency + short-op buffer per process; keep fleet sync pools under
      Redis maxclients. Override per deploy via MOTET_REDIS_MAX_CONNECTIONS
      (API processes can use a lower value).
"""

# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------
DEFAULT_REDIS_URL = "redis://localhost:6379/0"
REDIS_MAX_CONNECTIONS = 1250
REDIS_PUBSUB_MAX_CONNECTIONS = 32
REDIS_SOCKET_TIMEOUT = 5.0
REDIS_RETRY_ON_TIMEOUT = True

# ---------------------------------------------------------------------------
# HTTP client limits (for built-in tools)
# ---------------------------------------------------------------------------
HTTP_MAX_CONNECTIONS = 20
HTTP_MAX_KEEPALIVE_CONNECTIONS = 10

# ---------------------------------------------------------------------------
# Circuit breaker defaults (also in Config for override)
# ---------------------------------------------------------------------------
DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
DEFAULT_CIRCUIT_BREAKER_TIMEOUT_SECONDS = 60

# ---------------------------------------------------------------------------
# Distributed command execution
# ---------------------------------------------------------------------------
DEFAULT_COMMAND_TIMEOUT_SECONDS = 60
DEFAULT_COMMAND_MAX_RETRIES = 2
# Celery task name for process_distributed_command. Communicator wait, the
# task_postrun result-wake hook, and send_task sites must share this string —
# renaming it in one place only hangs every parent wait (ADR-0131).
CELERY_PROCESS_COMMAND_TASK = "imf.commands.process"

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
DEFAULT_RATE_LIMIT_PER_MINUTE = 300
