"""
Motet - Current Time Builtin

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Wall-clock / timezone tool for the Motet distributed framework.
    Returns the current datetime so agents can reason about absolute times,
    timezone conversion, and scheduling without guessing. Complements issue #131
    (date kept out of the cacheable agentic system prompt) and delayed scheduling
    via ``delay_seconds`` on ``core.schedule_command``.

Dependencies:
    - datetime / zoneinfo: Wall-clock time and IANA timezone conversion
    - pydantic: Parameter validation
    - Tool registry and protocol system

Usage:
    from motet.core.tools.builtin.current_time import run

    result = run({})
    # {"status": "success", "result": {"iso_utc": "2026-07-27T19:30:00Z", ...}}

    result = run({"timezone": "America/New_York"})

Notes:
    - Synchronous for Celery worker pool compatibility
    - Defaults to UTC; optional IANA timezone for local wall-clock
    - Prefer ``delay_seconds`` on delayed schedules over computing absolute times
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from ..protocol import ok, err
from ..registry import ToolRegistry


class Params(BaseModel):
    timezone: Optional[str] = Field(
        default="UTC",
        description="IANA timezone name (e.g. 'UTC', 'America/New_York'). Defaults to UTC.",
    )


def _parse(ln: str, trig: str) -> Dict[str, Any]:
    tz = ln[len(trig) :].strip()
    return {"timezone": tz} if tz else {}


def _fmt(res: Dict[str, Any]) -> str:
    if res.get("status") != "success":
        return f"current_time(error={res.get('error')})"
    result = res.get("result") or {}
    return f"current_time(iso_utc={result.get('iso_utc')}, timezone={result.get('timezone')})"


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Return the current wall-clock time (synchronous; ADR-0033)."""
    try:
        parsed = Params(**(params or {}))
    except Exception as exc:
        return err(f"validation error: {exc}")

    tz_name = (parsed.timezone or "UTC").strip() or "UTC"
    now_utc = datetime.now(timezone.utc)

    if tz_name.upper() == "UTC":
        local = now_utc
        resolved_tz = "UTC"
    else:
        try:
            local = now_utc.astimezone(ZoneInfo(tz_name))
            resolved_tz = tz_name
        except ZoneInfoNotFoundError:
            return err(
                f"unknown timezone: {tz_name}. Use an IANA name such as 'UTC' or 'America/New_York'."
            )
        except Exception as exc:
            return err(f"timezone conversion failed: {exc}")

    return ok(
        {
            "iso_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "iso_local": local.isoformat(),
            "unix_timestamp": int(now_utc.timestamp()),
            "timezone": resolved_tz,
            "human_utc": now_utc.strftime("%B %d, %Y %H:%M:%S UTC"),
            "human_local": local.strftime("%B %d, %Y %H:%M:%S %Z"),
            "date_utc": now_utc.strftime("%Y-%m-%d"),
        }
    )


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.current_time",
        description=(
            "Return the current wall-clock datetime (UTC and optional IANA timezone). "
            "Use when you need an accurate now for absolute ISO timestamps, timezone "
            "conversion, or date reasoning. For one-shot delayed Motet schedules, prefer "
            "core.schedule_command with delay_seconds instead of computing scheduled_at."
        ),
        func=run,
        tool_schema=Params,
        triggers=["time:", "current_time:", "now:"],
        priority=2,
        parse_params=_parse,
        observation_formatter=_fmt,
        category="system",
        default_timeout_seconds=5.0,
        suggested_max_calls=5,
        cost_class="low",
        keywords=[
            "time",
            "date",
            "clock",
            "datetime",
            "timezone",
            "utc",
            "now",
            "iso8601",
            "wall clock",
            "current time",
        ],
    )


__all__ = ["register", "run"]
