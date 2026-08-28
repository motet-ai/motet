"""
Motet - Agents CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    CLI for agent discovery operations via /api/v1/agents.
    Supports listing visible agent configurations for the current principal.

Dependencies:
    - click: CLI framework
    - motet.cli._api: API request helper and URL option
    - motet.cli._auth: Auth headers for API requests

Usage:
    motet-cli agents list
    motet-cli agents list --json-output
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import click

from ._api import api_request, api_url_option, normalize_base_url
from ._auth import get_api_headers

API_TIMEOUT = 30


@click.group("agents")
def agents_group() -> None:
    """Agent discovery commands (API: /api/v1/agents)."""
    pass


@agents_group.command("list")
@click.option("--json-output", is_flag=True, help="Output raw JSON")
@api_url_option()
def list_agents(json_output: bool, api_url: str) -> None:
    """List agents visible to the current principal."""
    base = normalize_base_url(api_url)
    response = api_request(
        "GET",
        f"{base}/api/v1/agents",
        headers=get_api_headers(),
        timeout=API_TIMEOUT,
    )
    payload: Dict[str, Any] = response.json()
    agents: List[Dict[str, Any]] = payload.get("agents", [])

    if json_output:
        click.echo(json.dumps(payload, indent=2, default=str))
        return

    if not agents:
        click.echo("📭 No visible agents")
        return

    click.echo(f"📋 Found {len(agents)} agent(s):")
    click.echo("")
    for agent in agents:
        qualified_id = agent.get("qualified_id", "N/A")
        display_name = agent.get("display_name", "") or ""
        bundle_id = agent.get("bundle_id", None)
        allowed_roles = agent.get("allowed_roles", [])
        roles_text = ", ".join(allowed_roles) if allowed_roles else "N/A"
        source = "core" if not bundle_id else str(bundle_id)

        click.echo(f"  • {qualified_id}")
        if display_name:
            click.echo(f"    Name: {display_name}")
        click.echo(f"    Source: {source}")
        click.echo(f"    Roles: {roles_text}")
        click.echo("")


__all__ = ["agents_group"]

