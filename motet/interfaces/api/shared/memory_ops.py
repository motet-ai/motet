"""
Motet - Manage-App Memory Scan Helpers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Shared Redis SCAN + filter helpers for manage-app memory browse, stats,
    contains search, and scoped clear. Browse decrypts only the newest
    rows (``recent``), while totals use index ``ZCARD`` / ``ZCOUNT``.
    Filters (contains query, type, tag, tier, conversation, agent) apply
    to the collected window.

Dependencies:
    - motet.core.distributed.redis_manager: sync Redis client for index SCAN
    - motet.core.memory.RedisStore: per-tenant KV reads and clear
    - motet.interfaces.api.shared.scope: tenant/motet equality matching

Usage:
    from motet.interfaces.api.shared.memory_ops import (
        collect_memories_for_scope,
        filter_memories,
        compute_memory_stats,
    )

    items = collect_memories_for_scope(stack, tenant_id, motet_id, limit=200)
    notes = filter_memories(items, memory_type="note")
    total, last_24h = count_memory_index(tenant_id, motet_id)
    stats = compute_memory_stats(items, vector_enabled=True)

Notes:
    - Upsert also writes the global index, so scanning ``idx:global`` sees
      conversation, principal, and task-scoped rows.
    - Leftover ``imf:mem:`` keys are ignored so an incomplete cutover stays visible.
    - ``None`` tenant/motet means "all" for callers that already authorized
      cross-tenant visibility.
    - Pass ``limit=None`` only when a full decrypt is required (scoped clear
      by type/tag). Browse and stats keep the default window.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple, cast

import structlog

from motet.core.distributed.redis_manager import get_redis_manager
from motet.interfaces.api.shared.scope import matches_scope

logger = structlog.get_logger(__name__)

_MEM_INDEX_RE_COLLAPSED = re.compile(r"(?:^|:)mem:([^:]+):idx:global")

DEFAULT_WM_TAG = "wm"
DEFAULT_STM_TAG = "stm"
DEFAULT_LTM_TAG = "ltm"
DEFAULT_AGENT_TAG_PREFIX = "agent:"
TIER_TAGS = (DEFAULT_WM_TAG, DEFAULT_STM_TAG, DEFAULT_LTM_TAG)
# Default newest-window size for collect, browse, and stats samples.
COLLECT_DEFAULT_LIMIT = 200
BROWSE_MAX_LIMIT = 5000


def _scan_finished(cursor: Any) -> bool:
    """True when Redis SCAN has wrapped back to the start."""
    return cursor in (0, "0", b"0")


def _decode_redis_key(raw: Any) -> str:
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode()
    return str(raw)


def memory_index_scan_patterns(
    tenant_id: Optional[str] = None,
    motet_id: Optional[str] = None,
) -> Tuple[str, ...]:
    """Return SCAN globs for collapsed memory indices only."""
    tid = (tenant_id or "").strip()
    mid = (motet_id or "").strip()
    if tid and mid:
        return (f"{tid}:mem:{mid}:idx:global",)
    if tid:
        return (f"{tid}:mem:*:idx:global",)
    if mid:
        return (f"*:mem:{mid}:idx:global",)
    return ("*:mem:*:idx:global",)


def iter_memory_index_keys(
    redis_client: Any,
    tenant_id: Optional[str] = None,
    motet_id: Optional[str] = None,
) -> List[str]:
    """Return unique collapsed ``idx:global`` keys for the optional tenant/motet."""
    seen_keys: set[str] = set()
    keys_out: List[str] = []
    for pattern in memory_index_scan_patterns(tenant_id, motet_id):
        cursor: Any = 0
        while True:
            scan_ret = cast(Any, redis_client.scan(cursor, match=pattern, count=100))
            cursor, keys = scan_ret
            for raw in keys:
                decoded = _decode_redis_key(raw)
                if decoded in seen_keys:
                    continue
                seen_keys.add(decoded)
                keys_out.append(decoded)
            if _scan_finished(cursor):
                break
    return keys_out


def scan_memory_index_pairs(
    redis_client: Any,
    tenant_id: Optional[str] = None,
    motet_id: Optional[str] = None,
) -> List[Tuple[str, str]]:
    """Return unique (motet_id, tenant_id) pairs from memory index keys."""
    pairs: set[Tuple[str, str]] = set()
    wanted_tenant = (tenant_id or "").strip()
    wanted_motet = (motet_id or "").strip()
    for decoded in iter_memory_index_keys(redis_client, tenant_id, motet_id):
        collapsed = _MEM_INDEX_RE_COLLAPSED.search(decoded)
        if not collapsed:
            continue
        pair_motet = collapsed.group(1)
        pair_tenant = (
            decoded.split(":", 1)[0]
            if ":" in decoded and not decoded.startswith("mem:")
            else ""
        )
        if not pair_tenant or not pair_motet:
            continue
        if wanted_tenant and pair_tenant != wanted_tenant:
            continue
        if wanted_motet and pair_motet != wanted_motet:
            continue
        pairs.add((pair_motet, pair_tenant))
    return list(pairs)


def count_memory_index(
    tenant_id: Optional[str] = None,
    motet_id: Optional[str] = None,
    *,
    now: Optional[datetime] = None,
) -> Tuple[int, int]:
    """Return ``(total, last_24h)`` from index zsets without decrypting payloads."""
    try:
        redis_manager = get_redis_manager()
        redis_client = redis_manager.get_sync_binary_client("memory_ops_count")
    except Exception as exc:
        logger.warning("memory_index_count_client_failed", error=str(exc))
        return (0, 0)

    clock = now or datetime.now(timezone.utc)
    min_score = (clock - timedelta(hours=24)).timestamp()
    total = 0
    last_24h = 0
    for key in iter_memory_index_keys(redis_client, tenant_id, motet_id):
        try:
            total += int(redis_client.zcard(key) or 0)
            last_24h += int(redis_client.zcount(key, min_score, "+inf") or 0)
        except Exception as exc:
            logger.debug("memory_index_zset_count_failed", key=key, error=str(exc))
            continue
    return (total, last_24h)


def memory_created_at(item: Any) -> Optional[datetime]:
    """Parse ``created_at`` from a MemoryItem or serialized dict."""
    raw = getattr(item, "created_at", None)
    if raw is None and isinstance(item, dict):
        raw = item.get("created_at")
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw
    if isinstance(raw, (int, float)):
        # Seconds if it looks like a unix timestamp; ms if huge.
        ts = float(raw)
        if ts > 1e12:
            ts = ts / 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(raw, str) and raw.strip():
        text = raw.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def memory_created_at_sort_key(item: Any) -> float:
    """Numeric sort key (newer first when reversed)."""
    created = memory_created_at(item)
    if created is None:
        return 0.0
    return created.timestamp()


def memory_tier(
    item: Any,
    *,
    wm_tag: str = DEFAULT_WM_TAG,
    stm_tag: str = DEFAULT_STM_TAG,
    ltm_tag: str = DEFAULT_LTM_TAG,
) -> Optional[str]:
    """Return wm / stm / ltm from tags, preferring long-term when several match."""
    tags = getattr(item, "tags", None)
    if tags is None and isinstance(item, dict):
        tags = item.get("tags")
    tag_set = {str(t).strip() for t in (tags or []) if str(t).strip()}
    if ltm_tag in tag_set:
        return "ltm"
    if stm_tag in tag_set:
        return "stm"
    if wm_tag in tag_set:
        return "wm"
    return None


def _item_field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _item_tags(item: Any) -> List[str]:
    tags = _item_field(item, "tags") or []
    if isinstance(tags, (list, tuple, set)):
        return [str(t) for t in tags]
    return []


def memory_agent_id(
    item: Any,
    *,
    agent_tag_prefix: str = DEFAULT_AGENT_TAG_PREFIX,
) -> Optional[str]:
    """Return the qualified agent id from metadata or an ``agent:`` tag."""
    meta = _item_field(item, "metadata")
    if isinstance(meta, dict):
        from_meta = str(meta.get("agent_id") or "").strip()
        if from_meta:
            return from_meta
    prefix = (agent_tag_prefix or DEFAULT_AGENT_TAG_PREFIX).strip() or DEFAULT_AGENT_TAG_PREFIX
    for tag in _item_tags(item):
        if tag.startswith(prefix):
            agent_id = tag[len(prefix) :].strip()
            if agent_id:
                return agent_id
    return None


def memory_agent_matches(
    item: Any,
    wanted: str,
    *,
    agent_tag_prefix: str = DEFAULT_AGENT_TAG_PREFIX,
) -> bool:
    """True when the item's agent equals the qualified id, ``agent:`` tag, or short name."""
    needle = (wanted or "").strip()
    if not needle:
        return True
    prefix = (agent_tag_prefix or DEFAULT_AGENT_TAG_PREFIX).strip() or DEFAULT_AGENT_TAG_PREFIX
    if needle.startswith(prefix):
        needle = needle[len(prefix) :].strip()
    item_agent = memory_agent_id(item, agent_tag_prefix=prefix)
    if not item_agent:
        return False
    if item_agent == needle:
        return True
    short = item_agent.rsplit(".", 1)[-1]
    return short.lower() == needle.lower()


def filter_memories(
    items: Iterable[Any],
    *,
    query: Optional[str] = None,
    memory_type: Optional[str] = None,
    conversation_id: Optional[str] = None,
    tag: Optional[str] = None,
    tier: Optional[str] = None,
    agent: Optional[str] = None,
    wm_tag: str = DEFAULT_WM_TAG,
    stm_tag: str = DEFAULT_STM_TAG,
    ltm_tag: str = DEFAULT_LTM_TAG,
    agent_tag_prefix: str = DEFAULT_AGENT_TAG_PREFIX,
) -> List[Any]:
    """Apply manage-app browse filters to an in-memory collection."""
    query_lower = (query or "").strip().lower()
    type_filter = (memory_type or "").strip()
    conversation_filter = (conversation_id or "").strip()
    tag_filter = (tag or "").strip()
    agent_filter = (agent or "").strip()
    tier_filter = (tier or "").strip().lower()
    if tier_filter in {"working", "wm"}:
        tier_filter = "wm"
    elif tier_filter in {"short-term", "short_term", "stm"}:
        tier_filter = "stm"
    elif tier_filter in {"long-term", "long_term", "ltm"}:
        tier_filter = "ltm"

    out: List[Any] = []
    for item in items:
        if type_filter and str(_item_field(item, "type") or "") != type_filter:
            continue
        if conversation_filter and str(_item_field(item, "conversation_id") or "") != conversation_filter:
            continue
        tag_list = _item_tags(item)
        if tag_filter and tag_filter not in tag_list:
            continue
        if agent_filter and not memory_agent_matches(
            item, agent_filter, agent_tag_prefix=agent_tag_prefix
        ):
            continue
        if tier_filter and memory_tier(
            item, wm_tag=wm_tag, stm_tag=stm_tag, ltm_tag=ltm_tag
        ) != tier_filter:
            continue
        if query_lower:
            content = str(_item_field(item, "content") or "").lower()
            tag_blob = " ".join(tag_list).lower()
            if query_lower not in content and query_lower not in tag_blob:
                continue
        out.append(item)
    return out


def compute_memory_stats(
    items: Iterable[Any],
    *,
    vector_enabled: bool,
    now: Optional[datetime] = None,
    wm_tag: str = DEFAULT_WM_TAG,
    stm_tag: str = DEFAULT_STM_TAG,
    ltm_tag: str = DEFAULT_LTM_TAG,
    agent_tag_prefix: str = DEFAULT_AGENT_TAG_PREFIX,
) -> Dict[str, Any]:
    """Build manage-app memory statistics from a collected item list."""
    collected = list(items)
    clock = now or datetime.now(timezone.utc)
    cutoff = clock - timedelta(hours=24)
    type_breakdown: Dict[str, int] = {}
    tier_breakdown: Dict[str, int] = {"wm": 0, "stm": 0, "ltm": 0, "untagged": 0}
    scope_breakdown: Dict[str, int] = {}
    motet_breakdown: Dict[str, int] = {}
    tenant_breakdown: Dict[str, int] = {}
    agent_breakdown: Dict[str, int] = {}
    tagged_count = 0
    last_24h = 0

    for item in collected:
        created = memory_created_at(item)
        if created is not None and created >= cutoff:
            last_24h += 1

        mem_type = str(_item_field(item, "type") or "").strip() or "unknown"
        type_breakdown[mem_type] = type_breakdown.get(mem_type, 0) + 1

        tier = memory_tier(item, wm_tag=wm_tag, stm_tag=stm_tag, ltm_tag=ltm_tag)
        tier_breakdown[tier or "untagged"] += 1

        scope_type = str(_item_field(item, "scope_type") or "GLOBAL").strip() or "GLOBAL"
        scope_breakdown[scope_type] = scope_breakdown.get(scope_type, 0) + 1

        motet_id = str(_item_field(item, "motet_id") or "default").strip() or "default"
        motet_breakdown[motet_id] = motet_breakdown.get(motet_id, 0) + 1

        tenant_id = str(_item_field(item, "tenant_id") or "default").strip() or "default"
        tenant_breakdown[tenant_id] = tenant_breakdown.get(tenant_id, 0) + 1

        agent_id = memory_agent_id(item, agent_tag_prefix=agent_tag_prefix) or "unattributed"
        agent_breakdown[agent_id] = agent_breakdown.get(agent_id, 0) + 1

        tags = _item_field(item, "tags") or []
        if tags:
            tagged_count += 1

    return {
        "total_memories": len(collected),
        "last_24h": last_24h,
        "memory_types": len(type_breakdown),
        "tagged_count": tagged_count,
        "type_breakdown": type_breakdown,
        "tier_breakdown": tier_breakdown,
        "scope_breakdown": scope_breakdown,
        "motet_breakdown": motet_breakdown,
        "tenant_breakdown": tenant_breakdown,
        "agent_breakdown": agent_breakdown,
        "vector_enabled": bool(vector_enabled),
    }


def _read_store_rows(store: Any, limit: Optional[int]) -> List[Any]:
    """Read newest ``limit`` rows, or every row when ``limit`` is None."""
    if store is None:
        return []
    if limit is None:
        reader = getattr(store, "all", None)
        if reader is None:
            return []
        try:
            return list(reader(scope="global") or [])
        except TypeError:
            return list(reader() or [])
    reader = getattr(store, "recent", None)
    if reader is not None:
        try:
            return list(reader(limit=limit, scope="global") or [])
        except TypeError:
            return list(reader(limit=limit) or [])
    return _read_store_rows(store, None)


def _store_in_requested_scope(store: Any, tenant_id: Optional[str], motet_id: Optional[str]) -> bool:
    """Skip the process-default store when the manage-app selector names another tenant."""
    if not ((tenant_id or "").strip() or (motet_id or "").strip()):
        return True
    return matches_scope(
        getattr(store, "_tenant_id", None),
        getattr(store, "_motet_id", None),
        tenant_id,
        motet_id,
    )


def _extend_collected(
    dest: List[Any],
    seen_ids: set[str],
    rows: Iterable[Any],
    tenant_id: Optional[str],
    motet_id: Optional[str],
) -> None:
    for memory in rows:
        memory_id = getattr(memory, "id", None)
        if not memory_id or memory_id in seen_ids:
            continue
        if not matches_scope(
            getattr(memory, "tenant_id", None),
            getattr(memory, "motet_id", None),
            tenant_id,
            motet_id,
        ):
            continue
        dest.append(memory)
        seen_ids.add(memory_id)


def collect_memories_for_scope(
    stack: Any,
    tenant_id: Optional[str] = None,
    motet_id: Optional[str] = None,
    limit: Optional[int] = COLLECT_DEFAULT_LIMIT,
) -> List[Any]:
    """
    Collect memories across motet/tenant combinations for the manage app.

    Scans collapsed ``{tenant}:mem:{motet}:idx:global`` only.
    Optional tenant/motet filters limit which stores are opened.
    ``limit`` decrypts only the newest rows per store (then merged).
    Pass ``limit=None`` to decrypt every row.
    """
    from motet.core.memory import RedisStore

    all_memories: List[Any] = []
    seen_ids: set[str] = set()
    default_store = getattr(stack, "memory", None)

    try:
        if default_store is not None and _store_in_requested_scope(default_store, tenant_id, motet_id):
            _extend_collected(
                all_memories,
                seen_ids,
                _read_store_rows(default_store, limit),
                tenant_id,
                motet_id,
            )
    except Exception:
        pass  # default store scan optional; may be unavailable

    try:
        redis_manager = get_redis_manager()
        redis_client = redis_manager.get_sync_binary_client("memory_ops_scan")
        motet_tenant_pairs = scan_memory_index_pairs(redis_client, tenant_id, motet_id)
        encryption_service = getattr(default_store, "_encryption_service", None)

        for pair_motet_id, pair_tenant_id in motet_tenant_pairs:
            if pair_motet_id == "default" and pair_tenant_id == "global":
                continue
            try:
                scoped_store = RedisStore(
                    redis_client=redis_client,
                    motet_id=pair_motet_id,
                    tenant_id=pair_tenant_id,
                    encryption_service=encryption_service,
                )
                _extend_collected(
                    all_memories,
                    seen_ids,
                    _read_store_rows(scoped_store, limit),
                    tenant_id,
                    motet_id,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to get memories for tenant",
                    motet_id=pair_motet_id,
                    tenant_id=pair_tenant_id,
                    error=str(exc),
                )
                continue
    except Exception as exc:
        logger.warning("Failed to scan for additional tenant memories", error=str(exc))

    if limit is not None:
        all_memories.sort(key=memory_created_at_sort_key, reverse=True)
        return all_memories[:limit]
    return all_memories


def clear_scoped_memory_stores(
    stack: Any,
    tenant_id: Optional[str] = None,
    motet_id: Optional[str] = None,
    *,
    clear_unscoped_default: bool = False,
) -> int:
    """Clear Redis memory stores matching tenant/motet. Returns deleted count."""
    from motet.core.memory import RedisStore

    total_cleared = 0
    if clear_unscoped_default and getattr(stack, "memory", None):
        try:
            total_cleared += int(stack.memory.clear_all() or 0)
        except Exception as exc:
            logger.warning("debug_memory_clear_failed", error=str(exc))

    try:
        redis_manager = get_redis_manager()
        redis_client = redis_manager.get_sync_binary_client("memory_ops_clear")
        motet_tenant_pairs = scan_memory_index_pairs(redis_client, tenant_id, motet_id)
        encryption_service = getattr(getattr(stack, "memory", None), "_encryption_service", None)

        for pair_motet_id, pair_tenant_id in motet_tenant_pairs:
            if pair_motet_id == "default" and pair_tenant_id == "global":
                continue
            try:
                scoped_store = RedisStore(
                    redis_client=redis_client,
                    motet_id=pair_motet_id,
                    tenant_id=pair_tenant_id,
                    encryption_service=encryption_service,
                )
                total_cleared += int(scoped_store.clear_all() or 0)
            except Exception as exc:
                logger.warning(
                    "Failed to clear memories for tenant",
                    motet_id=pair_motet_id,
                    tenant_id=pair_tenant_id,
                    error=str(exc),
                )
    except Exception as exc:
        logger.warning("Failed to clear memories from additional tenants", error=str(exc))

    return total_cleared
