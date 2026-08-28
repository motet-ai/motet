"""
Motet - Schedules CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-02-23

Description:
    CLI for schedule management — calls /api/v1/schedules.
    List, create, get, cancel, delete, suspend, resume, command-types, stats.

Dependencies:
    - click: CLI framework
    - motet.cli._auth: get_api_headers
    - motet.cli._api: api_request, normalize_base_url

Usage:
    motet-cli schedules list
    motet-cli schedules command-types
    motet-cli schedules stats
    motet-cli schedules get <schedule_id>
    motet-cli schedules create --command-type PingCommand --schedule-type recurring --cron "0 * * * *"
    motet-cli schedules cancel <schedule_id>
    motet-cli schedules delete <schedule_id>
    motet-cli schedules suspend <schedule_id>
    motet-cli schedules resume <schedule_id>
"""

from __future__ import annotations

import json
from typing import Any, Optional

import click

from ._api import api_request, api_url_option, normalize_base_url
from ._auth import get_api_headers

API_TIMEOUT = 60


@click.group("schedules")
def schedules_group() -> None:
    """Manage scheduled commands (API: /api/v1/schedules)."""
    pass


@schedules_group.command("list")
@click.option("--status", help="Filter by status (active, paused, completed, failed)")
@click.option("--schedule-type", help="Filter by type (immediate, delayed, recurring, conditional)")
@click.option("--limit", default=50, type=int, help="Max schedules to return (default: 50)")
@click.option("--offset", default=0, type=int, help="Offset for pagination (default: 0)")
@api_url_option()
def list_schedules(
    status: Optional[str],
    schedule_type: Optional[str],
    limit: int,
    offset: int,
    api_url: str,
) -> None:
    """List schedules (GET /api/v1/schedules)."""
    base = normalize_base_url(api_url)
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if status:
        params["status"] = status
    if schedule_type:
        params["schedule_type"] = schedule_type
    r = api_request(
        "GET", f"{base}/api/v1/schedules/", headers=get_api_headers(), params=params, timeout=API_TIMEOUT
    )
    data = r.json()
    total = data.get("total_schedules", 0)
    schedules = data.get("schedules", [])
    click.echo(f"Total: {total}\n")
    for s in schedules:
        click.echo(f"  {s.get('schedule_id')}  {s.get('schedule_type')}  {s.get('status')}  {s.get('command_type')}  next={s.get('next_execution_at') or '-'}")


@schedules_group.command("command-types")
@api_url_option()
def command_types(api_url: str) -> None:
    """List available command types for scheduling (GET /api/v1/schedules/command-types)."""
    base = normalize_base_url(api_url)
    r = api_request("GET", f"{base}/api/v1/schedules/command-types", headers=get_api_headers(), timeout=API_TIMEOUT)
    data = r.json()
    for ct in data.get("command_types", []):
        click.echo(f"  {ct.get('type')}: {ct.get('description', '')[:60]}")


@schedules_group.command("stats")
@api_url_option()
def stats(api_url: str) -> None:
    """Schedule statistics (GET /api/v1/schedules/stats/summary)."""
    base = normalize_base_url(api_url)
    r = api_request("GET", f"{base}/api/v1/schedules/stats/summary", headers=get_api_headers(), timeout=API_TIMEOUT)
    data = r.json()
    click.echo(json.dumps(data, indent=2))


@schedules_group.command("get")
@click.argument("schedule_id")
@api_url_option()
def get_schedule(schedule_id: str, api_url: str) -> None:
    """Get schedule details (GET /api/v1/schedules/{id})."""
    base = normalize_base_url(api_url)
    r = api_request(
        "GET", f"{base}/api/v1/schedules/{schedule_id}", headers=get_api_headers(), timeout=API_TIMEOUT
    )
    click.echo(json.dumps(r.json(), indent=2))


@schedules_group.command("create")
@click.option("--command-type", required=True, help="Command type (e.g. PingCommand)")
@click.option("--command-data", default="{}", help="JSON command data (default: {})")
@click.option("--schedule-type", required=True, type=click.Choice(["immediate", "delayed", "recurring", "conditional"]), help="Schedule type")
@click.option("--name", help="Human-readable name")
@click.option("--cron", help="Cron expression (for recurring)")
@click.option("--interval-seconds", type=int, help="Interval in seconds (for recurring)")
@click.option("--scheduled-at", help="ISO datetime (for delayed)")
@click.option("--timeout-seconds", default=300, type=int, help="Command timeout (default: 300)")
@click.option("--priority", default=5, type=int, help="Priority (default: 5)")
@api_url_option()
def create_schedule(
    command_type: str,
    command_data: str,
    schedule_type: str,
    name: Optional[str],
    cron: Optional[str],
    interval_seconds: Optional[int],
    scheduled_at: Optional[str],
    timeout_seconds: int,
    priority: int,
    api_url: str,
) -> None:
    """Create a schedule (POST /api/v1/schedules)."""
    base = normalize_base_url(api_url)
    payload = {
        "command_type": command_type,
        "command_data": json.loads(command_data),
        "schedule_type": schedule_type,
        "timeout_seconds": timeout_seconds,
        "priority": priority,
    }
    if name:
        payload["name"] = name
    if cron:
        payload["cron_expression"] = cron
    if interval_seconds is not None:
        payload["interval_seconds"] = interval_seconds
    if scheduled_at:
        payload["scheduled_at"] = scheduled_at
    r = api_request(
        "POST", f"{base}/api/v1/schedules/", headers=get_api_headers(), json=payload, timeout=API_TIMEOUT
    )
    data = r.json()
    click.echo(f"✅ Schedule created: {data.get('schedule_id')}")


@schedules_group.command("cancel")
@click.argument("schedule_id")
@api_url_option()
def cancel_schedule(schedule_id: str, api_url: str) -> None:
    """Cancel a schedule (DELETE /api/v1/schedules/{id})."""
    base = normalize_base_url(api_url)
    api_request(
        "DELETE", f"{base}/api/v1/schedules/{schedule_id}", headers=get_api_headers(), timeout=API_TIMEOUT
    )
    click.echo("✅ Schedule cancelled.")


@schedules_group.command("delete")
@click.argument("schedule_id")
@api_url_option()
def delete_schedule(schedule_id: str, api_url: str) -> None:
    """Permanently delete a schedule (DELETE /api/v1/schedules/{id}/delete)."""
    base = normalize_base_url(api_url)
    api_request(
        "DELETE", f"{base}/api/v1/schedules/{schedule_id}/delete", headers=get_api_headers(), timeout=API_TIMEOUT
    )
    click.echo("✅ Schedule deleted.")


@schedules_group.command("suspend")
@click.argument("schedule_id")
@api_url_option()
def suspend_schedule(schedule_id: str, api_url: str) -> None:
    """Suspend a schedule (POST /api/v1/schedules/{id}/suspend)."""
    base = normalize_base_url(api_url)
    api_request(
        "POST", f"{base}/api/v1/schedules/{schedule_id}/suspend", headers=get_api_headers(), timeout=API_TIMEOUT
    )
    click.echo("✅ Schedule suspended.")


@schedules_group.command("resume")
@click.argument("schedule_id")
@api_url_option()
def resume_schedule(schedule_id: str, api_url: str) -> None:
    """Resume a suspended schedule (POST /api/v1/schedules/{id}/resume)."""
    base = normalize_base_url(api_url)
    api_request(
        "POST", f"{base}/api/v1/schedules/{schedule_id}/resume", headers=get_api_headers(), timeout=API_TIMEOUT
    )
    click.echo("✅ Schedule resumed.")


__all__ = ["schedules_group"]
