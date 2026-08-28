"""
Motet - CLI Package Re-exports

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Runtime package-level re-exports for CLI groups implemented in motet_sdk.cli.
    Re-exports CLI groups so ``from motet.cli import main_group`` keeps working.

Dependencies:
    - motet_sdk.cli.*: source CLI command groups and entrypoint

Usage:
    from motet.cli import main_group
    from motet.cli import local_group, device_group
"""

def __getattr__(name: str):
    if name == "main_group" or name == "main":
        from motet_sdk.cli import main_group, main
        return main_group if name == "main_group" else main
    if name == "commands_group":
        from motet_sdk.cli.commands import commands_group
        return commands_group
    if name == "deploy_group":
        from motet_sdk.cli.deploy import deploy_group
        return deploy_group
    if name == "bundle_group":
        from motet_sdk.cli.bundle import bundle_group
        return bundle_group
    if name == "chat_command":
        from motet_sdk.cli.chat import chat_command
        return chat_command
    if name == "models_command":
        from motet_sdk.cli.models import models_command
        return models_command
    if name == "tools_group":
        from motet_sdk.cli.tools import tools_group
        return tools_group
    if name == "memories_group":
        from motet_sdk.cli.memories import memories_group
        return memories_group
    if name == "traces_group":
        from motet_sdk.cli.traces import traces_group
        return traces_group
    if name == "database_group":
        from motet_sdk.cli.database import database_group
        return database_group
    if name == "service_account_group":
        from motet_sdk.cli.service_accounts import service_account_group
        return service_account_group
    if name == "artifacts_group":
        from motet_sdk.cli.artifacts import artifacts_group
        return artifacts_group
    if name == "schedules_group":
        from motet_sdk.cli.schedules import schedules_group
        return schedules_group
    if name == "vault_group":
        from motet_sdk.cli.vault import vault_group
        return vault_group
    if name == "workers_group":
        from motet_sdk.cli.workers import workers_group
        return workers_group
    if name == "workflows_group":
        from motet_sdk.cli.workflows import workflows_group
        return workflows_group
    if name == "conversations_group":
        from motet_sdk.cli.conversations import conversations_group
        return conversations_group
    if name == "events_group":
        from motet_sdk.cli.events import events_group
        return events_group
    if name == "identity_group":
        from motet_sdk.cli.identity import identity_group
        return identity_group
    if name == "cost_group":
        from motet_sdk.cli.cost import cost_group
        return cost_group
    if name == "auth_group":
        from motet_sdk.cli.auth import auth_group
        return auth_group
    if name == "setup_group":
        from motet_sdk.cli.setup import setup_group
        return setup_group
    if name == "debug_group":
        from motet_sdk.cli.debug import debug_group
        return debug_group
    if name == "local_group":
        from motet_sdk.cli.local import local_group
        return local_group
    if name == "agents_group":
        from motet_sdk.cli.agents import agents_group
        return agents_group
    if name == "device_group":
        from motet_sdk.cli.device import device_group
        return device_group
    if name == "tasks_group":
        from motet_sdk.cli.tasks import tasks_group
        return tasks_group
    if name == "version_command":
        from motet_sdk.cli.version import version_command
        return version_command
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "main_group",
    "main",
    "commands_group",
    "deploy_group",
    "bundle_group",
    "chat_command",
    "models_command",
    "tools_group",
    "memories_group",
    "traces_group",
    "database_group",
    "schedules_group",
    "vault_group",
    "workers_group",
    "workflows_group",
    "conversations_group",
    "events_group",
    "identity_group",
    "cost_group",
    "auth_group",
    "setup_group",
    "debug_group",
    "local_group",
    "agents_group",
    "device_group",
    "tasks_group",
    "version_command",
]
