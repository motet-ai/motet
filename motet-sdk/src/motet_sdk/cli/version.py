"""
Motet - Stack Version CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    CLI for inspecting Motet product versions across the running stack.
    Calls GET /api/v1/version (API, workers, configured siblings). Distinct
    from ``motet-cli --version``, which prints the local CLI/SDK package
    version only.

Dependencies:
    - click: CLI framework
    - motet_sdk.cli._auth: get_api_headers
    - motet_sdk.cli._api: api_request, normalize_base_url

Usage:
    motet-cli version
    motet-cli version --api-url http://localhost:8000
"""

from __future__ import annotations

import json

import click

from ._api import api_request, api_url_option, normalize_base_url
from ._auth import get_api_headers

API_TIMEOUT = 30


@click.command("version")
@api_url_option()
def version_command(api_url: str) -> None:
    """Inspect Motet versions on the running stack (GET /api/v1/version).

    ``motet-cli --version`` prints this machine's package version only.
    """
    base = normalize_base_url(api_url)
    response = api_request(
        "GET",
        f"{base}/api/v1/version",
        headers=get_api_headers(),
        timeout=API_TIMEOUT,
    )
    click.echo(json.dumps(response.json(), indent=2))


__all__ = ["version_command"]
