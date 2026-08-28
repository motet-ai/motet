"""
Motet - Commands CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-09

Description:
    CLI for the commands API (/api/v1/commands): list, info, run. One-to-one
    with the commands API. Commands are always deployed via bundles; use
    motet-cli deploy (cli/deploy.py) for bundle deployment. List/info surface
    discovery ``description`` and Pydantic ``data_schema`` from the API.

Dependencies:
    - click: CLI framework
    - requests: API communication

Usage:
    motet-cli command list                 # GET /api/v1/commands
    motet-cli command info <command_type>  # GET /api/v1/commands/{command_type}
    motet-cli command run <command_type>   # POST /api/v1/commands/{command_type}/execute
"""

import json
from typing import Any, Optional

import click
import requests

from ._api import api_request, api_url_option
from ._auth import get_api_headers

_LIST_DESCRIPTION_MAX = 120


def _format_description(description: Any, *, max_len: Optional[int] = None) -> str:
    text = str(description or "").strip()
    if not text:
        return ""
    if max_len is not None and len(text) > max_len:
        return text[: max_len - 1].rstrip() + "…"
    return text


@click.group("command")
def commands_group() -> None:
    """Commands API: list, info, run (commands are deployed via bundles)."""
    pass


@commands_group.command("list")
@click.option("--bundle-id", help="Filter by bundle_id (manifest name)")
@click.option(
    "--implementation-type",
    type=click.Choice(["CLASS_BASED", "DECORATOR_BASED", "BUNDLE"]),
    help="Filter by implementation type",
)
@api_url_option()
def list_commands(
    bundle_id: Optional[str],
    implementation_type: Optional[str],
    api_url: str
) -> None:
    """List registered command types (core + bundle catalog)."""
    try:
        params = {}
        if bundle_id:
            params["bundle_id"] = bundle_id
        if implementation_type:
            params["implementation_type"] = implementation_type
        url = f"{api_url.rstrip('/')}/api/v1/commands"
        headers = get_api_headers()
        response = api_request("GET", url, headers=headers, params=params, timeout=30)
        result = response.json()
        commands = result.get("commands", [])
        if not commands:
            click.echo("📭 No commands found")
            return
        click.echo(f"📋 Found {len(commands)} command(s):\n")
        for cmd in commands:
            cmd_type = cmd.get("command_type", "N/A")
            version = cmd.get("version", "N/A")
            bundle = cmd.get("bundle_id", "N/A")
            motet_compat = cmd.get("motet_compatibility", [])
            description = _format_description(
                cmd.get("description"), max_len=_LIST_DESCRIPTION_MAX
            )
            has_schema = isinstance(cmd.get("data_schema"), dict)
            click.echo(f"  • {cmd_type}")
            if description:
                click.echo(f"    Description: {description}")
            click.echo(f"    Schema: {'yes' if has_schema else 'no'}")
            click.echo(f"    Version: {version}")
            click.echo(f"    Bundle: {bundle}")
            if motet_compat:
                click.echo(f"    Motets: {', '.join(motet_compat)}")
            else:
                click.echo("    Motets: All (no restrictions)")
            click.echo()
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo(f"\n❌ Could not connect to API at {api_url}", err=True)
        click.echo("💡 Make sure the API server is running", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"❌ Error listing commands: {e}", err=True)
        raise click.Abort()


@commands_group.command("info")
@click.argument("command_name")
@api_url_option()
def info_command(command_name: str, api_url: str) -> None:
    """Get details for a registered command type (core or bundle)."""
    try:
        url = f"{api_url.rstrip('/')}/api/v1/commands/{command_name}"
        headers = get_api_headers()
        response = api_request("GET", url, headers=headers, timeout=30)
        result = response.json()
        click.echo(f"📋 Command: {command_name}\n")
        description = _format_description(result.get("description"))
        if description:
            click.echo(f"  Description: {description}")
        else:
            click.echo("  Description: —")
        click.echo(f"  Version: {result.get('version', 'N/A')}")
        click.echo(f"  Bundle: {result.get('bundle_id', 'N/A')}")
        motet_compat = result.get('motet_compatibility', [])
        if motet_compat:
            click.echo(f"  Motets: {', '.join(motet_compat)}")
        else:
            click.echo("  Motets: All (no restrictions)")
        cmd_metadata = result.get('metadata', {})
        if cmd_metadata:
            capabilities = cmd_metadata.get('capabilities', [])
            if capabilities:
                click.echo(f"  Capabilities: {', '.join(capabilities)}")
            timeout = cmd_metadata.get('timeout')
            if timeout:
                click.echo(f"  Timeout: {timeout}s")
            priority = cmd_metadata.get('priority')
            if priority:
                click.echo(f"  Priority: {priority}")
        created_at = result.get('created_at')
        if created_at:
            click.echo(f"  Created: {created_at}")
        data_schema = result.get("data_schema")
        click.echo("\n  Data schema:")
        if isinstance(data_schema, dict):
            click.echo(json.dumps(data_schema, indent=2, default=str))
        else:
            click.echo("    —")
        click.echo("\n  🚀 Example CLI Command:")
        click.echo(f"    motet-cli command run {command_name} --data '{{\"key\": \"value\"}}'")
        click.echo("\n    # Optional: --conversation-id <id>  --timeout <seconds>")
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo(f"\n❌ Could not connect to API at {api_url}", err=True)
        click.echo("💡 Make sure the API server is running", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"❌ Error getting command info: {e}", err=True)
        raise click.Abort()


@commands_group.command("run")
@click.argument("command_name")
@click.option(
    "--data",
    default="{}",
    help="JSON data for command (default: '{}' for commands with no data)"
)
@click.option("--conversation-id", help="Conversation ID to associate with the execution")
@click.option(
    "--timeout",
    type=int,
    default=60,
    help="Execution timeout in seconds (default: 60)"
)
@api_url_option()
def run_command(
    command_name: str,
    data: str,
    conversation_id: Optional[str],
    timeout: int,
    api_url: str
) -> None:
    """Execute a command via POST /api/v1/commands/{command_type}/execute (ADR-0071)."""
    try:
        data_dict = json.loads(data)
    except json.JSONDecodeError as e:
        click.echo(f"❌ Invalid JSON data: {e}", err=True)
        raise click.Abort()
    payload = {"data": data_dict, "timeout_seconds": timeout}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    click.echo(f"🚀 Running command '{command_name}' via API...")
    if conversation_id:
        click.echo(f"💬 Conversation ID: {conversation_id}")
    click.echo(f"📝 Data: {json.dumps(data_dict, indent=2)}\n")
    url = f"{api_url.rstrip('/')}/api/v1/commands/{command_name}/execute"
    headers = get_api_headers()
    click.echo(f"🌐 API: {url}")
    click.echo("⏳ Waiting for command to complete...\n")
    try:
        response = api_request("POST", url, headers=headers, json=payload, timeout=timeout + 10)
        result = response.json()
        click.echo("✅ Command completed successfully\n")
        click.echo(f"📋 Task ID: {result.get('task_id', 'N/A')}")
        click.echo(f"💬 Conversation ID: {result.get('conversation_id', 'N/A')}")
        click.echo("\n📊 Result:")
        click.echo(json.dumps(result.get('result', result), indent=2, default=str))
    except click.ClickException:
        raise
    except requests.exceptions.Timeout:
        click.echo(f"\n❌ Request timed out after {timeout + 10} seconds", err=True)
        raise click.Abort()
    except requests.exceptions.ConnectionError:
        click.echo(f"\n❌ Could not connect to API at {api_url}", err=True)
        click.echo("💡 Make sure the API server is running", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"\n❌ Error running command: {e}", err=True)
        raise click.Abort()


