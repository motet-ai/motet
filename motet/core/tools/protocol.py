"""
Motet - Tool Protocol Utilities

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-23

Description:
    Tool protocol utilities for the Motet distributed framework.
    Provides standardized tool response formatting and status management
    for consistent tool execution across the distributed system. Includes
    success, error, and timeout status handling. ``ok`` accepts optional
    ``cache_control`` (``no-store`` / ``same-turn`` / ``max-age=N``).

Dependencies:
    - typing: Type hints and annotations

Usage:
    from motet.core.tools.protocol import ok, err, ToolStatusSuccess

    # Success response
    result = ok({"data": "value"}, meta={"execution_time": 150})
    result = ok({"data": "value"}, cache_control="same-turn")

    # Error response
    error = err("Tool execution failed", meta={"error_code": "TIMEOUT"})

Notes:
    - Provides standardized tool response formatting
    - Includes success, error, and timeout status handling
    - Supports metadata and result data management
    - Ensures consistent tool execution across distributed system
    - Includes comprehensive error handling and logging
    - Integrates with tool registry and execution system
    - Supports distributed tool coordination
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .cache_control import parse_cache_control


ToolStatusSuccess = "success"
ToolStatusError = "error"
ToolStatusTimeout = "timeout"


def ok(
    result: Any = None,
    meta: Optional[Dict[str, Any]] = None,
    cache_control: Any = None,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {"status": ToolStatusSuccess}
    if result is not None:
        out["result"] = result
    if meta:
        out["meta"] = meta
    if cache_control is not None:
        out["cache_control"] = parse_cache_control(cache_control).to_wire()
    return out


def err(message: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"status": ToolStatusError, "error": message}
    if meta:
        out["meta"] = meta
    return out


__all__ = [
    "ok",
    "err",
    "ToolStatusSuccess",
    "ToolStatusError",
    "ToolStatusTimeout",
]


