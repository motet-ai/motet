"""
Motet - Logging Configuration

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Logging configuration system for the Motet distributed framework.
    Provides structured logging with JSON output and context management.

Dependencies:
    - structlog: Structured logging framework
    - logging: Standard Python logging
    - contextvars: Context variable management

Usage:
    from motet.core.observability.logging import setup_logging
    
    # Setup logging
    setup_logging("INFO")

Notes:
    - Provides structured JSON logging
    - Includes context variable support
    - Supports log level configuration via environment
    - Integrates with distributed architecture
"""

from __future__ import annotations

import logging
import os

import structlog
from structlog.contextvars import merge_contextvars


def setup_logging(log_level: str | None = None) -> None:
    level_name = (log_level or os.getenv("MOTET_LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(format="%(message)s", level=level)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            merge_contextvars,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


__all__ = ["setup_logging"]


