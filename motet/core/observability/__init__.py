"""
Motet - Observability Module

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Observability system for the Motet distributed framework.
    Provides logging, metrics, tracing, and monitoring capabilities.

Dependencies:
    - structlog: Structured logging
    - prometheus_client: Metrics collection
    - opentelemetry: Distributed tracing
    - FastAPI: Web framework integration

Usage:
    from motet.core.observability import setup_logging, setup_tracing
    
    # Setup logging
    setup_logging("INFO")
    
    # Setup tracing
    setup_tracing()

Notes:
    - Provides structured logging with context
    - Includes comprehensive metrics collection
    - Supports distributed tracing
    - Integrates with monitoring systems
"""

from .logging import setup_logging
from .metrics import *  # noqa: F401,F403
from .tracing import setup_tracing, get_tracer, get_captured_spans

__all__ = [
    "setup_logging",
    "setup_tracing",
    "get_tracer",
    "get_captured_spans",
]


