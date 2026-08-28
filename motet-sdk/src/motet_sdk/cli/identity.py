"""
Motet - Identity CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-02-23

Description:
    CLI for identity — calls /api/v1/identity.
    Current principal (me), current tenant (tenant).

Dependencies:
    - click: CLI framework
    - motet.cli._auth: get_api_headers
    - motet.cli._api: api_request, normalize_base_url

Usage:
    motet-cli identity me
    motet-cli identity tenant
"""

from __future__ import annotations

import json

import click

from ._api import api_request, api_url_option, normalize_base_url
from ._auth import get_api_headers

API_TIMEOUT = 30


@click.group("identity")
def identity_group() -> None:
    """Current principal and tenant (API: /api/v1/identity)."""
    pass


@identity_group.command("me")
@api_url_option()
def me(api_url: str) -> None:
    """Current principal (GET /api/v1/identity/me)."""
    base = normalize_base_url(api_url)
    r = api_request(
        "GET", f"{base}/api/v1/identity/me", headers=get_api_headers(), timeout=API_TIMEOUT
    )
    click.echo(json.dumps(r.json(), indent=2))


@identity_group.command("tenant")
@api_url_option()
def tenant(api_url: str) -> None:
    """Current tenant (GET /api/v1/identity/tenant)."""
    base = normalize_base_url(api_url)
    r = api_request(
        "GET", f"{base}/api/v1/identity/tenant", headers=get_api_headers(), timeout=API_TIMEOUT
    )
    click.echo(json.dumps(r.json(), indent=2))


__all__ = ["identity_group"]
