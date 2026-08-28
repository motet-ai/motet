"""
Motet - Tenants / Motets Catalog CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    CLI for the operator-managed tenant and Motet (environment) catalog.
    Calls /api/v1/tenants.

Dependencies:
    - click: CLI framework
    - motet_sdk.cli._api / _auth: HTTP helpers

Usage:
    motet-cli tenants list --include-motets
    motet-cli tenants create acme --name "Acme Corp"
    motet-cli tenants get acme --include-motets
    motet-cli tenants update acme --name "Acme Corporation"
    motet-cli tenants delete acme --force
    motet-cli tenants ensure-defaults
    motet-cli tenants motets list acme
    motet-cli tenants motets create acme prod --name Production
    motet-cli tenants motets delete acme staging

Notes:
    - Mutations require an admin-authenticated API session
    - Motet means deployment environment under a tenant
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import click

from ._api import api_request, api_url_option, normalize_base_url
from ._auth import get_api_headers

API_TIMEOUT = 30


def _base(api_url: str) -> str:
    return normalize_base_url(api_url)


def _print_json(payload: Any) -> None:
    click.echo(json.dumps(payload, indent=2))


@click.group("tenants")
def tenants_group() -> None:
    """Tenant and Motet catalog (API: /api/v1/tenants)."""
    pass


@tenants_group.command("list")
@click.option(
    "--include-motets",
    is_flag=True,
    help="Nest Motets under each tenant",
)
@click.option(
    "--status",
    "status_filter",
    type=click.Choice(["active", "disabled"], case_sensitive=False),
    help="Filter by status",
)
@click.option("--json", "as_json", is_flag=True, help="Print raw JSON")
@api_url_option()
def list_tenants(
    include_motets: bool,
    status_filter: Optional[str],
    as_json: bool,
    api_url: str,
) -> None:
    """List tenants visible to the current principal."""
    params: Dict[str, Any] = {}
    if include_motets:
        params["include_motets"] = "true"
    if status_filter:
        params["status"] = status_filter
    response = api_request(
        "GET",
        f"{_base(api_url)}/api/v1/tenants",
        headers=get_api_headers(),
        params=params,
        timeout=API_TIMEOUT,
    )
    data = response.json()
    if as_json:
        _print_json(data)
        return

    tenants = data.get("tenants") or []
    can_all = data.get("can_access_all_tenants")
    click.echo(f"Tenants ({len(tenants)})  can_access_all_tenants={can_all}")
    if not tenants:
        return
    for tenant in tenants:
        click.echo(
            f"  {tenant['id']}: {tenant.get('name')} [{tenant.get('status')}]"
        )
        for motet in tenant.get("motets") or []:
            click.echo(
                f"    - {motet['id']}: {motet.get('name')} [{motet.get('status')}]"
            )


@tenants_group.command("create")
@click.argument("tenant_id")
@click.option("--name", help="Display name (defaults to id)")
@click.option("--description", help="Optional description")
@click.option(
    "--status",
    "status_value",
    type=click.Choice(["active", "disabled"], case_sensitive=False),
    default="active",
    show_default=True,
)
@api_url_option()
def create_tenant(
    tenant_id: str,
    name: Optional[str],
    description: Optional[str],
    status_value: str,
    api_url: str,
) -> None:
    """Create a tenant catalog entry."""
    payload: Dict[str, Any] = {"id": tenant_id, "status": status_value}
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    response = api_request(
        "POST",
        f"{_base(api_url)}/api/v1/tenants",
        headers=get_api_headers(),
        json=payload,
        timeout=API_TIMEOUT,
    )
    click.echo(f"Created tenant '{tenant_id}'")
    _print_json(response.json())


@tenants_group.command("get")
@click.argument("tenant_id")
@click.option("--include-motets", is_flag=True, help="Include nested Motets")
@api_url_option()
def get_tenant(tenant_id: str, include_motets: bool, api_url: str) -> None:
    """Get one tenant."""
    params = {"include_motets": "true"} if include_motets else None
    response = api_request(
        "GET",
        f"{_base(api_url)}/api/v1/tenants/{tenant_id}",
        headers=get_api_headers(),
        params=params,
        timeout=API_TIMEOUT,
    )
    _print_json(response.json())


@tenants_group.command("update")
@click.argument("tenant_id")
@click.option("--name", help="Display name")
@click.option("--description", help="Description")
@click.option(
    "--status",
    "status_value",
    type=click.Choice(["active", "disabled"], case_sensitive=False),
)
@api_url_option()
def update_tenant(
    tenant_id: str,
    name: Optional[str],
    description: Optional[str],
    status_value: Optional[str],
    api_url: str,
) -> None:
    """Update a tenant catalog entry."""
    payload: Dict[str, Any] = {}
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    if status_value is not None:
        payload["status"] = status_value
    if not payload:
        raise click.UsageError("Provide at least one of --name, --description, --status")
    response = api_request(
        "PATCH",
        f"{_base(api_url)}/api/v1/tenants/{tenant_id}",
        headers=get_api_headers(),
        json=payload,
        timeout=API_TIMEOUT,
    )
    click.echo(f"Updated tenant '{tenant_id}'")
    _print_json(response.json())


@tenants_group.command("delete")
@click.argument("tenant_id")
@click.option(
    "--force",
    is_flag=True,
    help="Also delete Motets under the tenant",
)
@click.confirmation_option(prompt="Delete this tenant from the catalog?")
@api_url_option()
def delete_tenant(tenant_id: str, force: bool, api_url: str) -> None:
    """Delete a tenant from the catalog."""
    params = {"force": "true"} if force else None
    api_request(
        "DELETE",
        f"{_base(api_url)}/api/v1/tenants/{tenant_id}",
        headers=get_api_headers(),
        params=params,
        timeout=API_TIMEOUT,
    )
    click.echo(f"Deleted tenant '{tenant_id}'")


@tenants_group.command("ensure-defaults")
@api_url_option()
def ensure_defaults(api_url: str) -> None:
    """Idempotently seed default/demo catalog entries."""
    response = api_request(
        "POST",
        f"{_base(api_url)}/api/v1/tenants/ensure-defaults",
        headers=get_api_headers(),
        timeout=API_TIMEOUT,
    )
    click.echo("Defaults ensured")
    _print_json(response.json())


@tenants_group.group("motets")
def motets_group() -> None:
    """Manage Motets (environments) under a tenant."""
    pass


@motets_group.command("list")
@click.argument("tenant_id")
@click.option(
    "--status",
    "status_filter",
    type=click.Choice(["active", "disabled"], case_sensitive=False),
)
@click.option("--json", "as_json", is_flag=True, help="Print raw JSON")
@api_url_option()
def list_motets(
    tenant_id: str,
    status_filter: Optional[str],
    as_json: bool,
    api_url: str,
) -> None:
    """List Motets for a tenant."""
    params: Dict[str, Any] = {}
    if status_filter:
        params["status"] = status_filter
    response = api_request(
        "GET",
        f"{_base(api_url)}/api/v1/tenants/{tenant_id}/motets",
        headers=get_api_headers(),
        params=params or None,
        timeout=API_TIMEOUT,
    )
    data = response.json()
    if as_json:
        _print_json(data)
        return
    motets = data.get("motets") or []
    click.echo(f"Motets for {tenant_id} ({len(motets)})")
    for motet in motets:
        click.echo(
            f"  {motet['id']}: {motet.get('name')} [{motet.get('status')}]"
        )


@motets_group.command("create")
@click.argument("tenant_id")
@click.argument("motet_id")
@click.option("--name", help="Display name (defaults to id)")
@click.option("--description", help="Optional description")
@click.option(
    "--status",
    "status_value",
    type=click.Choice(["active", "disabled"], case_sensitive=False),
    default="active",
    show_default=True,
)
@api_url_option()
def create_motet(
    tenant_id: str,
    motet_id: str,
    name: Optional[str],
    description: Optional[str],
    status_value: str,
    api_url: str,
) -> None:
    """Create a Motet under a tenant."""
    payload: Dict[str, Any] = {"id": motet_id, "status": status_value}
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    response = api_request(
        "POST",
        f"{_base(api_url)}/api/v1/tenants/{tenant_id}/motets",
        headers=get_api_headers(),
        json=payload,
        timeout=API_TIMEOUT,
    )
    click.echo(f"Created motet '{tenant_id}/{motet_id}'")
    _print_json(response.json())


@motets_group.command("get")
@click.argument("tenant_id")
@click.argument("motet_id")
@api_url_option()
def get_motet(tenant_id: str, motet_id: str, api_url: str) -> None:
    """Get one Motet."""
    response = api_request(
        "GET",
        f"{_base(api_url)}/api/v1/tenants/{tenant_id}/motets/{motet_id}",
        headers=get_api_headers(),
        timeout=API_TIMEOUT,
    )
    _print_json(response.json())


@motets_group.command("update")
@click.argument("tenant_id")
@click.argument("motet_id")
@click.option("--name", help="Display name")
@click.option("--description", help="Description")
@click.option(
    "--status",
    "status_value",
    type=click.Choice(["active", "disabled"], case_sensitive=False),
)
@api_url_option()
def update_motet(
    tenant_id: str,
    motet_id: str,
    name: Optional[str],
    description: Optional[str],
    status_value: Optional[str],
    api_url: str,
) -> None:
    """Update a Motet catalog entry."""
    payload: Dict[str, Any] = {}
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    if status_value is not None:
        payload["status"] = status_value
    if not payload:
        raise click.UsageError("Provide at least one of --name, --description, --status")
    response = api_request(
        "PATCH",
        f"{_base(api_url)}/api/v1/tenants/{tenant_id}/motets/{motet_id}",
        headers=get_api_headers(),
        json=payload,
        timeout=API_TIMEOUT,
    )
    click.echo(f"Updated motet '{tenant_id}/{motet_id}'")
    _print_json(response.json())


@motets_group.command("delete")
@click.argument("tenant_id")
@click.argument("motet_id")
@click.confirmation_option(prompt="Delete this Motet from the catalog?")
@api_url_option()
def delete_motet(tenant_id: str, motet_id: str, api_url: str) -> None:
    """Delete a Motet from the catalog."""
    api_request(
        "DELETE",
        f"{_base(api_url)}/api/v1/tenants/{tenant_id}/motets/{motet_id}",
        headers=get_api_headers(),
        timeout=API_TIMEOUT,
    )
    click.echo(f"Deleted motet '{tenant_id}/{motet_id}'")


__all__ = ["tenants_group"]
