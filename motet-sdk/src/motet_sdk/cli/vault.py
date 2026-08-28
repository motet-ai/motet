"""
Motet - Vault CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-02-23

Description:
    CLI for vault credentials and MCP — calls /api/v1/vault.
    List, get, store, retrieve, delete credentials; MCP env/servers; health, stats.

Dependencies:
    - click: CLI framework
    - motet.cli._auth: get_api_headers
    - motet.cli._api: api_request, normalize_base_url

Usage:
    motet-cli vault list
    motet-cli vault get <credential_id>
    motet-cli vault store --id <id> --data '{"key":"val"}' --type api_key --scope principal
    motet-cli vault retrieve --key <credential_key>
    motet-cli vault delete <credential_id>
    motet-cli vault mcp-env --server <mcp_server_id>
    motet-cli vault mcp-servers
    motet-cli vault health
    motet-cli vault stats
"""

from __future__ import annotations

import json
from typing import Optional

import click

from ._api import api_request, api_url_option, normalize_base_url
from ._auth import get_api_headers

API_TIMEOUT = 30

CRED_TYPES = ["api_key", "bearer_token", "oauth_token", "database_password", "ssh_key"]
SCOPES = ["global", "motet", "tenant", "principal"]
SECURITY_LEVELS = ["public", "internal", "confidential", "secret", "top_secret"]


@click.group("vault")
def vault_group() -> None:
    """Manage vault credentials and MCP (API: /api/v1/vault)."""
    pass


@vault_group.command("list")
@click.option("--credential-type", help=f"Filter by type: {', '.join(CRED_TYPES)}")
@api_url_option()
def list_credentials(credential_type: Optional[str], api_url: str) -> None:
    """List credentials (GET /api/v1/vault/credentials)."""
    base = normalize_base_url(api_url)
    params = {}
    if credential_type:
        params["credential_type"] = credential_type
    r = api_request(
        "GET", f"{base}/api/v1/vault/credentials", headers=get_api_headers(), params=params, timeout=API_TIMEOUT
    )
    data = r.json()
    if data.get("status") == "error":
        raise click.ClickException(data.get("error", "List failed"))
    creds = data.get("credentials", [])
    click.echo(f"Total: {len(creds)}\n")
    for c in creds:
        click.echo(f"  {c.get('credential_id')}  {c.get('credential_type')}  {c.get('scope')}  {c.get('description') or '-'}")


@vault_group.command("get")
@click.argument("credential_id")
@api_url_option()
def get_credential(credential_id: str, api_url: str) -> None:
    """Get credential metadata (GET /api/v1/vault/credentials/{id}). Does not return secret data."""
    base = normalize_base_url(api_url)
    r = api_request(
        "GET", f"{base}/api/v1/vault/credentials/{credential_id}", headers=get_api_headers(), timeout=API_TIMEOUT
    )
    data = r.json()
    if data.get("status") == "error":
        raise click.ClickException(data.get("error", "Get failed"))
    click.echo(json.dumps(data.get("credential", data), indent=2))


@vault_group.command("store")
@click.option("--id", "credential_id", required=True, help="Credential ID")
@click.option("--data", required=True, help="JSON credential data (e.g. {\"api_key\": \"xxx\"})")
@click.option("--type", "credential_type", required=True, type=click.Choice(CRED_TYPES), help="Credential type")
@click.option("--scope", required=True, type=click.Choice(SCOPES), help="Scope")
@click.option("--security-level", default="confidential", type=click.Choice(SECURITY_LEVELS), help="Security level")
@click.option("--description", default="", help="Description")
@api_url_option()
def store_credential(
    credential_id: str,
    data: str,
    credential_type: str,
    scope: str,
    security_level: str,
    description: str,
    api_url: str,
) -> None:
    """Store a credential (POST /api/v1/vault/credentials)."""
    base = normalize_base_url(api_url)
    payload = {
        "credential_id": credential_id,
        "credential_data": json.loads(data),
        "credential_type": credential_type,
        "scope": scope,
        "security_level": security_level,
        "description": description,
    }
    r = api_request(
        "POST", f"{base}/api/v1/vault/credentials", headers=get_api_headers(), json=payload, timeout=API_TIMEOUT
    )
    out = r.json()
    if out.get("success"):
        click.echo("✅ Credential stored.")
    else:
        raise click.ClickException(out.get("error_message", "Store failed"))


@vault_group.command("retrieve")
@click.option("--key", "credential_key", required=True, help="Credential key to retrieve")
@api_url_option()
def retrieve_credential(credential_key: str, api_url: str) -> None:
    """Retrieve credential data (POST /api/v1/vault/credentials/retrieve)."""
    base = normalize_base_url(api_url)
    payload = {"credential_key": credential_key}
    r = api_request(
        "POST", f"{base}/api/v1/vault/credentials/retrieve", headers=get_api_headers(), json=payload, timeout=API_TIMEOUT
    )
    out = r.json()
    if not out.get("success"):
        raise click.ClickException(out.get("error_message", "Retrieve failed"))
    # Don't echo raw secret data by default; allow --show for scripts
    click.echo(json.dumps(out.get("credential_data") or {}, indent=2))


@vault_group.command("delete")
@click.argument("credential_id")
@api_url_option()
def delete_credential(credential_id: str, api_url: str) -> None:
    """Delete a credential (DELETE /api/v1/vault/credentials)."""
    base = normalize_base_url(api_url)
    r = api_request(
        "DELETE", f"{base}/api/v1/vault/credentials", headers=get_api_headers(), json={"credential_id": credential_id}, timeout=API_TIMEOUT
    )
    out = r.json()
    if out.get("success"):
        click.echo("✅ Credential deleted.")
    else:
        raise click.ClickException(out.get("error_message", "Delete failed"))


@vault_group.command("mcp-env")
@click.option("--server", "mcp_server_id", required=True, help="MCP server ID")
@api_url_option()
def mcp_env(mcp_server_id: str, api_url: str) -> None:
    """Get MCP environment variables (POST /api/v1/vault/mcp/environment)."""
    base = normalize_base_url(api_url)
    r = api_request(
        "POST", f"{base}/api/v1/vault/mcp/environment", headers=get_api_headers(), json={"mcp_server_id": mcp_server_id}, timeout=API_TIMEOUT
    )
    data = r.json()
    if not data.get("success"):
        raise click.ClickException(data.get("error_message", "MCP env failed"))
    click.echo(json.dumps(data.get("environment_variables", {}), indent=2))


@vault_group.command("mcp-servers")
@api_url_option()
def mcp_servers(api_url: str) -> None:
    """List supported MCP servers (GET /api/v1/vault/mcp/servers)."""
    base = normalize_base_url(api_url)
    r = api_request("GET", f"{base}/api/v1/vault/mcp/servers", headers=get_api_headers(), timeout=API_TIMEOUT)
    click.echo(json.dumps(r.json(), indent=2))


@vault_group.command("health")
@api_url_option()
def health(api_url: str) -> None:
    """Vault health check (GET /api/v1/vault/health)."""
    base = normalize_base_url(api_url)
    r = api_request("GET", f"{base}/api/v1/vault/health", headers=get_api_headers(), timeout=API_TIMEOUT)
    click.echo(json.dumps(r.json(), indent=2))


@vault_group.command("stats")
@api_url_option()
def stats(api_url: str) -> None:
    """Vault stats (GET /api/v1/vault/stats)."""
    base = normalize_base_url(api_url)
    r = api_request("GET", f"{base}/api/v1/vault/stats", headers=get_api_headers(), timeout=API_TIMEOUT)
    click.echo(json.dumps(r.json(), indent=2))


__all__ = ["vault_group"]
