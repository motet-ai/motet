"""
Motet - Turn Hook Registry Resolution

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Every TurnHooks slot resolves through command_type_registry. None skips
    the phase. An unregistered name warns and skips, except finalize: that
    slot is the turn's commit step, so an unknown name falls back to
    core.finalize_turn and logs an error.

    Load-time validation (YAML parse / bundle deploy) is the primary guard:
    an unknown name fails before any turn runs. Motet default command names
    are always accepted so builtin agents can register before those modules
    are imported. Turn-time warn-and-skip is the backstop for registry drift.

    List-hook payloads are the declared TurnContextHookData /
    TurnAfterFinalizeData models. A data-model mismatch at resolve is a
    configuration bug and fails loudly (error log, hook skipped). A hook
    that resolves and then fails at runtime stays fail-soft (motet.maybe).

Dependencies:
    - motet.core.commands.command_type_registry: name → implementation
    - motet.core.commands.command_data_classes: create_command_data
    - structlog: warn / error for skip vs finalize fallback

Usage:
    from motet.core.orchestration.turn.hook_resolve import (
        DEFAULT_FINALIZE_COMMAND,
        collect_turn_hook_names,
        instantiate_hook_data,
        resolve_hook_implementation,
        validate_turn_hooks,
    )

    impl = resolve_hook_implementation(name, slot="finalize")
    hook_data = instantiate_hook_data(name, payload)

Notes:
    - Motet defaults stay as values on AgentConfig / YAML, not equality
      checks in hooks.py.
    - context_inject remains additive; this module only resolves and
      instantiates the payload.
"""

from __future__ import annotations

from typing import Any, Iterable, List, Optional

import structlog
from pydantic import BaseModel, ValidationError

from motet.core.commands.command_data_registry import command_data_registry
from motet.core.commands.command_type_registry import command_type_registry

logger = structlog.get_logger(__name__)

DEFAULT_FINALIZE_COMMAND = "core.finalize_turn"

# Known Motet slot defaults. Accepted at load even when those command
# modules have not been imported yet (builtin agent registration).
MOTE_DEFAULT_HOOK_COMMANDS = frozenset(
    {
        "core.conversation_analysis",
        "core.memory_reset",
        "core.prepare_context",
        DEFAULT_FINALIZE_COMMAND,
        "core.page_context",
    }
)

_SINGLE_SLOTS = (
    "conversation_analysis",
    "memory_reset",
    "context_prepare",
    "finalize",
)
_LIST_SLOTS = ("context_inject", "after_finalize")


class HookPayloadError(ValueError):
    """The hook's registered data class rejected the declared payload."""


def collect_turn_hook_names(turn_hooks: Any) -> List[str]:
    """All configured command names on a TurnHooks instance (order preserved)."""
    names: List[str] = []
    if turn_hooks is None:
        return names
    for slot in _SINGLE_SLOTS:
        value = getattr(turn_hooks, slot, None)
        if isinstance(value, str) and value.strip():
            names.append(value.strip())
    for slot in _LIST_SLOTS:
        values = getattr(turn_hooks, slot, None) or []
        for value in values:
            if isinstance(value, str) and value.strip():
                names.append(value.strip())
    return names


def _registry_has_commands() -> bool:
    try:
        regs = command_type_registry.get_all_registrations()
        return bool(regs)
    except Exception:
        return False


def _is_registered(name: str) -> bool:
    if name in MOTE_DEFAULT_HOOK_COMMANDS:
        return True
    if not _registry_has_commands():
        return True
    registration = command_type_registry.get(name)
    return bool(registration and callable(getattr(registration, "implementation", None)))


def validate_turn_hooks(turn_hooks: Any, *, require_registered: bool = True) -> None:
    """Fail if any configured hook name is unknown.

    Motet defaults always pass. Other names fail when the command registry
    is populated and the name is missing. ``require_registered=False`` is
    the import-time builtin path (registry may still be empty).
    """
    if turn_hooks is None or not require_registered:
        return
    unknown = [
        name
        for name in collect_turn_hook_names(turn_hooks)
        if not _is_registered(name)
    ]
    if unknown:
        raise ValueError(
            "Unknown turn hook command(s): "
            + ", ".join(unknown)
            + ". Set the field to a registered command name or None to skip."
        )


def resolve_hook_implementation(
    name: Optional[str],
    *,
    slot: str,
) -> Optional[Any]:
    """Look up a hook command. None skips. Unknown warns, or finalize falls back."""
    if not name or not str(name).strip():
        return None
    hook_name = str(name).strip()
    registration = command_type_registry.get(hook_name)
    impl = getattr(registration, "implementation", None) if registration else None
    if callable(impl):
        return impl
    if slot == "finalize":
        logger.error(
            "turn_hook_unregistered_finalize_fallback",
            slot=slot,
            hook=hook_name,
            fallback=DEFAULT_FINALIZE_COMMAND,
        )
        fallback = command_type_registry.get(DEFAULT_FINALIZE_COMMAND)
        fallback_impl = getattr(fallback, "implementation", None) if fallback else None
        return fallback_impl if callable(fallback_impl) else None
    logger.warning(
        "turn_hook_unregistered_skip",
        slot=slot,
        hook=hook_name,
    )
    return None


def instantiate_hook_data(command_type: str, payload: BaseModel) -> Any:
    """Build the hook's registered data class from the declared payload.

    A validation failure is a configuration bug: raise HookPayloadError
    instead of trying alternate kwargs.
    """
    from motet.core.commands.command_data_classes import create_command_data

    dump = payload.model_dump() if hasattr(payload, "model_dump") else dict(payload)
    data_class = command_data_registry.get(command_type)
    try:
        if data_class is not None:
            return data_class.model_validate(dump)
        return create_command_data(command_type, **dump)
    except ValidationError as exc:
        raise HookPayloadError(
            f"Hook {command_type} data model rejected the declared payload: {exc}"
        ) from exc
    except Exception as exc:
        raise HookPayloadError(
            f"Hook {command_type} data model rejected the declared payload: {exc}"
        ) from exc


def validate_bundle_agent_hooks(agent_ids: Iterable[str]) -> None:
    """Re-check hook names after a bundle's commands have been registered."""
    from motet.core.agents import get_agent_registry

    registry = get_agent_registry()
    for qualified_id in agent_ids:
        config = registry.get(qualified_id)
        if config is None:
            continue
        validate_turn_hooks(getattr(config, "turn_hooks", None), require_registered=True)


__all__ = [
    "DEFAULT_FINALIZE_COMMAND",
    "HookPayloadError",
    "MOTE_DEFAULT_HOOK_COMMANDS",
    "collect_turn_hook_names",
    "instantiate_hook_data",
    "resolve_hook_implementation",
    "validate_bundle_agent_hooks",
    "validate_turn_hooks",
]
