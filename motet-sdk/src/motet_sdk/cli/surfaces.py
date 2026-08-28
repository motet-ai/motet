"""
Motet - Surfaces Catalog CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-07

Description:
    CLI for the conversation surfaces catalog via /api/v1/surfaces.
    List/get for any authenticated principal; create/update/delete require admin.

Dependencies:
    - click: CLI framework
    - motet_sdk.cli._api / _auth: HTTP helpers

Usage:
    motet-cli surfaces list
    motet-cli surfaces get demo_chat
    motet-cli surfaces create partner_portal --name "Partner Portal"
    motet-cli surfaces update partner_portal --description "…"
    motet-cli surfaces delete partner_portal

Notes:
    - Builtin surfaces cannot be deleted
    - Bundle deploy can also register surfaces via config/surfaces.yaml
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import click

from ._api import api_request, api_url_option, normalize_base_url
from ._auth import get_api_headers

API_TIMEOUT = 30


def _base(api_url: str) -> str:
    return normalize_base_url(api_url)


def _print_json(payload: Any) -> None:
    click.echo(json.dumps(payload, indent=2, default=str))


@click.group("surfaces")
def surfaces_group() -> None:
    """Surfaces catalog (API: /api/v1/surfaces)."""
    pass


@surfaces_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Print raw JSON")
@api_url_option()
def list_surfaces(as_json: bool, api_url: str) -> None:
    """List surfaces in the catalog."""
    response = api_request(
        "GET",
        f"{_base(api_url)}/api/v1/surfaces",
        headers=get_api_headers(),
        timeout=API_TIMEOUT,
    )
    data = response.json()
    if as_json:
        _print_json(data)
        return

    surfaces: List[Dict[str, Any]] = data.get("surfaces") or []
    can_manage = data.get("can_manage")
    click.echo(f"Surfaces ({len(surfaces)})  can_manage={can_manage}")
    if not surfaces:
        return
    for surface in surfaces:
        sid = surface.get("id", "N/A")
        name = surface.get("display_name") or sid
        builtin = "builtin" if surface.get("builtin") else "custom"
        click.echo(f"  • {sid}: {name} [{builtin}]")
        desc = surface.get("description")
        if desc:
            click.echo(f"    {desc}")


@surfaces_group.command("get")
@click.argument("surface_id")
@click.option("--json", "as_json", is_flag=True, help="Print raw JSON")
@api_url_option()
def get_surface(surface_id: str, as_json: bool, api_url: str) -> None:
    """Get one surface catalog entry."""
    response = api_request(
        "GET",
        f"{_base(api_url)}/api/v1/surfaces/{surface_id}",
        headers=get_api_headers(),
        timeout=API_TIMEOUT,
    )
    data = response.json()
    if as_json:
        _print_json(data)
        return
    _print_json(data)


@surfaces_group.command("create")
@click.argument("surface_id")
@click.option("--name", "display_name", help="Display name (defaults to id)")
@click.option("--description", help="Optional description")
@api_url_option()
def create_surface(
    surface_id: str,
    display_name: Optional[str],
    description: Optional[str],
    api_url: str,
) -> None:
    """Create a surface in the catalog (admin)."""
    payload: Dict[str, Any] = {"id": surface_id}
    if display_name is not None:
        payload["display_name"] = display_name
    if description is not None:
        payload["description"] = description
    response = api_request(
        "POST",
        f"{_base(api_url)}/api/v1/surfaces",
        headers=get_api_headers(),
        json=payload,
        timeout=API_TIMEOUT,
    )
    click.echo(f"Created surface '{surface_id}'")
    _print_json(response.json())


@surfaces_group.command("update")
@click.argument("surface_id")
@click.option("--name", "display_name", help="Display name")
@click.option("--description", help="Description")
@api_url_option()
def update_surface(
    surface_id: str,
    display_name: Optional[str],
    description: Optional[str],
    api_url: str,
) -> None:
    """Update surface display name / description (admin)."""
    payload: Dict[str, Any] = {}
    if display_name is not None:
        payload["display_name"] = display_name
    if description is not None:
        payload["description"] = description
    if not payload:
        raise click.UsageError("Provide at least one of --name, --description")
    response = api_request(
        "PATCH",
        f"{_base(api_url)}/api/v1/surfaces/{surface_id}",
        headers=get_api_headers(),
        json=payload,
        timeout=API_TIMEOUT,
    )
    click.echo(f"Updated surface '{surface_id}'")
    _print_json(response.json())


@surfaces_group.command("delete")
@click.argument("surface_id")
@click.confirmation_option(prompt="Delete this surface from the catalog?")
@api_url_option()
def delete_surface(surface_id: str, api_url: str) -> None:
    """Delete a non-builtin surface (admin)."""
    api_request(
        "DELETE",
        f"{_base(api_url)}/api/v1/surfaces/{surface_id}",
        headers=get_api_headers(),
        timeout=API_TIMEOUT,
    )
    click.echo(f"Deleted surface '{surface_id}'")
