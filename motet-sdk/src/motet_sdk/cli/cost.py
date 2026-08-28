"""
Motet - Cost CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-02-23

Description:
    CLI for cost tracking — calls /api/v1/cost.
    Summary, by-principal, usage, budget get/set, events.

Dependencies:
    - click: CLI framework
    - motet.cli._auth: get_api_headers
    - motet.cli._api: api_request, normalize_base_url

Usage:
    motet-cli cost summary [--date YYYY-MM-DD] [--tenant-id ...]
    motet-cli cost summary-by-principal [--date ...] [--tenant-id ...]
    motet-cli cost usage [--date ...] [--tenant-id ...]
    motet-cli cost budget
    motet-cli cost budget-set [--daily-limit USD] [--monthly-limit USD] [--alert-threshold PCT]
    motet-cli cost events [--count N] [--start-id ...] [--tenant-id ...]
"""

from __future__ import annotations

import json
from typing import Optional

import click

from ._api import api_request, api_url_option, normalize_base_url
from ._auth import get_api_headers

API_TIMEOUT = 30


@click.group("cost")
def cost_group() -> None:
    """Cost tracking and budget (API: /api/v1/cost)."""
    pass


@cost_group.command("summary")
@click.option("--date", help="Date YYYY-MM-DD (default: today)")
@click.option("--tenant-id", help="Tenant ID (default: current). Use 'motet-global' for motet-level)")
@api_url_option()
def summary(date: Optional[str], tenant_id: Optional[str], api_url: str) -> None:
    """Daily cost summary (GET /api/v1/cost/summary)."""
    base = normalize_base_url(api_url)
    params = {}
    if date:
        params["date"] = date
    if tenant_id:
        params["tenant_id"] = tenant_id
    r = api_request(
        "GET", f"{base}/api/v1/cost/summary", headers=get_api_headers(), params=params, timeout=API_TIMEOUT
    )
    click.echo(json.dumps(r.json(), indent=2))


@cost_group.command("summary-by-principal")
@click.option("--date", help="Date YYYY-MM-DD (default: today)")
@click.option("--tenant-id", help="Tenant ID (default: current)")
@api_url_option()
def summary_by_principal(date: Optional[str], tenant_id: Optional[str], api_url: str) -> None:
    """Daily cost by principal (GET /api/v1/cost/summary/by_principal)."""
    base = normalize_base_url(api_url)
    params = {}
    if date:
        params["date"] = date
    if tenant_id:
        params["tenant_id"] = tenant_id
    r = api_request(
        "GET",
        f"{base}/api/v1/cost/summary/by_principal",
        headers=get_api_headers(),
        params=params,
        timeout=API_TIMEOUT,
    )
    click.echo(json.dumps(r.json(), indent=2))


@cost_group.command("usage")
@click.option("--date", help="Date YYYY-MM-DD (default: today)")
@click.option("--tenant-id", help="Tenant ID (default: current)")
@api_url_option()
def usage(date: Optional[str], tenant_id: Optional[str], api_url: str) -> None:
    """Usage summary with budget status (GET /api/v1/cost/usage)."""
    base = normalize_base_url(api_url)
    params = {}
    if date:
        params["date"] = date
    if tenant_id:
        params["tenant_id"] = tenant_id
    r = api_request(
        "GET", f"{base}/api/v1/cost/usage", headers=get_api_headers(), params=params, timeout=API_TIMEOUT
    )
    click.echo(json.dumps(r.json(), indent=2))


@cost_group.command("budget")
@api_url_option()
def budget_get(api_url: str) -> None:
    """Get budget configuration (GET /api/v1/cost/budget)."""
    base = normalize_base_url(api_url)
    r = api_request(
        "GET", f"{base}/api/v1/cost/budget", headers=get_api_headers(), timeout=API_TIMEOUT
    )
    click.echo(json.dumps(r.json(), indent=2))


@cost_group.command("budget-set")
@click.option("--daily-limit", type=float, help="Daily limit USD")
@click.option("--monthly-limit", type=float, help="Monthly limit USD")
@click.option("--alert-threshold", type=float, help="Alert at percent (0-100)")
@api_url_option()
def budget_set(
    daily_limit: Optional[float],
    monthly_limit: Optional[float],
    alert_threshold: Optional[float],
    api_url: str,
) -> None:
    """Update budget (PUT /api/v1/cost/budget). Requires admin role."""
    base = normalize_base_url(api_url)
    payload = {}
    if daily_limit is not None:
        payload["daily_limit_usd"] = daily_limit
    if monthly_limit is not None:
        payload["monthly_limit_usd"] = monthly_limit
    if alert_threshold is not None:
        payload["alert_threshold_pct"] = alert_threshold
    if not payload:
        raise click.UsageError("Specify at least one of --daily-limit, --monthly-limit, --alert-threshold")
    r = api_request(
        "PUT", f"{base}/api/v1/cost/budget", headers=get_api_headers(), json=payload, timeout=API_TIMEOUT
    )
    click.echo(json.dumps(r.json(), indent=2))


@cost_group.command("events")
@click.option("--count", default=100, type=int, help="Max events (default: 100)")
@click.option("--start-id", default="+", help="Redis stream start ID (default: +)")
@click.option("--tenant-id", help="Tenant ID (default: current)")
@api_url_option()
def events(count: int, start_id: str, tenant_id: Optional[str], api_url: str) -> None:
    """Cost events (GET /api/v1/cost/events)."""
    base = normalize_base_url(api_url)
    params = {"count": count, "start_id": start_id}
    if tenant_id:
        params["tenant_id"] = tenant_id
    r = api_request(
        "GET", f"{base}/api/v1/cost/events", headers=get_api_headers(), params=params, timeout=API_TIMEOUT
    )
    click.echo(json.dumps(r.json(), indent=2))


__all__ = ["cost_group"]
