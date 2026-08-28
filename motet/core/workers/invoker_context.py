"""
Motet - Invoker Context and Identity Resolution

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Worker-local context management and principal identity resolution for
    distributed command execution.  Provides ContextVar-backed storage for
    worker context, command ID tracking, and immutable identity propagation
    across nested command composition.

    Key types and helpers:
    - IdentityContext: Frozen dataclass carrying tenant_id, motet_id,
    principal_id through nested command chains.
    - resolve_current_identity: Canonical identity resolution helper used by builtin tools and bundle @motet.tool functions.
    - InvokerContextManager: Context manager for scoped worker context
    setup/teardown during command execution.

Dependencies:
    - contextvars: ContextVar for worker context, command ID, and identity
    - dataclasses: Frozen IdentityContext for immutable identity propagation
    - motet.core.commands.decorator: MotetContext access
      (lazy import to avoid circular dependency)

Usage:
    from motet.core.workers.invoker_context import (
        IdentityContext,
        resolve_current_identity,
        set_worker_context,
        get_worker_context,
    )

    # Resolve identity inside a tool function (ADR-0090)
    identity = resolve_current_identity()
    # identity.tenant_id, identity.motet_id, identity.principal_id

    # Propagate identity to nested commands
    set_current_identity_context(IdentityContext(
        tenant_id="t1", motet_id="m1", principal_id="u1",
    ))

Notes:
    - resolve_current_identity is the canonical entry point for identity
      resolution in builtin tools and bundle tools.  It is re-exported
      from motet.core.commands.decorator for backward
      compatibility and exposed in the SDK (motet_sdk.command).
    - IdentityContext is intentionally a frozen dataclass (not Pydantic)
      to keep it lightweight with no runtime dependencies beyond stdlib.
"""


import asyncio
from dataclasses import dataclass
from typing import Optional, Dict, Any, Mapping
from contextvars import ContextVar

# Context variable to store the current worker context
_current_worker_context: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    'current_worker_context', 
    default=None
)

# Context variable to store the currently executing command ID for parent tracking
_current_command_id: ContextVar[Optional[str]] = ContextVar(
    'current_command_id',
    default=None
)

@dataclass(frozen=True)
class IdentityContext:
    """Immutable identity context used for nested command composition."""

    tenant_id: str
    motet_id: str
    principal_id: str


# Immutable parent identity context for nested command composition
_current_identity_context: ContextVar[Optional[IdentityContext]] = ContextVar(
    'current_identity_context',
    default=None
)

def set_worker_context(worker_context: Dict[str, Any]) -> None:
    """Set the current worker context for this execution context."""
    _current_worker_context.set(worker_context)

def get_worker_context() -> Optional[Dict[str, Any]]:
    """Get the current worker context if available."""
    return _current_worker_context.get()

def set_current_command_id(command_id: str) -> None:
    """Set the currently executing command ID for parent tracking."""
    _current_command_id.set(command_id)

def get_current_command_id() -> Optional[str]:
    """Get the currently executing command ID if available."""
    return _current_command_id.get()

def clear_current_command_id() -> None:
    """Clear the currently executing command ID."""
    _current_command_id.set(None)


def set_current_identity_context(identity_context: Mapping[str, Any] | IdentityContext) -> None:
    """
    Set immutable identity context for nested command creation.

    Only tenant_id/motet_id/principal_id are retained and normalized to strings.
    """
    if isinstance(identity_context, IdentityContext):
        normalized = identity_context
    else:
        normalized = IdentityContext(
            tenant_id=str((identity_context or {}).get("tenant_id") or "").strip(),
            motet_id=str((identity_context or {}).get("motet_id") or "").strip(),
            principal_id=str((identity_context or {}).get("principal_id") or "").strip(),
        )
    _current_identity_context.set(normalized)


def get_current_identity_context() -> Optional[IdentityContext]:
    """Get the current immutable identity context if available."""
    return _current_identity_context.get()


def clear_current_identity_context() -> None:
    """Clear immutable identity context."""
    _current_identity_context.set(None)


def resolve_current_identity(
    *,
    system_defaults: Optional[IdentityContext] = None,
) -> IdentityContext:
    """
    Resolve identity from MotetContext or invoker identity context (ADR-0090).

    Resolution order:
    1. MotetContext (command-local WorkerLocal) — extract identity fields.
    2. IdentityContext (invoker-propagated ContextVar) — return directly.
    3. If *system_defaults* is provided, return those defaults.
    4. Otherwise raise ValueError — missing identity is a bug, not a default.

    Returns an ``IdentityContext`` (frozen dataclass with tenant_id, motet_id,
    principal_id).

    User-scoped tools call without arguments and get a hard failure when identity
    is missing.  System-scoped tools pass ``system_defaults`` to opt in to
    fallback identity.
    """
    try:
        from motet.core.commands.decorator import get_motet_context
        ctx = get_motet_context()
        pid = str(getattr(ctx, "principal_id", "") or "").strip()
        tid = str(getattr(ctx, "tenant_id", "") or "").strip()
        mid = str(getattr(ctx, "motet_id", "") or "").strip()
        if pid and tid and mid:
            return IdentityContext(tenant_id=tid, motet_id=mid, principal_id=pid)
    except Exception:
        pass  # context unavailable; fall through to invoker

    invoker = get_current_identity_context()
    if invoker is not None and invoker.principal_id:
        return invoker

    if system_defaults is not None:
        return system_defaults

    raise ValueError(
        "No identity context available. User-scoped tools require identity "
        "propagation via MotetContext or invoker identity context. "
        "System-scoped tools should pass system_defaults."
    )


def get_distributed_invoker():
    """
    Get the appropriate distributed invoker for the current context.
    
    Returns worker-specific invoker if set in the worker context; otherwise
    returns the process-wide global invoker.
    """
    # Check if we're in a worker context
    worker_context = get_worker_context()
    if worker_context and worker_context.get("distributed_invoker"):
        print("🔄 Using worker-specific distributed invoker")
        return worker_context["distributed_invoker"]

    # Fall back to global invoker (alias of new_global_invoker)
    print("🌐 Using global distributed invoker")
    from . import global_invoker
    return global_invoker


class InvokerContextManager:
    """Context manager for setting worker context in async functions."""
    
    def __init__(self, worker_context: Dict[str, Any]):
        self.worker_context = worker_context
        self.token = None
    
    def __enter__(self):
        self.token = _current_worker_context.set(self.worker_context)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.token:
            _current_worker_context.reset(self.token)

def with_worker_context(worker_context: Dict[str, Any]):
    """Decorator to set worker context for a function."""
    def decorator(func):
        async def wrapper(*args, **kwargs):
            with InvokerContextManager(worker_context):
                return await func(*args, **kwargs)
        return wrapper
    return decorator

__all__ = [
    "IdentityContext",
    "set_worker_context",
    "get_worker_context", 
    "set_current_command_id",
    "get_current_command_id",
    "clear_current_command_id",
    "set_current_identity_context",
    "get_current_identity_context",
    "clear_current_identity_context",
    "resolve_current_identity",
    "get_distributed_invoker",
    "InvokerContextManager",
    "with_worker_context"
]
