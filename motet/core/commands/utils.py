"""
Motet - Command Utilities

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Command utilities for the Motet distributed framework.
    Provides comprehensive command utility functions including tool
    observation formatting, result processing, and command coordination.
    Includes centralized observation formatting and distributed command
    management for distributed systems.

Dependencies:
    - typing: Type hints and annotations
    - Tool registry and command system

Usage:
    from motet.core.commands.utils import format_tool_observation_text

    # Format tool observation
    formatted = format_tool_observation_text("web_search", {
        "status": "success",
        "result": "Search completed"
    })

Notes:
    - Provides comprehensive command utility functions
    - Includes tool observation formatting and result processing
    - Supports centralized observation formatting
    - Includes distributed command management
    - Supports comprehensive error handling and logging
    - Integrates with tool registry and command system
    - Includes comprehensive observability and monitoring
"""

from __future__ import annotations

from typing import Any, Dict, Tuple


def format_tool_observation_text(tool_name: str, result: Dict[str, Any]) -> str:
    """Central observation formatter used by tool_execution and callers.
    Prefers registry formatter; falls back to compact status/result/error summary.
    """
    try:
        from motet.core.tools import registry as tools
        formatted = tools.format_observation(tool_name, result)
        if formatted:
            return formatted
    except Exception:
        pass  # formatting is best-effort; fallback to compact summary
    if isinstance(result, dict):
        if "status" in result:
            return f"{tool_name}(status={result['status']})"
        if "result" in result:
            return f"{tool_name}(result={result['result']})"
        if "error" in result:
            return f"{tool_name}(error={result['error']})"
    return f"{tool_name}(ok)"

def require_context_field(
    context: Any,
    *,
    field_name: str,
    operation: str,
    context_name: str = "MotetContext",
    error_template: str = "{operation} requires {field} in {context_name}",
) -> str:
    """Require one non-empty identity field from context and return it."""
    value = str(getattr(context, field_name, "") or "").strip() if context is not None else ""
    if value:
        return value
    raise ValueError(
        error_template.format(
            operation=operation,
            field=field_name,
            context_name=context_name,
        )
    )


def require_context_identity(
    context: Any,
    *,
    operation: str,
    context_name: str = "MotetContext",
    fields: Tuple[str, ...] = ("motet_id", "tenant_id", "principal_id"),
    error_template: str = "{operation} requires {field} in {context_name}",
) -> Tuple[str, ...]:
    """Require non-empty identity fields from context and return values in order."""
    return tuple(
        require_context_field(
            context,
            field_name=field_name,
            operation=operation,
            context_name=context_name,
            error_template=error_template,
        )
        for field_name in fields
    )


__all__ = [
    "format_tool_observation_text",
    "require_context_field",
    "require_context_identity",
]


