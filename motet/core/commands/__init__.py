"""
Motet - Commands

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    The command framework and the built-in command library.

    This module exports only the framework vocabulary — the `Command` base and
    its context/status types, `WorkerCapability`, `BaseCommandData`, the
    response models, and the two registries. Everything heavier is a
    submodule you import directly:

        decorator.py            @distributed_command / @motet.command, MotetContext
        distributed.py          DistributedCommand base, routing, serialization
        command_data_classes.py payload models for the built-in commands
        concurrency.py          gather / dispatch / map primitives
        builtin/                the built-in command library

    Reasoning, tools, memory, artifacts, and workers all write commands and
    import the framework vocabulary from this package. They are peers that
    share a framework, not dependents of orchestration.

    The built-in commands live here rather than in the domain packages they
    serve because they reach those domains through the injected context, not
    through imports — scattering them would collapse no dependency edge — and
    because `MotetContext` dispatches to them, which stays an internal edge
    while they share a package with the decorator that defines it.

Dependencies:
    - motet.core.registry: ScopedRegistry backing both command registries
    - motet.core.workers.concurrency_primitives: pool-agnostic locks for registry mutation
    - motet.core.types: canonical Message type referenced by BaseCommandData
    - pydantic: model definitions and validation

Usage:
    from motet.core.commands import (
        Command, CommandContext, CommandStatus,
        WorkerCapability, BaseCommandData,
        CommandExecutionError,
    )

    # Declaring what a command needs from its worker
    @motet.command(required_capabilities=[WorkerCapability.TOOL_EXECUTION])
    def my_command(data: MyData, motet: MotetContext) -> Dict[str, Any]:
        ...

Notes:
    - This module's own exports stay leaf-level: importing `motet.core.commands`
      pulls in no orchestration, no reasoning, and no built-in command. Keep it
      that way so any layer can depend on the vocabulary without a cycle, and
      import `decorator` / `distributed` / `builtin.*` explicitly when the
      heavier machinery is actually needed.
    - Registration is explicit. `@distributed_command` registers a command when
      its module is imported, and those imports live in
      `DistributedCommand._ensure_commands_registered`. This package `__init__`
      does not import every sibling as a backstop, so a command module missing
      from that list is unregistered: workers reject it at runtime with
      "Unknown command type" and no unit test catches it.
"""

from .base import Command, CommandContext, CommandStatus
from .base_command_data import BaseCommandData, MessageFieldMixin
from .capabilities import WorkerCapability
from .command_data_registry import (
    command_data_registry,
    get_all_command_data_classes,
    get_command_data_class,
    get_command_types,
    register_command_data,
)
from .command_type_registry import (
    CommandImplementationType,
    CommandRegistration,
    command_type_registry,
    get_all_command_types,
    get_command_registration,
    is_command_registered,
    register_command_type,
)
from .response_models import (
    ApplyExecutionError,
    CommandError,
    CommandExecutionError,
    CommandMetadata,
    GatherExecutionError,
)

__all__ = [
    # base
    "Command",
    "CommandContext",
    "CommandStatus",
    # capabilities
    "WorkerCapability",
    # command data
    "BaseCommandData",
    "MessageFieldMixin",
    # response models
    "CommandError",
    "CommandMetadata",
    "CommandExecutionError",
    "GatherExecutionError",
    "ApplyExecutionError",
    # command data registry
    "command_data_registry",
    "register_command_data",
    "get_command_data_class",
    "get_all_command_data_classes",
    "get_command_types",
    # command type registry
    "command_type_registry",
    "CommandImplementationType",
    "CommandRegistration",
    "register_command_type",
    "get_command_registration",
    "is_command_registered",
    "get_all_command_types",
]
