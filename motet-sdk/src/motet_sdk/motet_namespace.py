"""
Motet SDK - Motet decorator namespace.

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Bundle authors use @motet.command and @motet.tool. When the bundle runs
inside the Motet runtime, the runtime replaces this with the real
namespace. When developing or testing locally, command is a no-op and
tool is a no-op so unit tests work.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from motet_sdk.command import distributed_command


def _tool_noop(
    description: str,
    name: Optional[str] = None,
    *,
    category: str = "general",
    **kwargs: Any,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """No-op tool decorator for SDK (no registry when not in runtime)."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        return fn

    return decorator


class _MotetNamespace:
    """
    Namespace for Motet decorators.
    In SDK: no-op decorators for tests. In runtime: real @motet.command, @motet.tool.
    Set on instance so @motet.command(...) does not bind self as first argument.
    """

    command: Any
    tool: Any


motet = _MotetNamespace()
motet.command = distributed_command
motet.tool = _tool_noop
