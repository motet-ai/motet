"""
Motet - Service Account CLI

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    CLI commands for managing service account tokens over the public API.
    Service accounts are long-lived tokens for CLI/automation use cases.

    Tokens can also carry OpenAI-compatible facade policy: execution mode, model
    allowlist, force_thinking, and agent_id for clients such as Cursor that cannot
    send Motet extensions.

Dependencies:
    - click: CLI framework
    - requests: API communication
    - motet.cli._auth: Authentication helper

Usage:
    motet-cli service-account create --name ci-pipeline --tenant acme-corp --roles admin,ci --store
    motet-cli service-account create --name cursor --roles member \
        --facade-mode agent --allowed-models 'openai/*,deepseek/*' \
        --force-thinking --agent-id cursor.backend
    motet-cli service-account list
    motet-cli service-account revoke <token>

Notes:
    - Service accounts are stored in Redis by the API backend
    - Tokens are prefixed with "sa_" for identification
    - Part of Week 2-3: CLI JWT Support
"""

from __future__ import annotations

import os
from typing import Optional

import click
import requests

from ._api import api_request, api_url_option
from ._auth import get_api_headers, store_credentials
from motet.core.config import Config

API_TIMEOUT_SECONDS = 30


@click.group("service-account")
def service_account_group() -> None:
    """Manage service account tokens for automation."""
    pass


@service_account_group.command("create")
@click.option("--name", required=True, help="Service account name (e.g., 'ci-pipeline')")
@click.option("--tenant", help="Tenant ID (optional)")
@click.option("--motet", help="Motet/environment ID (optional)")
@click.option("--roles", required=True, help="Comma-separated roles (e.g., 'admin,ci')")
@click.option("--expires-days", default=365, type=int, help="Expiration in days (default: 365)")
@click.option(
    "--facade-mode",
    type=click.Choice(["passthrough", "hosted_tools", "agent"]),
    help="OpenAI-compatible facade mode bound to this token (also its ceiling)",
)
@click.option(
    "--allowed-models",
    help=(
        "Comma-separated OpenAI facade model allowlist "
        "(e.g. 'openai/gpt-4o-mini,anthropic/*'). Omit to deny all facade models."
    ),
)
@click.option(
    "--force-thinking/--no-force-thinking",
    default=None,
    help=(
        "Enable Motet thinking for CAP_REASONING models even when the client omits "
        "reasoning opt-in (useful for Cursor BYOK). Omit to use server config default."
    ),
)
@click.option(
    "--force-thinking-effort",
    help="Default reasoning effort when --force-thinking applies (e.g. medium).",
)
@click.option(
    "--agent-id",
    help=(
        "Default Motet agent id for facade agent mode when the client omits "
        "motet_agent_id (e.g. cursor.backend)."
    ),
)
@click.option("--store", is_flag=True, help="Store token in ~/.motet/credentials.json")
@api_url_option()
def create_service_account(
    name: str,
    tenant: Optional[str],
    motet: Optional[str],
    roles: str,
    expires_days: int,
    facade_mode: Optional[str],
    allowed_models: Optional[str],
    force_thinking: Optional[bool],
    force_thinking_effort: Optional[str],
    agent_id: Optional[str],
    store: bool,
    api_url: str,
) -> None:
    """Create a new service account token via the API."""
    headers = get_api_headers()
    cfg = Config()
    resolved_tenant = tenant or headers.get("X-Tenant-Id") or os.getenv("MOTET_TENANT_ID")
    resolved_motet = motet or headers.get("X-Motet-Id") or getattr(cfg, "motet_id", None) or os.getenv("MOTET_MOTET_ID", "default")

    if not resolved_tenant:
        raise click.BadParameter("Tenant ID is required (use --tenant or MOTET_TENANT_ID)")
    if not resolved_motet:
        raise click.BadParameter("Motet ID is required (use --motet or MOTET_MOTET_ID)")

    payload = {
        "name": name,
        "tenant_id": resolved_tenant,
        "motet_id": resolved_motet,
        "roles": [r.strip() for r in roles.split(",") if r.strip()],
        "expires_days": expires_days,
    }
    if facade_mode:
        payload["facade_mode"] = facade_mode
    if allowed_models:
        payload["allowed_models"] = [m.strip() for m in allowed_models.split(",") if m.strip()]
    if force_thinking is not None:
        payload["force_thinking"] = force_thinking
    if force_thinking_effort:
        payload["force_thinking_effort"] = force_thinking_effort
    if agent_id:
        payload["agent_id"] = agent_id

    response = api_request(
        "POST",
        f"{api_url.rstrip('/')}/api/v1/service-accounts",
        headers=headers,
        json=payload,
        timeout=API_TIMEOUT_SECONDS,
    )
    data = response.json()
    token = data["token"]

    click.echo(f"✅ Service account '{name}' created successfully!")
    click.echo("\n🔑 Token (save this - it won't be shown again):")
    click.echo(f"   {token}")
    click.echo("\n📋 Usage:")
    click.echo(f"   export MOTET_SERVICE_ACCOUNT_TOKEN='{token}'")
    click.echo("   motet-cli commands list")

    if store:
        store_credentials(sa_token=token)
        click.echo("\n💾 Token stored in ~/.motet/credentials.json")


@service_account_group.command("list")
@click.option("--tenant", help="Filter by tenant ID")
@click.option("--motet", help="Filter by motet/environment ID")
@api_url_option()
def list_service_accounts(tenant: Optional[str], motet: Optional[str], api_url: str) -> None:
    """List service accounts via the API."""
    headers = get_api_headers()
    params = {}
    if tenant:
        params["tenant_id"] = tenant
    if motet:
        params["motet_id"] = motet

    response = api_request(
        "GET",
        f"{api_url.rstrip('/')}/api/v1/service-accounts",
        headers=headers,
        params=params,
        timeout=API_TIMEOUT_SECONDS,
    )
    accounts = response.json().get("service_accounts", [])

    if not accounts:
        click.echo("No service accounts found.")
        return

    click.echo(f"\n🔑 Service Accounts ({len(accounts)}):")
    for account in accounts:
        click.echo(f"\n  Name: {account['name']}")
        click.echo(f"  Token ID: {account['id']}")
        click.echo(f"  Principal: {account['principal_id']}")
        click.echo(f"  Tenant: {account.get('tenant_id') or '(none)'}")
        click.echo(f"  Motet: {account.get('motet_id') or '(none)'}")
        click.echo(f"  Roles: {', '.join(account.get('roles', []))}")
        click.echo(f"  Created: {account.get('created_at')}")
        click.echo(f"  Expires: {account.get('expires_at')}")
        if account.get("last_used_at"):
            click.echo(f"  Last Used: {account['last_used_at']}")
        if account.get("revoked_at"):
            click.echo(f"  ⚠️  Revoked: {account['revoked_at']}")


@service_account_group.command("revoke")
@click.argument("token")
@api_url_option()
def revoke_service_account(token: str, api_url: str) -> None:
    """Revoke a service account token via the API."""
    headers = get_api_headers()
    api_request(
        "DELETE",
        f"{api_url.rstrip('/')}/api/v1/service-accounts/{token}",
        headers=headers,
        timeout=API_TIMEOUT_SECONDS,
    )

    click.echo("✅ Service account token revoked successfully!")

    from ._auth import get_credentials_path
    import json

    creds_path = get_credentials_path()
    if creds_path.exists():
        try:
            with open(creds_path) as f:
                creds = json.load(f)
            if creds.get("service_account_token") == token:
                creds.pop("service_account_token", None)
                with open(creds_path, "w") as f:
                    json.dump(creds, f, indent=2)
                creds_path.chmod(0o600)
                click.echo("💾 Token removed from stored credentials")
        except Exception:
            pass


__all__ = ["service_account_group"]
