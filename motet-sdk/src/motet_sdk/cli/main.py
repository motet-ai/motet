"""
Motet - CLI Main Entry Point

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Main entry point for the Motet CLI.
    Registers all CLI groups and provides a unified interface.

Dependencies:
    - click: CLI framework
    - All CLI modules (commands, chat, models, tools, memories, etc.)

Usage:
    motet-cli version                      # Stack Motet versions (API + workers + siblings)
    motet-cli command list                 # Command management (list/info/run)
    motet-cli chat --message "Hello"       # Chat with AI
    motet-cli models --provider openai     # List models
    motet-cli tools call --name <tool>     # Execute tools
    motet-cli skills list                  # List installed Agent Skills
    motet-cli memories inspect             # Inspect memories
    motet-cli memories store --content ".." # Store memory via API
    motet-cli traces list                  # List traces
    motet-cli database migrate-pgvector    # Database operations

Notes:
    - Unified CLI structure aligned with API organization
    - All operations are grouped by domain
    - Product CLIs (e.g. app-builder) are separate packages — see
      docs/developer_onboarding/39-extending-the-cli.md
"""

import click

from motet_sdk._version import get_version

# Configure logging first (same order as cli_entry)
from . import _logging  # noqa: F401 - configure logging before other imports

# Import all CLI groups using relative imports within motet_sdk.cli
from .commands import commands_group
from .deploy import deploy_group
from .bundle import bundle_group
from .chat import chat_command
from .models import models_command
from .tools import tools_group
from .skills import skills_group
from .memories import memories_group
from .traces import traces_group
from .database import database_group
from .service_accounts import service_account_group
from .artifacts import artifacts_group
from .schedules import schedules_group
from .vault import vault_group
from .workers import workers_group
from .workflows import workflows_group
from .conversations import conversations_group
from .events import events_group
from .identity import identity_group
from .cost import cost_group
from .auth import auth_group
from .setup import setup_group
from .debug import debug_group
from .local import local_group
from .agents import agents_group
from .device import device_group
from .tenants import tenants_group
from .surfaces import surfaces_group
from .tasks import tasks_group
from .version import version_command


@click.group("motet-cli")
@click.version_option(version=get_version(), prog_name="motet-cli")
def main_group() -> None:
    """Motet - Distributed AI Command & Control."""
    pass


# Register all CLI groups and commands
main_group.add_command(commands_group)
main_group.add_command(deploy_group)
main_group.add_command(bundle_group)
main_group.add_command(chat_command)
main_group.add_command(models_command)
main_group.add_command(tools_group)
main_group.add_command(skills_group)
main_group.add_command(memories_group)
main_group.add_command(traces_group)
main_group.add_command(database_group)
main_group.add_command(service_account_group)
main_group.add_command(artifacts_group)
main_group.add_command(schedules_group)
main_group.add_command(vault_group)
main_group.add_command(workers_group)
main_group.add_command(workflows_group)
main_group.add_command(conversations_group)
main_group.add_command(events_group)
main_group.add_command(identity_group)
main_group.add_command(cost_group)
main_group.add_command(auth_group)
main_group.add_command(setup_group)
main_group.add_command(debug_group)
main_group.add_command(local_group)
main_group.add_command(agents_group)
main_group.add_command(device_group)
main_group.add_command(tenants_group)
main_group.add_command(surfaces_group)
main_group.add_command(tasks_group)
main_group.add_command(version_command)


if __name__ == "__main__":
    main_group()

