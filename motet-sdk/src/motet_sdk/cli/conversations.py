"""
Motet - Conversations CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    CLI for conversations — calls /api/v1/conversations.
    List, get, clear, rename, delete.

Dependencies:
    - click: CLI framework
    - motet.cli._auth: get_api_headers
    - motet.cli._api: api_request, normalize_base_url

Usage:
    motet-cli conversations list
    motet-cli conversations get <conversation_id>
    motet-cli conversations clear <conversation_id>
    motet-cli conversations rename <conversation_id> --title "New title"
    motet-cli conversations delete <conversation_id>
"""

from __future__ import annotations

import json
from typing import Optional
from urllib.parse import urlencode

import click

from ._api import api_request, api_url_option, normalize_base_url
from ._auth import get_api_headers

API_TIMEOUT = 60


@click.group("conversations")
def conversations_group() -> None:
    """Manage conversations (API: /api/v1/conversations)."""
    pass


@conversations_group.command("list")
@click.option("--agent-id", help="Filter by agent (e.g. default, motet_admin). Omitted defaults to core.default.")
@click.option("--surface-id", help="Filter by surface (e.g. demo_chat, ops_dashboard, cli). Omitted returns all surfaces for agent.")
@api_url_option()
def list_conversations(
    api_url: str,
    agent_id: Optional[str],
    surface_id: Optional[str],
) -> None:
    """List conversations (GET /api/v1/conversations). Optional agent_id and surface_id filters."""
    base = normalize_base_url(api_url)
    url = f"{base}/api/v1/conversations"
    q = {}
    if agent_id:
        q["agent_id"] = agent_id
    if surface_id:
        q["surface_id"] = surface_id
    if q:
        url = f"{url}?{urlencode(q)}"
    r = api_request("GET", url, headers=get_api_headers(), timeout=API_TIMEOUT)
    data = r.json()
    for c in data.get("conversations", []):
        parts = [c.get("id", ""), c.get("title", ""), f"updated={c.get('updated_at')}"]
        if c.get("agent_id"):
            parts.append(f"agent={c['agent_id']}")
        if c.get("surface_id"):
            parts.append(f"surface={c['surface_id']}")
        click.echo("  " + "  ".join(parts))


@conversations_group.command("get")
@click.argument("conversation_id")
@api_url_option()
def get_conversation(conversation_id: str, api_url: str) -> None:
    """Get conversation details (GET /api/v1/conversations/{id})."""
    base = normalize_base_url(api_url)
    r = api_request(
        "GET",
        f"{base}/api/v1/conversations/{conversation_id}",
        headers=get_api_headers(),
        timeout=API_TIMEOUT,
    )
    click.echo(json.dumps(r.json(), indent=2))


@conversations_group.command("clear")
@click.argument("conversation_id")
@api_url_option()
def clear_conversation(conversation_id: str, api_url: str) -> None:
    """Clear conversation (POST /api/v1/conversations/{id}/clear)."""
    base = normalize_base_url(api_url)
    r = api_request(
        "POST",
        f"{base}/api/v1/conversations/{conversation_id}/clear",
        headers=get_api_headers(),
        timeout=API_TIMEOUT,
    )
    data = r.json()
    click.echo(f"✅ Cleared: {data.get('cleared', {})}")


@conversations_group.command("rename")
@click.argument("conversation_id")
@click.option("--title", required=True, help="New title")
@api_url_option()
def rename_conversation(conversation_id: str, title: str, api_url: str) -> None:
    """Rename conversation (PATCH /api/v1/conversations/{id})."""
    base = normalize_base_url(api_url)
    r = api_request(
        "PATCH",
        f"{base}/api/v1/conversations/{conversation_id}",
        headers=get_api_headers(),
        json={"title": title},
        timeout=API_TIMEOUT,
    )
    click.echo("✅ Renamed.")


@conversations_group.command("delete")
@click.argument("conversation_id")
@api_url_option()
def delete_conversation(conversation_id: str, api_url: str) -> None:
    """Delete conversation (DELETE /api/v1/conversations/{id})."""
    base = normalize_base_url(api_url)
    api_request(
        "DELETE",
        f"{base}/api/v1/conversations/{conversation_id}",
        headers=get_api_headers(),
        timeout=API_TIMEOUT,
    )
    click.echo("✅ Deleted.")


__all__ = ["conversations_group"]
