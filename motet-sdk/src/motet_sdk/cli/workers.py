"""
Motet - Workers CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-06

Description:
    CLI for worker management — calls /api/v1/workers.
    Readiness, health, terminate, start, stop, restart, terminate-unhealthy,
    termination-history, managers, skill-workspaces.

Dependencies:
    - click: CLI framework
    - motet.cli._auth: get_api_headers
    - motet.cli._api: api_request, normalize_base_url

Usage:
    motet-cli workers readiness
    motet-cli workers health
    motet-cli workers managers
    motet-cli workers skill-workspaces
    motet-cli workers terminate <worker_id> [--reason ...] [--method ...]
    motet-cli workers start <worker_id>
    motet-cli workers stop <worker_id>
    motet-cli workers restart <worker_id>
    motet-cli workers terminate-unhealthy
    motet-cli workers termination-history
"""

from __future__ import annotations

import json
from typing import Optional

import click

from ._api import api_request, api_url_option, normalize_base_url
from ._auth import get_api_headers

API_TIMEOUT = 30


@click.group("workers")
def workers_group() -> None:
    """Manage workers (API: /api/v1/workers). Admin actions require motet-admin role."""
    pass


@workers_group.command("readiness")
@api_url_option()
def readiness(api_url: str) -> None:
    """Worker readiness status (GET /api/v1/workers/readiness)."""
    base = normalize_base_url(api_url)
    r = api_request("GET", f"{base}/api/v1/workers/readiness", headers=get_api_headers(), timeout=API_TIMEOUT)
    click.echo(json.dumps(r.json(), indent=2))


@workers_group.command("health")
@api_url_option()
def health(api_url: str) -> None:
    """Worker health status (GET /api/v1/workers/health)."""
    base = normalize_base_url(api_url)
    r = api_request("GET", f"{base}/api/v1/workers/health", headers=get_api_headers(), timeout=API_TIMEOUT)
    click.echo(json.dumps(r.json(), indent=2))


@workers_group.command("managers")
@api_url_option()
def managers_status(api_url: str) -> None:
    """Instance manager status — MCP and Local Inference (GET /api/v1/workers/managers/status)."""
    base = normalize_base_url(api_url)
    r = api_request("GET", f"{base}/api/v1/workers/managers/status", headers=get_api_headers(), timeout=API_TIMEOUT)
    click.echo(json.dumps(r.json(), indent=2))


@workers_group.command("skill-workspaces")
@click.option("--tenant-id", default=None, help="Optional tenant_id filter")
@api_url_option()
def skill_workspaces(tenant_id: Optional[str], api_url: str) -> None:
    """Skill workspace bindings (GET /api/v1/workspace-containers)."""
    base = normalize_base_url(api_url)
    params = {"tenant_id": tenant_id} if tenant_id else None
    r = api_request(
        "GET",
        f"{base}/api/v1/workspace-containers",
        headers=get_api_headers(),
        params=params,
        timeout=API_TIMEOUT,
    )
    click.echo(json.dumps(r.json(), indent=2))


@workers_group.command("terminate")
@click.argument("worker_id")
@click.option("--reason", default="manual_request", help="Termination reason")
@click.option("--method", default="graceful_shutdown", type=click.Choice(["graceful_shutdown", "immediate", "revoke_tasks"]), help="Termination method")
@click.option("--timeout-seconds", default=60, type=int, help="Timeout for graceful shutdown")
@api_url_option()
def terminate_worker(
    worker_id: str,
    reason: str,
    method: str,
    timeout_seconds: int,
    api_url: str,
) -> None:
    """Terminate a worker (POST /api/v1/workers/{id}/terminate). Requires motet-admin."""
    base = normalize_base_url(api_url)
    r = api_request(
        "POST",
        f"{base}/api/v1/workers/{worker_id}/terminate",
        headers=get_api_headers(),
        json={"reason": reason, "method": method, "timeout_seconds": timeout_seconds},
        timeout=API_TIMEOUT,
    )
    click.echo(json.dumps(r.json(), indent=2))


@workers_group.command("start")
@click.argument("worker_id")
@api_url_option()
def start_worker(worker_id: str, api_url: str) -> None:
    """Start a worker (POST /api/v1/workers/{id}/start)."""
    base = normalize_base_url(api_url)
    r = api_request(
        "POST", f"{base}/api/v1/workers/{worker_id}/start", headers=get_api_headers(), timeout=API_TIMEOUT
    )
    click.echo(json.dumps(r.json(), indent=2))


@workers_group.command("stop")
@click.argument("worker_id")
@api_url_option()
def stop_worker(worker_id: str, api_url: str) -> None:
    """Stop a worker (POST /api/v1/workers/{id}/stop)."""
    base = normalize_base_url(api_url)
    r = api_request(
        "POST", f"{base}/api/v1/workers/{worker_id}/stop", headers=get_api_headers(), timeout=API_TIMEOUT
    )
    click.echo(json.dumps(r.json(), indent=2))


@workers_group.command("restart")
@click.argument("worker_id")
@api_url_option()
def restart_worker(worker_id: str, api_url: str) -> None:
    """Restart a worker (POST /api/v1/workers/{id}/restart)."""
    base = normalize_base_url(api_url)
    r = api_request(
        "POST", f"{base}/api/v1/workers/{worker_id}/restart", headers=get_api_headers(), timeout=API_TIMEOUT
    )
    click.echo(json.dumps(r.json(), indent=2))


@workers_group.command("terminate-unhealthy")
@click.confirmation_option(prompt="Terminate ALL unhealthy workers?")
@api_url_option()
def terminate_unhealthy(api_url: str) -> None:
    """Terminate all unhealthy workers (POST /api/v1/workers/terminate-unhealthy)."""
    base = normalize_base_url(api_url)
    r = api_request(
        "POST", f"{base}/api/v1/workers/terminate-unhealthy", headers=get_api_headers(), timeout=API_TIMEOUT
    )
    click.echo(json.dumps(r.json(), indent=2))


@workers_group.command("termination-history")
@api_url_option()
def termination_history(api_url: str) -> None:
    """Worker termination history (GET /api/v1/workers/termination-history)."""
    base = normalize_base_url(api_url)
    r = api_request(
        "GET", f"{base}/api/v1/workers/termination-history", headers=get_api_headers(), timeout=API_TIMEOUT
    )
    click.echo(json.dumps(r.json(), indent=2))


__all__ = ["workers_group"]
