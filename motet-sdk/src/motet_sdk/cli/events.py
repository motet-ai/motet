"""
Motet - Events CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-02-23

Description:
    CLI for events — calls /api/v1/events.
    Stream SSE events, get stats.

Dependencies:
    - click: CLI framework
    - requests: HTTP (stream)
    - motet.cli._auth: get_api_headers
    - motet.cli._api: api_request, normalize_base_url

Usage:
    motet-cli events stream [--event-kinds kind1,kind2] [--unpack-result]
    motet-cli events stats
"""

from __future__ import annotations

import json
from typing import Optional

import click
import requests

from ._api import api_request, api_url_option, normalize_base_url
from ._auth import get_api_headers

API_TIMEOUT = 30


@click.group("events")
def events_group() -> None:
    """Event stream and stats (API: /api/v1/events)."""
    pass


@events_group.command("stream")
@click.option("--event-kinds", help="Comma-separated event kinds to filter")
@click.option("--unpack-result", is_flag=True, help="Unpack result.data from completion events")
@api_url_option()
def stream_events(
    event_kinds: Optional[str],
    unpack_result: bool,
    api_url: str,
) -> None:
    """Stream real-time events (GET /api/v1/events, SSE). Press Ctrl+C to stop."""
    base = normalize_base_url(api_url)
    params = {"unpack_result": "true" if unpack_result else "false"}
    if event_kinds:
        params["event_kinds"] = event_kinds
    url = f"{base}/api/v1/events"
    headers = get_api_headers()
    try:
        r = api_request("GET", url, headers=headers, params=params, stream=True, timeout=API_TIMEOUT)
        for line in r.iter_lines(decode_unicode=True):
            if line and line.startswith("data:"):
                click.echo(line)
    except click.ClickException:
        raise
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"Request error: {e}")
    except KeyboardInterrupt:
        click.echo("\nStopped.")


@events_group.command("stats")
@api_url_option()
def stats(api_url: str) -> None:
    """Event statistics (GET /api/v1/events/stats)."""
    base = normalize_base_url(api_url)
    r = api_request(
        "GET", f"{base}/api/v1/events/stats", headers=get_api_headers(), timeout=API_TIMEOUT
    )
    click.echo(json.dumps(r.json(), indent=2))


__all__ = ["events_group"]
