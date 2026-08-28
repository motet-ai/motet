"""
Motet SDK - Live Tasks CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-12

Description:
    CLI for live orchestration tasks — calls /api/v1/tasks (cooperative
    task cancel). List in-flight tasks, get a live summary, or request
    sticky cancel + push wake for a task tree.

Dependencies:
    - click: CLI framework
    - motet_sdk.cli._api: api_request, normalize_base_url
    - motet_sdk.cli._auth: get_api_headers

Usage:
    motet-cli tasks live [--conversation-id ...] [--include-cancelled]
    motet-cli tasks list                   # alias of live
    motet-cli tasks get <task_id>
    motet-cli tasks cancel <task_id> [--reason ...]
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import click

from ._api import api_request, api_url_option, normalize_base_url
from ._auth import get_api_headers

API_TIMEOUT = 60


@click.group("tasks")
def tasks_group() -> None:
    """Live orchestration tasks (API: /api/v1/tasks)."""
    pass


def _list_live(
    *,
    conversation_id: Optional[str],
    include_cancelled: bool,
    api_url: str,
    json_output: bool,
) -> None:
    base = normalize_base_url(api_url)
    params: Dict[str, Any] = {}
    if conversation_id:
        params["conversation_id"] = conversation_id
    if include_cancelled:
        params["include_cancelled"] = True
    r = api_request(
        "GET",
        f"{base}/api/v1/tasks/live",
        headers=get_api_headers(),
        params=params or None,
        timeout=API_TIMEOUT,
    )
    data = r.json()
    if json_output:
        click.echo(json.dumps(data, indent=2))
        return
    tasks = data.get("tasks") or []
    click.echo(f"count={data.get('count', len(tasks))}")
    for task in tasks:
        if not isinstance(task, dict):
            click.echo(f"  {task}")
            continue
        tid = task.get("task_id") or "?"
        status = task.get("status") or ""
        ctype = task.get("command_type") or ""
        conv = task.get("conversation_id") or ""
        click.echo(
            f"  {tid}  status={status}  command_type={ctype}  conversation_id={conv}"
        )


@tasks_group.command("live")
@click.option("--conversation-id", default=None, help="Optional conversation filter.")
@click.option(
    "--include-cancelled",
    is_flag=True,
    help="Include live-index rows marked cancelled (TTL linger).",
)
@click.option(
    "--json-output",
    is_flag=True,
    help="Print full JSON response instead of a summary table.",
)
@api_url_option()
def list_live_tasks(
    conversation_id: Optional[str],
    include_cancelled: bool,
    json_output: bool,
    api_url: str,
) -> None:
    """List in-flight tasks (GET /api/v1/tasks/live)."""
    _list_live(
        conversation_id=conversation_id,
        include_cancelled=include_cancelled,
        api_url=api_url,
        json_output=json_output,
    )


@tasks_group.command("list")
@click.option("--conversation-id", default=None, help="Optional conversation filter.")
@click.option(
    "--include-cancelled",
    is_flag=True,
    help="Include live-index rows marked cancelled (TTL linger).",
)
@click.option(
    "--json-output",
    is_flag=True,
    help="Print full JSON response instead of a summary table.",
)
@api_url_option()
def list_tasks_alias(
    conversation_id: Optional[str],
    include_cancelled: bool,
    json_output: bool,
    api_url: str,
) -> None:
    """Alias for ``tasks live``."""
    _list_live(
        conversation_id=conversation_id,
        include_cancelled=include_cancelled,
        api_url=api_url,
        json_output=json_output,
    )


@tasks_group.command("get")
@click.argument("task_id")
@api_url_option()
def get_task(task_id: str, api_url: str) -> None:
    """Get a live task summary (GET /api/v1/tasks/{task_id})."""
    base = normalize_base_url(api_url)
    r = api_request(
        "GET",
        f"{base}/api/v1/tasks/{task_id}",
        headers=get_api_headers(),
        timeout=API_TIMEOUT,
    )
    click.echo(json.dumps(r.json(), indent=2))


@tasks_group.command("cancel")
@click.argument("task_id")
@click.option("--reason", default=None, help="Optional operator reason.")
@api_url_option()
def cancel_task(task_id: str, reason: Optional[str], api_url: str) -> None:
    """Cancel a live task (POST /api/v1/tasks/{task_id}/cancel)."""
    base = normalize_base_url(api_url)
    payload: Dict[str, Any] = {}
    if reason:
        payload["reason"] = reason
    r = api_request(
        "POST",
        f"{base}/api/v1/tasks/{task_id}/cancel",
        headers=get_api_headers(),
        json=payload,
        timeout=API_TIMEOUT,
    )
    click.echo(json.dumps(r.json(), indent=2))


__all__ = ["tasks_group"]
