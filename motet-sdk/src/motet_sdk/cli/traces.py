"""
Motet - Traces CLI

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-19

Description:
    CLI commands for trace operations (local file operations).
    These operations work with local trace files and do not have API equivalents.

Dependencies:
    - click: CLI framework
    - motet.core.tracing: Trace management

Usage:
    motet-cli traces list --limit 10           # List recent traces
    motet-cli traces show --trace-id <id>      # Show specific trace
    motet-cli traces replay --trace-id <id>    # Replay trace
    motet-cli traces watch --duration 10       # Watch live events

Notes:
    - CLI-only operations (no API equivalent)
    - Works with local trace files
"""

import json
import time as _t

import click

# Import shared logging configuration
from ._logging import logger


@click.group("traces")
def traces_group() -> None:
    """Trace operations."""
    pass


@traces_group.command("list")
@click.option("--limit", type=int, default=10, help="Max traces to list")
def list_traces(limit: int) -> None:
    """List recent traces."""
    from motet.core import tracing
    
    items = tracing.list_traces(limit=limit)
    if not items:
        click.echo("No traces found (enable with MOTET_TRACE_ENABLED=true)")
        return
    for it in items:
        click.echo(f"{it['trace_id']}\t{int(it['bytes'])}B\t{int(it['modified'])}")


@traces_group.command("show")
@click.option("--trace-id", required=True, help="Trace ID to show")
def show_trace(trace_id: str) -> None:
    """Show a specific trace as JSONL."""
    from motet.core import tracing
    
    events = tracing.load_trace(trace_id)
    if not events:
        click.echo("Trace not found")
        return
    for ev in events:
        click.echo(ev)


@traces_group.command("watch")
@click.option("--duration", type=float, default=10.0, help="Seconds to watch plan/step/end events")
def watch_events(duration: float) -> None:
    """Print plan/step/end events for a short duration.

    Preferred source is Redis pub/sub (``PSUBSCRIBE *:events:channel`` so
    tenant and platform EventBus channels are visible). Falls back to this
    process's in-memory global bus history when Redis pub/sub is unavailable.
    """
    from motet.core.observability.logging import setup_logging

    setup_logging()
    end = _t.time() + duration

    def _emit_if_relevant(event: dict) -> None:
        kind = event.get("kind")
        if kind in ("plan", "step", "end"):
            click.echo(f"{kind}: {event}")

    # First try distributed event stream via Redis pub/sub.
    try:
        from motet.core.distributed.redis_manager import get_sync_redis_client
        from motet.core.distributed.tenant_keys import EVENT_BUS_PSUBSCRIBE_PATTERN

        redis_client = get_sync_redis_client("traces_watch")
        pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
        pubsub.psubscribe(EVENT_BUS_PSUBSCRIBE_PATTERN)
        try:
            while _t.time() < end:
                message = pubsub.get_message(timeout=0.2)
                if not message or message.get("type") not in ("message", "pmessage"):
                    continue
                data = message.get("data")
                if isinstance(data, bytes):
                    payload = data.decode("utf-8", errors="ignore")
                elif isinstance(data, str):
                    payload = data
                else:
                    continue
                try:
                    event = json.loads(payload)
                except Exception:
                    continue
                if isinstance(event, dict):
                    _emit_if_relevant(event)
            return
        finally:
            pubsub.close()
    except Exception as exc:
        logger.warning("traces watch redis pubsub unavailable; falling back to local bus: %s", str(exc))

    # Fallback: local process event history only.
    from motet.core.workers.events import global_bus

    last_len = 0
    while _t.time() < end:
        recent = global_bus.get_recent_events(limit=1000)
        if len(recent) > last_len:
            for event in recent[last_len:]:
                if isinstance(event, dict):
                    _emit_if_relevant(event)
            last_len = len(recent)
        _t.sleep(0.2)


@traces_group.command("replay")
@click.option("--trace-id", required=True, help="Trace ID to replay")
def replay_trace(trace_id: str) -> None:
    """Replay a trace: prints planned tool names and verifies step sequence parity."""
    from motet.core import tracing
    
    events = tracing.load_trace(trace_id)
    if not events:
        click.echo("Trace not found")
        return
    plan = next((e for e in events if e.get("kind") == "plan"), None)
    steps = [e for e in events if e.get("kind") == "step"]
    planned = [n for n in (plan.get("names") if plan else [])]
    actual = [s.get("name") for s in steps]
    click.echo(f"planned: {planned}")
    click.echo(f"actual:  {actual}")
    if planned and planned == actual:
        click.echo("parity: OK")
    else:
        click.echo("parity: MISMATCH")

