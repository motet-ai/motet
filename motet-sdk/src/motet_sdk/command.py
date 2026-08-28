"""
Motet SDK - Distributed command decorator (interface only).

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Bundle authors use @distributed_command to declare commands. When the bundle
runs inside the Motet runtime, the runtime replaces this with the real
decorator. When developing or testing locally, this no-op preserves the
function for unit tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, List, Optional

from motet_sdk.capabilities import WorkerCapability

if TYPE_CHECKING:
    from motet_sdk.models import IdentityContext


def distributed_command(
    timeout_seconds: Optional[int] = None,
    priority: Optional[int] = None,
    required_capabilities: Optional[List[WorkerCapability]] = None,
    capability_inference: Optional[Callable[[Any], List[WorkerCapability]]] = None,
    streaming_enabled: bool = False,
    stream_key: Optional[str] = None,
    can_undo: bool = False,
    preferred_pool_type: Optional[Any] = None,
    description: Optional[str] = None,
    namespace: Optional[str] = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Decorator for defining distributed commands (SDK interface).

    Use this on command functions that take (data: YourData, motet: MotetContext).
    When the bundle runs in the Motet runtime, the runtime applies the real
    decorator; when developing or testing without a runtime, this no-op leaves
    the function unchanged so you can call it directly or with MockMotetContext.

    Args:
        timeout_seconds: Command timeout in seconds.
        priority: Execution priority.
        required_capabilities: Worker capabilities required for this command.
        capability_inference: Optional function(data) -> list of capabilities.
        streaming_enabled: Enable streaming support.
        stream_key: Stream key pattern when streaming.
        can_undo: Whether the command supports undo.
        preferred_pool_type: Preferred worker pool type.
        description: Optional help/discovery summary. In the runtime, defaults to
            the first line of the function docstring when omitted.
        namespace: Explicit namespace prefix for command type.

    Returns:
        Decorated function (identity in SDK; real binding in runtime).
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        return fn

    return decorator


def get_motet_context() -> Optional[Any]:
    """
    Return the current MotetContext when running inside the Motet runtime.

    In the SDK (e.g. unit tests or local script), there is no runtime context;
    this returns None. When the bundle runs in a worker, the runtime provides
    the real context. Prefer passing motet as the second parameter to your
    command function instead of calling this.
    """
    return None


def resolve_current_identity(
    *,
    system_defaults: Optional["IdentityContext"] = None,
) -> "IdentityContext":
    """
    Resolve the current principal identity (ADR-0090).

    Returns an ``IdentityContext`` (frozen dataclass with ``tenant_id``,
    ``motet_id``, ``principal_id``) resolved from the ambient execution
    context.

    Resolution order (in the runtime):
    1. MotetContext (command-local) — extract identity fields.
    2. IdentityContext (invoker-propagated ContextVar).
    3. If *system_defaults* is provided, return those defaults.
    4. Otherwise raise ValueError.

    In the SDK (tests/local), this always raises ValueError since there is
    no runtime context. Use MockMotetContext for testing commands, or pass
    system_defaults for system-scoped tools.
    """
    if system_defaults is not None:
        return system_defaults
    raise ValueError(
        "resolve_current_identity: no runtime context available. "
        "In tests, pass system_defaults or use MockMotetContext."
    )
