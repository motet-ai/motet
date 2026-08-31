"""
Motet - Conversation Registry

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-31

Description:
    Redis-backed registry of conversations per (motet_id, tenant_id, principal_id) for the
    Conversations API. Enables listing, creating, updating, and removing conversation
    metadata (id, title, created_at, updated_at, agent_id, surface_id,
    parent_conversation_id, root_conversation_id, optional turn_agent_id
    and spawn_contract) so the chat UI
    can restore the conversation list on page refresh. Uses motet_id
    for isolation across deployment environments (e.g. production vs staging), consistent
    with memory. Supports agent and surface scoping for list filtering.

Dependencies:
    - motet.core.distributed.redis_manager: store/retrieve_structured_data (async)

Usage:
    from motet.core.conversations.registry import (
        list_conversations,
        register_or_touch_conversation,
        remove_conversation,
    )
    convs = await list_conversations(motet_id="default", tenant_id="t1", principal_id="p1")
    await register_or_touch_conversation("default", "t1", "p1", "conv-123", title="My Chat",
        agent_id="core.default", surface_id="demo_chat")
    await remove_conversation("default", "t1", "p1", "conv-123")

Notes:
    - Uses client_id "conversation_registry"; logical key conv:{motet_id}:{principal_id},
      stored as {tenant_id}:conv:… (issue #218). Leftover Phase 2
      {tenant}:imf:conv:… and pre-Phase-2 imf:conv:… keys are not dual-read.
    - Conversations stored as JSON list; sorted by updated_at desc when listing.
    - agent_id/surface_id set on create, backfilled on touch if missing.
    - turn_agent_id / spawn_contract are optional child-follow-up fields;
      also backfilled on touch when missing.
    - list_descendant_conversations_from_registry_sync walks the durable
      parent_conversation_id rows; conversation delete merges it with the
      TTL-bound lineage index so old children still cascade.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import structlog

from ..distributed.redis_manager import (
    retrieve_structured_data,
    store_structured_data,
    retrieve_structured_data_sync,
    store_structured_data_sync,
)
from ..distributed.tenant_keys import tenant_key

logger = structlog.get_logger(__name__)

REGISTRY_CLIENT_ID = "conversation_registry"


def _logical_registry_key(motet_id: str, tenant_id: str, principal_id: str) -> str:
    if not (motet_id and tenant_id and principal_id):
        raise ValueError(
            f"_registry_key requires non-empty motet_id, tenant_id, and principal_id "
            f"(got motet_id={motet_id!r}, tenant_id={tenant_id!r}, principal_id={principal_id!r})"
        )
    return f"conv:{motet_id}:{principal_id}"


def _registry_key(motet_id: str, tenant_id: str, principal_id: str) -> str:
    return tenant_key(tenant_id, _logical_registry_key(motet_id, tenant_id, principal_id))


async def _load_registry(
    motet_id: str,
    tenant_id: str,
    principal_id: str,
) -> Optional[Dict[str, Any]]:
    logical = _logical_registry_key(motet_id, tenant_id, principal_id)
    return await retrieve_structured_data(
        REGISTRY_CLIENT_ID, tenant_key(tenant_id, logical), format_type="json_string"
    )


def _load_registry_sync(
    motet_id: str,
    tenant_id: str,
    principal_id: str,
) -> Optional[Dict[str, Any]]:
    logical = _logical_registry_key(motet_id, tenant_id, principal_id)
    return retrieve_structured_data_sync(
        REGISTRY_CLIENT_ID, tenant_key(tenant_id, logical), format_type="json_string"
    )


# Default agent when client omits agent_id (ADR-0083).
DEFAULT_AGENT_ID = "core.default"


def _matches_scope(
    c: Dict[str, Any],
    agent_id: str,
    surface_id: Optional[str],
) -> bool:
    """
    True if conversation c matches the requested agent_id (and surface_id if provided).
    Strict scoping only: records missing agent_id/surface_id never match filtered results.
    """
    c_agent = c.get("agent_id")
    c_surface = c.get("surface_id")

    # Agent must match exactly for scoped listing.
    agent_ok = c_agent == agent_id
    if not agent_ok:
        return False

    if surface_id is None:
        # No surface filter: include all for this agent
        return True
    # Surface filter: exact match only.
    return c_surface == surface_id


async def list_conversations(
    motet_id: str,
    tenant_id: str,
    principal_id: str,
    limit: int = 100,
    agent_id: Optional[str] = None,
    surface_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    List conversations for a principal in a tenant (within a motet), most recently updated first.

    When agent_id is provided, filters to that agent; when surface_id is also provided,
    filters to that exact scope. Omitted agent_id defaults to core.default (ADR-0083).
    Returns list of dicts with id, title, created_at, updated_at, and optional agent_id, surface_id.
    """
    effective_agent = agent_id if agent_id is not None else DEFAULT_AGENT_ID
    key = _registry_key(motet_id, tenant_id, principal_id)
    try:
        raw = await _load_registry(motet_id, tenant_id, principal_id)
        if not raw or "conversations" not in raw:
            return []
        convs = raw.get("conversations") or []
        if not isinstance(convs, list):
            return []
        convs = [
            c
            for c in convs
            if _matches_scope(c, effective_agent, surface_id)
        ]
        convs = sorted(
            convs,
            key=lambda c: float(c.get("updated_at") or c.get("created_at") or 0),
            reverse=True,
        )
        return convs[:limit]
    except Exception as e:
        logger.warning("conversation_registry_list_failed", key=key, error=str(e))
        return []


async def register_or_touch_conversation(
    motet_id: str,
    tenant_id: str,
    principal_id: str,
    conversation_id: str,
    title: Optional[str] = None,
    agent_id: Optional[str] = None,
    surface_id: Optional[str] = None,
) -> None:
    """
    Register a new conversation or update updated_at (and optionally title) if it exists.
    agent_id and surface_id are set on create and backfilled on touch if the existing
    entry is missing them (handles race between fire-and-forget register and rename); ADR-0083.
    """
    key = _registry_key(motet_id, tenant_id, principal_id)
    now = time.time()
    try:
        raw = await _load_registry(motet_id, tenant_id, principal_id)
        convs: List[Dict[str, Any]] = (raw.get("conversations") or []) if raw else []
        if not isinstance(convs, list):
            convs = []

        found = False
        for c in convs:
            if c.get("id") == conversation_id:
                c["updated_at"] = now
                if title is not None:
                    c["title"] = title
                if agent_id is not None and not c.get("agent_id"):
                    c["agent_id"] = agent_id
                if surface_id is not None and not c.get("surface_id"):
                    c["surface_id"] = surface_id
                found = True
                break
        if not found:
            entry: Dict[str, Any] = {
                "id": conversation_id,
                "title": title or "New Chat",
                "created_at": now,
                "updated_at": now,
            }
            if agent_id is not None:
                entry["agent_id"] = agent_id
            if surface_id is not None:
                entry["surface_id"] = surface_id
            convs.append(entry)

        await store_structured_data(
            REGISTRY_CLIENT_ID,
            key,
            {"conversations": convs},
            format_type="json_string",
        )
        logger.debug(
            "conversation_registry_updated",
            tenant_id=tenant_id,
            principal_id=principal_id,
            conversation_id=conversation_id,
            created=not found,
        )
    except Exception as e:
        logger.warning(
            "conversation_registry_register_failed",
            key=key,
            conversation_id=conversation_id,
            error=str(e),
        )
        raise


async def update_conversation_title(
    motet_id: str,
    tenant_id: str,
    principal_id: str,
    conversation_id: str,
    title: str,
) -> bool:
    """Update the title of an existing conversation. Returns True if updated."""
    key = _registry_key(motet_id, tenant_id, principal_id)
    try:
        raw = await _load_registry(motet_id, tenant_id, principal_id)
        convs: List[Dict[str, Any]] = (raw.get("conversations") or []) if raw else []
        if not isinstance(convs, list):
            return False
        for c in convs:
            if c.get("id") == conversation_id:
                c["title"] = title
                c["updated_at"] = time.time()
                await store_structured_data(
                    REGISTRY_CLIENT_ID,
                    key,
                    {"conversations": convs},
                    format_type="json_string",
                )
                return True
        return False
    except Exception as e:
        logger.warning(
            "conversation_registry_update_title_failed",
            key=key,
            conversation_id=conversation_id,
            error=str(e),
        )
        raise


async def remove_conversation(
    motet_id: str,
    tenant_id: str,
    principal_id: str,
    conversation_id: str,
) -> bool:
    """Remove a conversation from the registry. Returns True if removed."""
    key = _registry_key(motet_id, tenant_id, principal_id)
    try:
        raw = await _load_registry(motet_id, tenant_id, principal_id)
        convs: List[Dict[str, Any]] = (raw.get("conversations") or []) if raw else []
        if not isinstance(convs, list):
            return False
        before = len(convs)
        convs = [c for c in convs if c.get("id") != conversation_id]
        if len(convs) == before:
            return False
        await store_structured_data(
            REGISTRY_CLIENT_ID,
            key,
            {"conversations": convs},
            format_type="json_string",
        )
        logger.debug(
            "conversation_registry_removed",
            tenant_id=tenant_id,
            principal_id=principal_id,
            conversation_id=conversation_id,
        )
        return True
    except Exception as e:
        logger.warning(
            "conversation_registry_remove_failed",
            key=key,
            conversation_id=conversation_id,
            error=str(e),
        )
        raise


# --- Sync versions for use from Celery workers (distributed commands) ---


def list_conversations_sync(
    motet_id: str,
    tenant_id: str,
    principal_id: str,
    limit: int = 100,
    agent_id: Optional[str] = None,
    surface_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Sync version: list conversations for a principal in a tenant (within a motet). ADR-0083 filter semantics."""
    effective_agent = agent_id if agent_id is not None else DEFAULT_AGENT_ID
    key = _registry_key(motet_id, tenant_id, principal_id)
    try:
        raw = _load_registry_sync(motet_id, tenant_id, principal_id)
        if not raw or "conversations" not in raw:
            return []
        convs = raw.get("conversations") or []
        if not isinstance(convs, list):
            return []
        convs = [
            c
            for c in convs
            if _matches_scope(c, effective_agent, surface_id)
        ]
        convs = sorted(
            convs,
            key=lambda c: float(c.get("updated_at") or c.get("created_at") or 0),
            reverse=True,
        )
        return convs[:limit]
    except Exception as e:
        logger.warning("conversation_registry_list_failed", key=key, error=str(e))
        return []


def _backfill_registry_field(entry: Dict[str, Any], key: str, value: Any) -> None:
    """Set *key* on create or when the existing row is missing it."""
    if value is None:
        return
    if not entry.get(key):
        entry[key] = value


def register_or_touch_conversation_sync(
    motet_id: str,
    tenant_id: str,
    principal_id: str,
    conversation_id: str,
    title: Optional[str] = None,
    agent_id: Optional[str] = None,
    surface_id: Optional[str] = None,
    parent_conversation_id: Optional[str] = None,
    root_conversation_id: Optional[str] = None,
    turn_agent_id: Optional[str] = None,
    spawn_contract: Optional[Dict[str, Any]] = None,
) -> None:
    """Sync version: register or touch a conversation. Backfills agent_id/surface_id on touch if missing (ADR-0083)."""
    key = _registry_key(motet_id, tenant_id, principal_id)
    now = time.time()
    raw = _load_registry_sync(motet_id, tenant_id, principal_id)
    convs: List[Dict[str, Any]] = (raw.get("conversations") or []) if raw else []
    if not isinstance(convs, list):
        convs = []
    found = False
    for c in convs:
        if c.get("id") == conversation_id:
            c["updated_at"] = now
            if title is not None:
                c["title"] = title
            _backfill_registry_field(c, "agent_id", agent_id)
            _backfill_registry_field(c, "surface_id", surface_id)
            _backfill_registry_field(c, "parent_conversation_id", parent_conversation_id)
            _backfill_registry_field(c, "root_conversation_id", root_conversation_id)
            _backfill_registry_field(c, "turn_agent_id", turn_agent_id)
            _backfill_registry_field(c, "spawn_contract", spawn_contract)
            found = True
            break
    if not found:
        entry: Dict[str, Any] = {
            "id": conversation_id,
            "title": title or "New Chat",
            "created_at": now,
            "updated_at": now,
        }
        _backfill_registry_field(entry, "agent_id", agent_id)
        _backfill_registry_field(entry, "surface_id", surface_id)
        _backfill_registry_field(entry, "parent_conversation_id", parent_conversation_id)
        _backfill_registry_field(entry, "root_conversation_id", root_conversation_id)
        _backfill_registry_field(entry, "turn_agent_id", turn_agent_id)
        _backfill_registry_field(entry, "spawn_contract", spawn_contract)
        convs.append(entry)
    store_structured_data_sync(
        REGISTRY_CLIENT_ID,
        key,
        {"conversations": convs},
        format_type="json_string",
    )


def remove_conversation_sync(
    motet_id: str,
    tenant_id: str,
    principal_id: str,
    conversation_id: str,
) -> bool:
    """Sync version: remove a conversation from the registry."""
    key = _registry_key(motet_id, tenant_id, principal_id)
    raw = _load_registry_sync(motet_id, tenant_id, principal_id)
    convs: List[Dict[str, Any]] = (raw.get("conversations") or []) if raw else []
    if not isinstance(convs, list):
        return False
    before = len(convs)
    convs = [c for c in convs if c.get("id") != conversation_id]
    if len(convs) == before:
        return False
    store_structured_data_sync(
        REGISTRY_CLIENT_ID,
        key,
        {"conversations": convs},
        format_type="json_string",
    )
    return True


def has_conversation_sync(
    motet_id: str,
    tenant_id: str,
    principal_id: str,
    conversation_id: str,
) -> bool:
    """
    True when *conversation_id* is present in this principal's registry list.

    Unfiltered by agent/surface — used for ownership migration (issue #139).
    """
    cid = (conversation_id or "").strip()
    if not cid:
        return False
    key = _registry_key(motet_id, tenant_id, principal_id)
    try:
        raw = _load_registry_sync(motet_id, tenant_id, principal_id)
    except Exception as e:
        logger.warning(
            "conversation_registry_has_failed",
            key=key,
            conversation_id=cid,
            error=str(e),
        )
        return False
    convs = (raw or {}).get("conversations") or []
    if not isinstance(convs, list):
        return False
    return any(isinstance(c, dict) and c.get("id") == cid for c in convs)


#: Cap when walking registry descendants (cycle / corrupt parent pointers).
_MAX_REGISTRY_DESCENDANT_DEPTH = 16


def list_descendant_conversations_from_registry_sync(
    motet_id: str,
    tenant_id: str,
    principal_id: str,
    conversation_id: str,
) -> List[str]:
    """
    Descendant conversation ids from registry ``parent_conversation_id`` rows.

    The registry row is durable, so this walk still finds children after the
    lineage index TTL has expired. Does not include ``conversation_id``
    itself. Best-effort: returns [] on failure or when nothing points here.
    """
    cid = (conversation_id or "").strip()
    if not cid:
        return []
    try:
        raw = _load_registry_sync(motet_id, tenant_id, principal_id)
    except Exception as e:
        logger.warning(
            "conversation_registry_descendants_failed",
            conversation_id=cid,
            error=str(e),
        )
        return []
    convs = (raw or {}).get("conversations") or []
    if not isinstance(convs, list):
        return []
    children_by_parent: Dict[str, List[str]] = {}
    for row in convs:
        if not isinstance(row, dict):
            continue
        child_id = str(row.get("id") or "").strip()
        parent_id = str(row.get("parent_conversation_id") or "").strip()
        if child_id and parent_id:
            children_by_parent.setdefault(parent_id, []).append(child_id)
    seen: set[str] = set()
    out: List[str] = []
    queue: List[tuple[str, int]] = [(cid, 0)]
    while queue:
        current, depth = queue.pop(0)
        if depth >= _MAX_REGISTRY_DESCENDANT_DEPTH:
            continue
        for child_id in children_by_parent.get(current, []):
            if child_id == cid or child_id in seen:
                continue
            seen.add(child_id)
            out.append(child_id)
            queue.append((child_id, depth + 1))
    return sorted(out)


def get_conversation_sync(
    motet_id: str,
    tenant_id: str,
    principal_id: str,
    conversation_id: str,
) -> Optional[Dict[str, Any]]:
    """Return one registry row, or None. Unfiltered by agent or surface."""
    cid = (conversation_id or "").strip()
    if not cid:
        return None
    try:
        raw = _load_registry_sync(motet_id, tenant_id, principal_id)
    except Exception as e:
        logger.warning(
            "conversation_registry_get_failed",
            conversation_id=cid,
            error=str(e),
        )
        return None
    convs = (raw or {}).get("conversations") or []
    if not isinstance(convs, list):
        return None
    for row in convs:
        if isinstance(row, dict) and row.get("id") == cid:
            return row
    return None
