"""
Motet - Debug CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-02-23

Description:
    CLI for debug API (/api/v1/debug). Commands, task flow, task events,
    flow analysis, memory stats/search, traces. Many endpoints require
    MOTET_DEBUG_MODE=true on the API server.

Dependencies:
    - click: CLI framework
    - motet.cli._auth: get_api_headers
    - motet.cli._api: api_request, api_url_option, normalize_base_url

Usage:
    motet-cli debug commands list [--limit 50]
    motet-cli debug command get <command_id>
    motet-cli debug task-flow <task_id>
    motet-cli debug task-events <task_id> [--limit N]
    motet-cli debug flow-analysis <task_id>
    motet-cli debug memory stats
    motet-cli debug memory search --q QUERY [--limit 50]
    motet-cli debug traces list [--limit 20] [--q QUERY]
    motet-cli debug trace get <trace_id>
"""

from __future__ import annotations

import json
from typing import Any, Optional

import click
import requests

from ._api import api_request, api_url_option, normalize_base_url
from ._auth import get_api_headers

API_TIMEOUT = 30


@click.group("debug")
def debug_group() -> None:
    """Debug API: commands, task flow, memory, traces (requires MOTET_DEBUG_MODE=true for many)."""
    pass


@debug_group.group("commands")
def commands_sub() -> None:
    """List or get command debug data (debug mode only)."""
    pass


@commands_sub.command("list")
@click.option("--limit", type=int, default=50, help="Max commands to return (default: 50)")
@api_url_option()
def commands_list(limit: int, api_url: str) -> None:
    """List recent commands for debugging (GET /api/v1/debug/commands)."""
    try:
        base = normalize_base_url(api_url)
        r = api_request(
            "GET",
            f"{base}/api/v1/debug/commands",
            headers=get_api_headers(),
            params={"limit": limit},
            timeout=API_TIMEOUT,
        )
        click.echo(json.dumps(r.json(), indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo("Could not connect to API.", err=True)
        raise SystemExit(1)


@debug_group.command("command")
@click.argument("command_id")
@api_url_option()
def command_get(command_id: str, api_url: str) -> None:
    """Get full command data for a command ID (GET /api/v1/debug/commands/{id})."""
    try:
        base = normalize_base_url(api_url)
        r = api_request(
            "GET",
            f"{base}/api/v1/debug/commands/{command_id}",
            headers=get_api_headers(),
            timeout=API_TIMEOUT,
        )
        click.echo(json.dumps(r.json(), indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo("Could not connect to API.", err=True)
        raise SystemExit(1)


@debug_group.command("task-flow")
@click.argument("task_id")
@api_url_option()
def task_flow(task_id: str, api_url: str) -> None:
    """Get command execution flow for a task (GET /api/v1/debug/task-flow/{task_id})."""
    try:
        base = normalize_base_url(api_url)
        r = api_request(
            "GET",
            f"{base}/api/v1/debug/task-flow/{task_id}",
            headers=get_api_headers(),
            timeout=API_TIMEOUT,
        )
        click.echo(json.dumps(r.json(), indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo("Could not connect to API.", err=True)
        raise SystemExit(1)


@debug_group.command("task-events")
@click.argument("task_id")
@click.option("--limit", type=int, default=None, help="Max events to return")
@api_url_option()
def task_events(task_id: str, limit: Optional[int], api_url: str) -> None:
    """Get events for a task (GET /api/v1/debug/task-events/{task_id})."""
    try:
        base = normalize_base_url(api_url)
        params = {}
        if limit is not None:
            params["limit"] = limit
        r = api_request(
            "GET",
            f"{base}/api/v1/debug/task-events/{task_id}",
            headers=get_api_headers(),
            params=params or None,
            timeout=API_TIMEOUT,
        )
        click.echo(json.dumps(r.json(), indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo("Could not connect to API.", err=True)
        raise SystemExit(1)


@debug_group.command("flow-analysis")
@click.argument("task_id")
@api_url_option()
def flow_analysis(task_id: str, api_url: str) -> None:
    """Analyze command flow for a task (GET /api/v1/debug/command-flow/analysis/{task_id})."""
    try:
        base = normalize_base_url(api_url)
        r = api_request(
            "GET",
            f"{base}/api/v1/debug/command-flow/analysis/{task_id}",
            headers=get_api_headers(),
            timeout=API_TIMEOUT,
        )
        click.echo(json.dumps(r.json(), indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo("Could not connect to API.", err=True)
        raise SystemExit(1)


@debug_group.group("memory")
def memory_sub() -> None:
    """Memory stats and search (debug API)."""
    pass


@memory_sub.command("stats")
@api_url_option()
def memory_stats(api_url: str) -> None:
    """Get memory statistics (GET /api/v1/debug/memory/stats)."""
    try:
        base = normalize_base_url(api_url)
        r = api_request(
            "GET",
            f"{base}/api/v1/debug/memory/stats",
            headers=get_api_headers(),
            timeout=API_TIMEOUT,
        )
        click.echo(json.dumps(r.json(), indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo("Could not connect to API.", err=True)
        raise SystemExit(1)


@memory_sub.command("search")
@click.option("--q", "query", required=True, help="Search query (content or tags)")
@click.option("--limit", type=int, default=50, help="Max results (default: 50)")
@api_url_option()
def memory_search(query: str, limit: int, api_url: str) -> None:
    """Search memories (GET /api/v1/debug/memory/search)."""
    try:
        base = normalize_base_url(api_url)
        r = api_request(
            "GET",
            f"{base}/api/v1/debug/memory/search",
            headers=get_api_headers(),
            params={"q": query, "limit": limit},
            timeout=API_TIMEOUT,
        )
        click.echo(json.dumps(r.json(), indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo("Could not connect to API.", err=True)
        raise SystemExit(1)


@debug_group.group("traces")
def traces_sub() -> None:
    """List or get traces (debug API JSON)."""
    pass


@traces_sub.command("list")
@click.option("--limit", type=int, default=20, help="Max traces (default: 20)")
@click.option("--q", "query", default=None, help="Filter by trace_id search")
@api_url_option()
def traces_list(limit: int, query: Optional[str], api_url: str) -> None:
    """List recent traces (GET /api/v1/debug/traces.json)."""
    try:
        base = normalize_base_url(api_url)
        params: dict[str, Any] = {"limit": limit}
        if query:
            params["q"] = query
        r = api_request(
            "GET",
            f"{base}/api/v1/debug/traces.json",
            headers=get_api_headers(),
            params=params,
            timeout=API_TIMEOUT,
        )
        click.echo(json.dumps(r.json(), indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo("Could not connect to API.", err=True)
        raise SystemExit(1)


@debug_group.command("trace")
@click.argument("trace_id")
@api_url_option()
def trace_get(trace_id: str, api_url: str) -> None:
    """Get a trace by ID as JSON (GET /api/v1/debug/traces/{trace_id}.json)."""
    try:
        base = normalize_base_url(api_url)
        r = api_request(
            "GET",
            f"{base}/api/v1/debug/traces/{trace_id}.json",
            headers=get_api_headers(),
            timeout=API_TIMEOUT,
        )
        click.echo(json.dumps(r.json(), indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo("Could not connect to API.", err=True)
        raise SystemExit(1)


__all__ = ["debug_group"]
