"""
Motet - Conversation Lineage

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Single source of truth for the child conversation ID convention
    (``{parent}__{suffix}``) used by workflow ``isolate_conversation``, plus a Redis-backed parent→children index for
    end-to-end cycle observability.

    The mint/parse helpers here are the contract shared by the workflow
    executor (mints child IDs) and the cost tracking service (indexes cost
    totals under the root parent). Nothing else may hand-roll ``__``
    splitting or formatting.

Dependencies:
    - motet.core.distributed.redis_manager: get_sync_redis_client for the
      lineage index (sets are not covered by store_structured_data helpers)

Usage:
    from motet.core.conversations.lineage import (
        make_child_conversation_id,
        root_conversation_id_of,
        record_conversation_lineage_sync,
        list_child_conversations_sync,
    )

    child = make_child_conversation_id("api-exec-123", suffix="implement_chunk_0")
    # "api-exec-123__implement_chunk_0"
    root_conversation_id_of(child)   # "api-exec-123"
    root_conversation_id_of("api-exec-123")  # None (not a child)

    record_conversation_lineage_sync(tenant_id="motet-global", child_conversation_id=child)
    list_child_conversations_sync(tenant_id="motet-global", conversation_id="api-exec-123")

Notes:
    - Child IDs always attribute to the ROOT parent: ``root__a__b`` parses to
      ``root`` (nested isolation rolls up to the top-level conversation).
    - The lineage index is best-effort observability data (30-day TTL,
      refreshed on write); transcripts and cost totals do not depend on it.
    - The cost tracking service maintains its own ``:children`` set at
      event-write time for exact rollups; it derives parentage via
      ``root_conversation_id_of`` from this module.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional
from uuid import uuid4

import structlog

from ..distributed.redis_manager import get_sync_redis_client
from ..distributed.tenant_keys import tenant_key

logger = structlog.get_logger(__name__)

LINEAGE_CLIENT_ID = "conversation_lineage"

#: Separator between a parent conversation id and an isolation suffix.
#: Changing this breaks cost rollups and lineage for existing conversations.
CHILD_SEPARATOR = "__"

#: TTL for lineage index keys, refreshed on every write.
_LINEAGE_TTL_SECONDS = 30 * 24 * 3600  # 30 days


def make_child_conversation_id(
    parent_conversation_id: Optional[str],
    *,
    suffix: str,
) -> str:
    """
    Build a stable child conversation id under the parent for isolation.

    Keeps the parent as-is and appends a sanitized suffix (e.g.
    ``implement_chunk_0``). Empty parents get a generated ``workflow-*`` base
    so the child is still traceable to one synthetic root.
    """
    parent = (parent_conversation_id or "").strip() or f"workflow-{uuid4().hex[:12]}"
    safe_suffix = re.sub(r"[^a-zA-Z0-9_-]+", "_", suffix).strip("_") or "step"
    return f"{parent}{CHILD_SEPARATOR}{safe_suffix}"


def is_child_conversation_id(conversation_id: Optional[str]) -> bool:
    """True when the id carries the child separator (workflow isolation)."""
    cid = (conversation_id or "").strip()
    return CHILD_SEPARATOR in cid and bool(cid.split(CHILD_SEPARATOR, 1)[0])


def root_conversation_id_of(conversation_id: Optional[str]) -> Optional[str]:
    """
    Return the ROOT parent conversation id, or None when not a child.

    Nested children (``root__a__b``) attribute to the top-level root so cost
    rollups and lineage always aggregate at the cycle conversation.
    """
    cid = (conversation_id or "").strip()
    if CHILD_SEPARATOR not in cid:
        return None
    root = cid.split(CHILD_SEPARATOR, 1)[0]
    return root or None


def _logical_lineage_key(tenant_id: str, conversation_id: str) -> str:
    return f"conv:children:{conversation_id}"


def _lineage_key(tenant_id: str, conversation_id: str) -> str:
    return tenant_key(tenant_id, _logical_lineage_key(tenant_id, conversation_id))


def record_conversation_lineage_sync(
    *,
    tenant_id: str,
    child_conversation_id: str,
) -> Optional[str]:
    """
    Index a child conversation under its root parent (best-effort).

    Returns the root parent id when recorded, None when the id is not a child
    or the write failed. Never raises — lineage is observability data.
    """
    root = root_conversation_id_of(child_conversation_id)
    tid = (tenant_id or "").strip()
    if not root or not tid:
        return None
    try:
        redis = get_sync_redis_client(LINEAGE_CLIENT_ID)
        key = _lineage_key(tid, root)
        pipe = redis.pipeline()
        pipe.sadd(key, child_conversation_id.strip())
        pipe.expire(key, _LINEAGE_TTL_SECONDS)
        pipe.execute()
        return root
    except Exception as e:
        logger.warning(
            "conversation_lineage_record_failed",
            tenant_id=tid,
            child_conversation_id=child_conversation_id,
            error=str(e),
            error_type=type(e).__name__,
        )
        return None


def list_child_conversations_sync(
    *,
    tenant_id: str,
    conversation_id: str,
) -> List[str]:
    """
    List child conversation ids indexed under a parent (sorted).

    Best-effort: returns [] on any failure or when nothing is indexed.
    """
    tid = (tenant_id or "").strip()
    cid = (conversation_id or "").strip()
    if not tid or not cid:
        return []
    try:
        redis = get_sync_redis_client(LINEAGE_CLIENT_ID)
        members = redis.smembers(tenant_key(tid, _logical_lineage_key(tid, cid))) or set()
        out: List[str] = []
        for member in members:
            if isinstance(member, (bytes, bytearray)):
                out.append(member.decode("utf-8", errors="replace"))
            else:
                out.append(str(member))
        return sorted(out)
    except Exception as e:
        logger.warning(
            "conversation_lineage_list_failed",
            tenant_id=tid,
            conversation_id=cid,
            error=str(e),
            error_type=type(e).__name__,
        )
        return []


__all__ = [
    "CHILD_SEPARATOR",
    "make_child_conversation_id",
    "is_child_conversation_id",
    "root_conversation_id_of",
    "record_conversation_lineage_sync",
    "list_child_conversations_sync",
]
