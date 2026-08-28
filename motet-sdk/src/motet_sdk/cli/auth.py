"""
Motet - Auth CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-06

Description:
    CLI for obtaining and managing principal (user) authentication.
    Login (OAuth in browser), store-token, logout (API + local), status.
    Other CLIs use the stored token or MOTET_JWT_TOKEN / service account
    via get_api_headers().

Dependencies:
    - click: CLI framework
    - motet_sdk.cli._auth: get_api_headers, store_credentials, clear_credentials, get_stored_token
    - motet_sdk.cli._api: api_request, normalize_base_url

Usage:
    motet-cli auth login
    motet-cli auth store-token [TOKEN]
    motet-cli auth logout
    motet-cli auth status
"""

from __future__ import annotations

import os
import sys
from typing import Optional

import click
import requests

from ._api import api_request, api_url_option, normalize_base_url
from ._auth import (
    clear_credentials,
    get_api_headers,
    get_credentials_path,
    get_stored_token,
    store_credentials,
)

API_TIMEOUT = 15


@click.group("auth")
def auth_group() -> None:
    """Obtain and manage principal (user) authentication for the CLI."""
    pass


@auth_group.command("login")
@api_url_option()
def login(api_url: str) -> None:
    """Open browser to log in and get a token; then run 'motet-cli auth store-token <token>' from the success page."""
    base = normalize_base_url(api_url)
    redirect_uri = f"{base}/api/v1/auth/cli-success"
    try:
        r = requests.get(
            f"{base}/api/v1/auth/login",
            params={"redirect_uri": redirect_uri},
            allow_redirects=False,
            timeout=API_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"Cannot reach API: {e}")
    # Accept any redirect (302, 307, etc.) with a Location header
    if r.status_code not in (301, 302, 303, 307, 308):
        detail = r.text
        if r.headers.get("content-type", "").startswith("application/json"):
            try:
                data = r.json()
                detail = data.get("detail", r.text)
                if isinstance(detail, list):
                    detail = "; ".join(str(x) for x in detail)
            except Exception:
                pass
        msg = f"Login start failed ({r.status_code}): {detail}"
        if r.status_code == 500:
            msg += "\nCheck API server logs for the traceback (e.g. Redis, Keycloak/MOTET_JWT_* config)."
        raise click.ClickException(msg)
    location = r.headers.get("Location")
    if not location:
        raise click.ClickException("API did not return a login redirect (no Location header).")
    try:
        import webbrowser
        webbrowser.open(location)
    except Exception as e:
        click.echo(f"Could not open browser: {e}", err=True)
        click.echo(f"Open this URL in your browser:\n  {location}")
    else:
        click.echo("Opened login in your browser.")
    click.echo("")
    click.echo("After logging in, the page will show a command like:")
    click.echo("  motet-cli auth store-token <token>")
    click.echo("Run that command to store your token; then other CLI commands will use it.")


@auth_group.command("store-token")
@click.argument("token", required=False)
@click.option("--stdin", "from_stdin", is_flag=True, help="Read token from stdin (e.g. echo $TOKEN | motet-cli auth store-token --stdin)")
def store_token(token: Optional[str], from_stdin: bool) -> None:
    """Store a JWT so other CLI commands use it. Get the token from 'motet-cli auth login' or set MOTET_JWT_TOKEN."""
    if from_stdin:
        token = sys.stdin.read().strip()
    if not token:
        raise click.UsageError("Provide TOKEN as argument or use --stdin to read from stdin.")
    store_credentials(jwt_token=token)
    click.echo("Token stored. Other CLI commands will use it (see ~/.motet/credentials.json).")


@auth_group.command("check")
@api_url_option()
def check(api_url: str) -> None:
    """Check why login might fail (JWT config, Keycloak, Redis). No auth required."""
    base = normalize_base_url(api_url)
    try:
        r = api_request(
            "GET",
            f"{base}/api/v1/auth/check",
            headers={},
            timeout=API_TIMEOUT,
            retry_on_401_refresh=False,
        )
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"Cannot reach API: {e}")
    data = r.json()
    click.echo("Auth login preflight:")
    click.echo(f"  JWT configured:    {data.get('jwt_configured', False)}")
    click.echo(f"  Keycloak resolved: {data.get('keycloak_resolved', False)}")
    click.echo(f"  Redis OK:          {data.get('redis_ok', False)}")
    click.echo(f"  Login ready:       {data.get('login_ready', False)}")
    errors = data.get("errors") or []
    if errors:
        click.echo("")
        for err in errors:
            click.echo(f"  ⚠ {err}", err=True)
        click.echo("")
        click.echo("Fix the issues above, then run: motet-cli auth login", err=True)
    else:
        click.echo("")
        click.echo("All checks passed. Run: motet-cli auth login")


@auth_group.command("logout")
@api_url_option()
def logout(api_url: str) -> None:
    """Clear server refresh token (GET /api/v1/auth/logout) and local credentials."""
    base = normalize_base_url(api_url)
    token = (
        get_stored_token()
        or os.environ.get("MOTET_JWT_TOKEN")
        or os.environ.get("MOTET_SERVICE_ACCOUNT_TOKEN")
    )
    server_cleared = False
    if token:
        try:
            api_request(
                "GET",
                f"{base}/api/v1/auth/logout",
                headers=get_api_headers(),
                timeout=API_TIMEOUT,
                retry_on_401_refresh=False,
            )
            server_cleared = True
            click.echo("Server session cleared.")
        except click.ClickException as e:
            click.echo(f"Warning: could not clear server session: {e}", err=True)
        except requests.exceptions.RequestException as e:
            click.echo(f"Warning: could not reach API to logout: {e}", err=True)
    else:
        click.echo("No token available; skipping server logout.")

    path = get_credentials_path()
    if path.exists():
        clear_credentials()
        click.echo("Local credentials cleared.")
    elif not server_cleared:
        click.echo("No stored credentials.")
    else:
        click.echo("No local credentials file to clear.")


@auth_group.command("status")
@api_url_option()
def status(api_url: str) -> None:
    """Show current principal (who the CLI is acting as). Uses stored token or MOTET_JWT_TOKEN / service account."""
    has_stored = bool(get_stored_token())
    headers = get_api_headers()
    base = normalize_base_url(api_url)
    try:
        r = api_request(
            "GET", f"{base}/api/v1/identity/me", headers=headers, timeout=API_TIMEOUT
        )
        data = r.json()
        click.echo("Logged in as:")
        click.echo(f"  id:          {data.get('id', '—')}")
        click.echo(f"  tenant_id:   {data.get('tenant_id', '—')}")
        click.echo(f"  roles:       {data.get('roles', [])}")
        if data.get("display_name"):
            click.echo(f"  display_name: {data['display_name']}")
        if data.get("email"):
            click.echo(f"  email:       {data['email']}")
        if has_stored:
            click.echo("  (using stored credentials)")
    except click.ClickException as e:
        if "401" in str(e) or "403" in str(e):
            click.echo("Not logged in (or token expired).", err=True)
            click.echo("Run 'motet-cli auth login' or set MOTET_JWT_TOKEN / MOTET_SERVICE_ACCOUNT_TOKEN.", err=True)
        else:
            raise


__all__ = ["auth_group"]
