"""
Motet - Tools CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-03

Description:
    CLI commands for tool listing, description, and execution.

Dependencies:
    - click: CLI framework
    - requests: API communication

Usage:
    motet-cli tools list              # List available tools (GET /api/v1/tools)
    motet-cli tools describe          # Tool descriptions only (GET /api/v1/tools/describe)
    motet-cli tools call --name <tool> --params '{}'  # Execute a tool

Notes:
    - Aligns with API structure (api/v1/tools.py)
    - Uses API for consistency
"""

import json
from typing import Any

import click
import requests

from ._api import api_request, api_url_option
from ._auth import get_api_headers
from ._logging import logger


@click.group("tools")
def tools_group() -> None:
    """Tools/MCP utilities."""
    pass


@tools_group.command("list")
@click.option("--json-output", "json_output", is_flag=True, help="Output raw JSON")
@api_url_option()
def list_tools(json_output: bool, api_url: str) -> None:
    """List available tools (names and descriptions) from the API."""
    try:
        url = f"{api_url.rstrip('/')}/api/v1/tools"
        headers = get_api_headers()
        response = api_request("GET", url, headers=headers, timeout=30)
        data: dict[str, Any] = response.json()
        if json_output:
            click.echo(json.dumps(data, indent=2))
            return
        if not data:
            click.echo("📭 No tools found")
            return
        click.echo(f"📋 Found {len(data)} tool(s):\n")
        for name, info in sorted(data.items()):
            desc = (info.get("description") or "").strip()
            if desc:
                click.echo(f"  • {name}")
                click.echo(f"    {desc}")
            else:
                click.echo(f"  • {name}")
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo("\n❌ Could not connect to API at " + api_url, err=True)
        click.echo("💡 Make sure the API server is running", err=True)
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)


@tools_group.command("describe")
@click.option("--json-output", "json_output", is_flag=True, help="Output raw JSON")
@api_url_option()
def describe_tools(json_output: bool, api_url: str) -> None:
    """Get tool descriptions (simpler format, no schemas) from the API."""
    try:
        url = f"{api_url.rstrip('/')}/api/v1/tools/describe"
        headers = get_api_headers()
        response = api_request("GET", url, headers=headers, timeout=30)
        data = response.json()
        if json_output:
            click.echo(json.dumps(data, indent=2))
            return
        if not isinstance(data, list):
            click.echo(json.dumps(data, indent=2))
            return
        if not data:
            click.echo("📭 No tools found")
            return
        click.echo(f"📋 {len(data)} tool(s):\n")
        for item in data:
            name = item.get("name", "N/A") if isinstance(item, dict) else item
            desc = item.get("description", "") if isinstance(item, dict) else ""
            if desc:
                click.echo(f"  • {name}: {desc}")
            else:
                click.echo(f"  • {name}")
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo("\n❌ Could not connect to API at " + api_url, err=True)
        click.echo("💡 Make sure the API server is running", err=True)
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)


@tools_group.command("call")
@click.option("--name", required=True, help="Tool name (e.g., mcp.weather.get_current_conditions)")
@click.option("--params", default="{}", help="JSON params")
@click.option("--timeout", type=float, default=15.0)
@api_url_option()
def call_tool(name: str, params: str, timeout: float, api_url: str) -> None:
    """Call a registered tool by name via API and print JSON result."""
    try:
        p = json.loads(params or "{}")
    except Exception:
        click.echo("invalid JSON for --params", err=True)
        return
    try:
        url = f"{api_url.rstrip('/')}/api/v1/tools/execute"
        # API ToolRequest field is ``params`` (not ``parameters``).
        payload = {"name": name, "params": p, "timeout": timeout}
        headers = get_api_headers()
        response = api_request("POST", url, headers=headers, json=payload, timeout=int(timeout) + 5)
        click.echo(json.dumps(response.json(), indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo(f"\n❌ Could not connect to API at {api_url}", err=True)
        click.echo("💡 Make sure the API server is running", err=True)
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)

