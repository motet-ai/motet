"""
Motet - Tool Observation Cache-Control

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-23

Description:
    Response-level freshness for tool observations, shaped like HTTP
    Cache-Control. A tool result may say the observation is ``no-store``
    (retry is allowed) or fresh for this turn / a max-age. The agentic
    loop honors a fresh entry by replaying a short cached notice instead
    of re-executing the same signature. Default is no-store: missing
    metadata never tightens the rail.

    Snapshot built-ins (``core.http_get``, ``core.http_get_browser``,
    ``core.web_search``) attach a directive on success. Origin HTTP
    Cache-Control is not forwarded — CDN ``no-cache`` is the opposite of
    what the agent needs.

Dependencies:
    - pydantic: Wire/validate the directive
    - time: max-age freshness

Usage:
    from motet.core.tools.cache_control import (
        NO_STORE,
        attach_snapshot_cache_control,
        resolve_cache_control,
    )

    payload = attach_snapshot_cache_control("core.http_get", {
        "status": 200, "text": page,
    })
    cc = resolve_cache_control("core.http_get", payload)

    Notes:
    - This is freshness, not a stall rail. A cache hit is cheap; shopping
      new URLs is unchanged.
    - ``inherit_snapshot_cache`` copies child snapshot-tool *keys* onto
      a parent after ``core.spawn_agents``. The parent does not get the
      child's page body — that lives in the child's loop. An inherited
      hit is a refetch veto that points at the spawn observation. When
      the child offloaded the fetch, the entry also carries
      ``artifact_id`` so the parent can ``artifact_read`` that page
      instead of hitting the network. File / MCP / write tools are not
      inherited.
    - ``http_post`` is not a snapshot tool and must not get an age.
    - Do not store the page body in loop state — only the directive
      and, when present, the child's tool ``artifact_id``.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

SNAPSHOT_TOOL_NAMES = frozenset(
    {
        "core.http_get",
        "core.http_get_browser",
        "core.web_search",
    }
)
MIN_SNAPSHOT_CHARS = 80
CACHE_NOTICE_PREFIX = "[cached]"
INHERITED_FROM_SPAWN = "spawn"


class ToolCacheControl(BaseModel):
    """Freshness directive on one tool observation."""

    no_store: bool = False
    same_turn: bool = False
    max_age_seconds: Optional[int] = Field(default=None, ge=0)

    @field_validator("max_age_seconds", mode="before")
    @classmethod
    def _empty_max_age(cls, v: Any) -> Any:
        if v == "" or v is False:
            return None
        return v

    def is_cacheable(self) -> bool:
        """True when a later identical call may reuse this observation."""
        if self.no_store:
            return False
        if self.same_turn:
            return True
        return bool(self.max_age_seconds and self.max_age_seconds > 0)

    def is_fresh(self, stored_at: float, now: float) -> bool:
        """True when the stored observation is still usable at *now*."""
        if not self.is_cacheable():
            return False
        if self.same_turn:
            return True
        age = self.max_age_seconds or 0
        return (now - stored_at) < age

    def to_wire(self) -> Dict[str, Any]:
        """JSON-friendly dict for tool payloads and checkpoints."""
        return self.model_dump(exclude_none=True)


NO_STORE = ToolCacheControl(no_store=True)
SAME_TURN = ToolCacheControl(same_turn=True)


class ObservationCacheEntry(BaseModel):
    """Per-signature freshness record. Does not store the observation body."""

    tool_name: str
    cache_control: ToolCacheControl
    stored_at: float
    inherited_from: Optional[str] = Field(
        default=None,
        description=(
            "When set to ``spawn``, this key came from a child loop. "
            "The page body is not in this transcript; a hit must point "
            "at the spawn_agents observation, not a local observation."
        ),
    )
    artifact_id: Optional[str] = Field(
        default=None,
        description=(
            "Tool-result artifact for the child's fetch, when one was "
            "stored. A parent hit can artifact_read this instead of "
            "refetching. Absent when the observation stayed inline."
        ),
    )


def parse_cache_control(raw: Any) -> ToolCacheControl:
    """Parse a dict, model, or HTTP-ish string into a directive."""
    if isinstance(raw, ToolCacheControl):
        return raw
    if isinstance(raw, str):
        return _parse_http_style(raw)
    if isinstance(raw, dict):
        return ToolCacheControl.model_validate(raw)
    return NO_STORE


def _parse_http_style(raw: str) -> ToolCacheControl:
    """Accept ``no-store``, ``same-turn``, ``max-age=N``, comma-separated."""
    no_store = False
    same_turn = False
    max_age: Optional[int] = None
    for part in raw.split(","):
        token = part.strip().lower()
        if not token:
            continue
        if token in {"no-store", "nostore", "no_store"}:
            no_store = True
            continue
        if token in {"same-turn", "sameturn", "same_turn"}:
            same_turn = True
            continue
        if token.startswith("max-age=") or token.startswith("max_age="):
            try:
                max_age = int(token.split("=", 1)[1].strip())
            except ValueError:
                max_age = 0
    if no_store or max_age == 0:
        return NO_STORE
    return ToolCacheControl(no_store=False, same_turn=same_turn, max_age_seconds=max_age)


def extract_cache_control(payload: Any) -> Optional[ToolCacheControl]:
    """Read an explicit ``cache_control`` off a tool payload, if present."""
    if not isinstance(payload, dict):
        return None
    raw = payload.get("cache_control")
    if raw is None and isinstance(payload.get("meta"), dict):
        raw = payload["meta"].get("cache_control")
    if raw is None and isinstance(payload.get("result"), dict):
        inner = payload["result"]
        raw = inner.get("cache_control")
        if raw is None and isinstance(inner.get("meta"), dict):
            raw = inner["meta"].get("cache_control")
    if raw is None:
        return None
    return parse_cache_control(raw)


def _body_text(payload: Dict[str, Any]) -> str:
    """Best-effort text used only to decide if a snapshot is usable."""
    parts: list[str] = []
    for key in ("text", "main_content", "content", "summary", "title"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    result = payload.get("result")
    if isinstance(result, str) and result.strip():
        parts.append(result)
    elif isinstance(result, dict):
        parts.append(_body_text(result))
    return "\n".join(parts)


def snapshot_default(tool_name: str, payload: Any) -> ToolCacheControl:
    """Default directive for a snapshot tool that omitted ``cache_control``."""
    if tool_name not in SNAPSHOT_TOOL_NAMES or not isinstance(payload, dict):
        return NO_STORE
    if payload.get("status") == "error" or payload.get("error"):
        return NO_STORE
    status = payload.get("status")
    if isinstance(status, int) and not (200 <= status < 400):
        return NO_STORE
    if tool_name == "core.web_search":
        results = payload.get("results")
        if results is None:
            results = payload.get("data")
        if not (isinstance(results, list) and results):
            return NO_STORE
        return SAME_TURN
    if len(_body_text(payload).strip()) < MIN_SNAPSHOT_CHARS:
        return NO_STORE
    return SAME_TURN


def resolve_cache_control(tool_name: str, payload: Any) -> ToolCacheControl:
    """Explicit directive wins; snapshot tools fall back to a default."""
    explicit = extract_cache_control(payload)
    if explicit is not None:
        return explicit
    return snapshot_default(tool_name, payload)


def attach_snapshot_cache_control(tool_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Stamp ``cache_control`` on a snapshot success payload if missing."""
    if "cache_control" in payload:
        return payload
    payload["cache_control"] = resolve_cache_control(tool_name, payload).to_wire()
    return payload


def cached_observation_text(
    tool_name: str,
    *,
    inherited_from: Optional[str] = None,
    artifact_id: Optional[str] = None,
) -> str:
    """Notice for a fresh cache hit.

    A local hit assumes the earlier tool observation is still in this
    transcript. An inherited spawn hit does not — the child loop held
    the page. Point at the spawn_agents observation, and at the child's
    tool artifact when one was stored, instead of a missing body.
    """
    aid = (artifact_id or "").strip()
    pointer = (
        f" Raw fetch: artifact_id={aid}; use core.artifact_read if you "
        "need the page."
        if aid
        else ""
    )
    if inherited_from == INHERITED_FROM_SPAWN:
        return (
            f"{CACHE_NOTICE_PREFIX} A sub-agent already fetched this "
            f"{tool_name} call with these arguments. Use the "
            f"core.spawn_agents observation.{pointer} Do not refetch."
        )
    return (
        f"{CACHE_NOTICE_PREFIX} Fresh. Same result as the earlier {tool_name} "
        f"call with these arguments. Use that observation.{pointer} "
        "Do not refetch."
    )


def take_fresh_cache_hit(
    cache: Dict[str, Any],
    signature: str,
    *,
    now: Optional[float] = None,
    executed_signatures: Optional[List[str]] = None,
) -> Optional[ObservationCacheEntry]:
    """Return the entry when *signature* is still fresh, else None.

    ``executed_signatures`` is the resume-safe gate: a client that summarized
    the transcript drops the prior observation, and we must not 304 a body
    that is no longer above (same rule as signature derivation).
    """
    if not signature:
        return None
    if executed_signatures is not None and signature not in executed_signatures:
        return None
    raw = cache.get(signature)
    if raw is None:
        return None
    try:
        entry = (
            raw
            if isinstance(raw, ObservationCacheEntry)
            else ObservationCacheEntry.model_validate(raw)
        )
    except Exception:
        return None
    clock = time.time() if now is None else now
    if not entry.cache_control.is_fresh(entry.stored_at, clock):
        return None
    return entry


def inherit_snapshot_cache(
    dest_cache: Dict[str, Any],
    dest_signatures: List[str],
    source_cache: Any,
    source_signatures: Any,
) -> int:
    """Copy snapshot-tool keys from a child onto the parent.

    Only ``SNAPSHOT_TOOL_NAMES`` entries that are still cacheable, and
    only signatures that appear in the child's ``executed_signatures``.
    Marks each copy ``inherited_from=spawn`` so a later hit does not
    claim the page body is in this transcript. File / MCP / write tools
    are left alone so a parent can re-read something a child just
    changed. Returns how many signatures were inherited.
    """
    if not isinstance(source_cache, dict):
        return 0
    allowed: Optional[set[str]] = None
    if isinstance(source_signatures, list):
        allowed = {str(sig) for sig in source_signatures if sig}
    dest_sig_set = {str(sig) for sig in dest_signatures}
    inherited = 0
    for raw_sig, raw_entry in source_cache.items():
        signature = str(raw_sig)
        if not signature:
            continue
        if allowed is not None and signature not in allowed:
            continue
        try:
            entry = (
                raw_entry
                if isinstance(raw_entry, ObservationCacheEntry)
                else ObservationCacheEntry.model_validate(raw_entry)
            )
        except Exception:
            continue
        if entry.tool_name not in SNAPSHOT_TOOL_NAMES:
            continue
        if not entry.cache_control.is_cacheable():
            continue
        dest_cache[signature] = entry.model_copy(
            update={"inherited_from": INHERITED_FROM_SPAWN}
        ).model_dump(mode="json")
        if signature not in dest_sig_set:
            dest_signatures.append(signature)
            dest_sig_set.add(signature)
        inherited += 1
    return inherited


def _extract_observation_artifact_id(payload: Any) -> Optional[str]:
    """Best-effort tool-result artifact id on the wrapper or inner body."""
    if not isinstance(payload, dict):
        return None
    candidates = [
        payload.get("artifact_id"),
    ]
    inner = payload.get("result")
    if isinstance(inner, dict):
        candidates.append(inner.get("artifact_id"))
        nested = inner.get("result")
        if isinstance(nested, dict):
            candidates.append(nested.get("artifact_id"))
    for raw in candidates:
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _unwrap_tool_execution_payload(payload: Any) -> Any:
    """Prefer the inner tool body when *payload* is the tool_execution wrapper."""
    if (
        isinstance(payload, dict)
        and payload.get("executed") is True
        and isinstance(payload.get("result"), dict)
    ):
        return payload["result"]
    return payload


def _snapshot_fetch_succeeded(inner: Any) -> bool:
    """True when a snapshot tool fetched something, even if the body was offloaded."""
    if not isinstance(inner, dict):
        return False
    if inner.get("status") == "error" or inner.get("error"):
        return False
    status = inner.get("status")
    if isinstance(status, int) and not (200 <= status < 400):
        return False
    if inner.get("_context_processed") or inner.get("artifact_id"):
        return True
    return len(_body_text(inner).strip()) >= MIN_SNAPSHOT_CHARS


def remember_observation(
    cache: Dict[str, Any],
    *,
    signature: str,
    tool_name: str,
    payload: Any,
    now: Optional[float] = None,
) -> None:
    """Store or drop the signature based on the result's cache-control."""
    if not signature:
        return
    inner = _unwrap_tool_execution_payload(payload)
    cc = resolve_cache_control(tool_name, inner)
    if (
        not cc.is_cacheable()
        and tool_name in SNAPSHOT_TOOL_NAMES
        and _snapshot_fetch_succeeded(inner)
    ):
        # Context management often strips ``text`` / ``main_content`` after a
        # real fetch. That is not an empty page — keep the same-turn veto.
        cc = SAME_TURN
    if not cc.is_cacheable():
        cache.pop(signature, None)
        return
    clock = time.time() if now is None else now
    cache[signature] = ObservationCacheEntry(
        tool_name=tool_name,
        cache_control=cc,
        stored_at=clock,
        artifact_id=_extract_observation_artifact_id(payload),
    ).model_dump(mode="json")


__all__ = [
    "CACHE_NOTICE_PREFIX",
    "INHERITED_FROM_SPAWN",
    "MIN_SNAPSHOT_CHARS",
    "NO_STORE",
    "ObservationCacheEntry",
    "SAME_TURN",
    "SNAPSHOT_TOOL_NAMES",
    "ToolCacheControl",
    "attach_snapshot_cache_control",
    "cached_observation_text",
    "extract_cache_control",
    "inherit_snapshot_cache",
    "parse_cache_control",
    "remember_observation",
    "resolve_cache_control",
    "snapshot_default",
    "take_fresh_cache_hit",
]
