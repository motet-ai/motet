"""
Motet - Built-in Command Library

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    The commands that ship with Motet: memory, model inference, tool execution,
    RAG, artifacts, conversations, derivation, scheduling, agents,
    workflows, transforms, and worker lifecycle.

    These sit beside the framework rather than inside the domain packages they
    serve because they reach their domains through the injected context
    (`motet.memory.recall(...)`) rather than through imports — scattering them
    into `core/memory/`, `core/tools/`, and friends would collapse no
    dependency edge. Keeping them here also means `MotetContext`, which
    dispatches to them, does so within one package instead of reaching back
    into a separate library.

Dependencies:
    - motet.core.commands: the framework these are written against
    - the domain packages each command fronts (memory, tools, artifacts, ...)

Usage:
    Import the module you need; this package deliberately re-exports nothing.

        from motet.core.commands.builtin.memory import memory_store
        from motet.core.commands.builtin.tool import tool_execution

Notes:
    - Re-exports nothing on purpose. An eager `__init__` here would make the
      cheapest command as expensive as the most expensive one, which is the
      trap the old `orchestration/commands/__init__.py` fell into.
    - Registration is explicit. `@motet.command` registers on import, and
      the imports live in `DistributedCommand._ensure_commands_registered()`.
      A new module here that is not added to that list is an unregistered
      command: workers reject it with "Unknown command type" at runtime, and no
      unit test will catch it.
"""
