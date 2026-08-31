"""
Motet - Conversation Lineage

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-31

Description:
    Isolated conversations use an opaque id plus stored parent and root
    pointers. Used by workflow ``isolate_conversation`` and
    ``core.spawn_agents``. Cost rollup reads ``root_conversation_id`` from
    the child context (or the parentage hash) — it does not parse the id.
    Conversation clear walks this index so deleting a parent also deletes
    isolated descendants.

    The mint/record helpers here are the contract shared by the workflow
    executor, spawn_agents, and the cost tracking service. Nothing else
    may infer parentage from the conversation id string.

Dependencies:
    - motet.core.distributed.redis_manager: get_sync_redis_client for the
      parentage hash and parent→children index (sets are not covered by
      store_structured_data helpers)

Usage:
    from motet.core.conversations.lineage import (
        mint_isolated_conversation,
        root_conversation_id_of,
        record_conversation_lineage_sync,
        list_child_conversations_sync,
        list_descendant_conversations_sync,
        forget_conversation_lineage_sync,
    )

    iso = mint_isolated_conversation("api-exec-123", tenant_id="motet-global")
    # iso.conversation_id == "iso-…"
    # iso.parent_conversation_id == "api-exec-123"
    # iso.root_conversation_id == "api-exec-123"
    root_conversation_id_of(iso.conversation_id, tenant_id="motet-global")

    list_child_conversations_sync(tenant_id="motet-global", conversation_id="api-exec-123")
    list_descendant_conversations_sync(tenant_id="motet-global", conversation_id="api-exec-123")

Notes:
    - Child ids do not encode the parent. Re-running an isolated step mints
      a new conversation (no suffix collision).
    - Root is denormalized on mint so cost events do not walk Redis.
    - Nested isolation uses the parent's stored root when present.
    - The lineage index is best-effort (30-day TTL, refreshed on write).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional
from uuid import uuid4

import structlog

from ..distributed.redis_manager import get_sync_redis_client
from ..distributed.tenant_keys import tenant_key

logger = structlog.get_logger(__name__)

LINEAGE_CLIENT_ID = "conversation_lineage"

#: Prefix for minted isolated conversation ids (logs / support).
ISOLATED_ID_PREFIX = "iso-"

#: TTL for lineage index and parentage keys, refreshed on every write.
_LINEAGE_TTL_SECONDS = 30 * 24 * 3600  # 30 days

#: Cap when walking descendants for cascade delete (cycle / corrupt index).
_MAX_DESCENDANT_DEPTH = 16


@dataclass(frozen=True)
class IsolatedConversation:
    """Opaque child conversation plus parent and root pointers."""

    conversation_id: str
    parent_conversation_id: str
    root_conversation_id: str


def _new_isolated_id() -> str:
    return f"{ISOLATED_ID_PREFIX}{uuid4().hex}"


def _logical_lineage_key(conversation_id: str) -> str:
    return f"conv:children:{conversation_id}"


def _lineage_key(tenant_id: str, conversation_id: str) -> str:
    return tenant_key(tenant_id, _logical_lineage_key(conversation_id))


def _logical_parentage_key(conversation_id: str) -> str:
    return f"conv:parentage:{conversation_id}"


def _parentage_key(tenant_id: str, conversation_id: str) -> str:
    return tenant_key(tenant_id, _logical_parentage_key(conversation_id))


def _parentage_of(conversation_id: str, tenant_id: str) -> Optional[Dict[str, str]]:
    """Load stored parent/root for a conversation, or None."""
    cid = (conversation_id or "").strip()
    tid = (tenant_id or "").strip()
    if not cid or not tid:
        return None
    try:
        redis = get_sync_redis_client(LINEAGE_CLIENT_ID)
        raw = redis.hgetall(_parentage_key(tid, cid)) or {}
        if not raw:
            return None
        decoded: Dict[str, str] = {}
        for key, value in raw.items():
            k = key.decode("utf-8", errors="replace") if isinstance(key, (bytes, bytearray)) else str(key)
            v = value.decode("utf-8", errors="replace") if isinstance(value, (bytes, bytearray)) else str(value)
            decoded[k] = v
        parent = (decoded.get("parent") or "").strip()
        root = (decoded.get("root") or "").strip()
        if not parent or not root:
            return None
        return {"parent": parent, "root": root}
    except Exception as e:
        logger.warning(
            "conversation_parentage_lookup_failed",
            tenant_id=tid,
            conversation_id=cid,
            error=str(e),
            error_type=type(e).__name__,
        )
        return None


def resolve_root_conversation_id(
    parent_conversation_id: str,
    *,
    tenant_id: Optional[str] = None,
    root_conversation_id: Optional[str] = None,
) -> str:
    """Root for a new child of ``parent`` (explicit, stored, or the parent itself)."""
    explicit = (root_conversation_id or "").strip()
    if explicit:
        return explicit
    parent = (parent_conversation_id or "").strip()
    tid = (tenant_id or "").strip()
    if parent and tid:
        found = _parentage_of(parent, tid)
        if found:
            return found["root"]
    return parent


def mint_isolated_conversation(
    parent_conversation_id: Optional[str],
    *,
    tenant_id: Optional[str] = None,
    kind: Optional[str] = None,
    root_conversation_id: Optional[str] = None,
) -> IsolatedConversation:
    """
    Mint a unique isolated conversation under ``parent``.

    When ``tenant_id`` is set, writes parentage and the children index
    (best-effort). Empty parents get a generated ``workflow-*`` root so the
    child is still attributable.
    """
    parent = (parent_conversation_id or "").strip() or f"workflow-{uuid4().hex[:12]}"
    root = resolve_root_conversation_id(
        parent,
        tenant_id=tenant_id,
        root_conversation_id=root_conversation_id,
    )
    child = _new_isolated_id()
    iso = IsolatedConversation(
        conversation_id=child,
        parent_conversation_id=parent,
        root_conversation_id=root,
    )
    tid = (tenant_id or "").strip()
    if tid:
        record_conversation_lineage_sync(
            tenant_id=tid,
            child_conversation_id=child,
            parent_conversation_id=parent,
            root_conversation_id=root,
            kind=kind,
        )
    return iso


def is_child_conversation_id(
    conversation_id: Optional[str],
    *,
    tenant_id: Optional[str] = None,
) -> bool:
    """True when a parentage record exists for this id."""
    cid = (conversation_id or "").strip()
    tid = (tenant_id or "").strip()
    if not cid or not tid:
        return False
    return _parentage_of(cid, tid) is not None


def root_conversation_id_of(
    conversation_id: Optional[str],
    *,
    tenant_id: Optional[str] = None,
) -> Optional[str]:
    """
    Return the stored root parent, or None when this id is not a child.

    Requires ``tenant_id`` to read the parentage hash.
    """
    cid = (conversation_id or "").strip()
    tid = (tenant_id or "").strip()
    if not cid or not tid:
        return None
    found = _parentage_of(cid, tid)
    if not found:
        return None
    return found["root"]


def record_conversation_lineage_sync(
    *,
    tenant_id: str,
    child_conversation_id: str,
    parent_conversation_id: Optional[str] = None,
    root_conversation_id: Optional[str] = None,
    kind: Optional[str] = None,
) -> Optional[str]:
    """
    Store parent/root pointers and index the child (best-effort).

    Returns the root id when recorded, None when inputs are incomplete
    or the write failed. Never raises.
    """
    tid = (tenant_id or "").strip()
    child = (child_conversation_id or "").strip()
    parent = (parent_conversation_id or "").strip()
    root = (root_conversation_id or parent).strip()
    if not tid or not child or not parent or not root:
        return None
    try:
        redis = get_sync_redis_client(LINEAGE_CLIENT_ID)
        parentage = _parentage_key(tid, child)
        mapping: Dict[str, str] = {"parent": parent, "root": root}
        kind_text = (kind or "").strip()
        if kind_text:
            mapping["kind"] = kind_text
        pipe = redis.pipeline()
        pipe.hset(parentage, mapping=mapping)
        pipe.expire(parentage, _LINEAGE_TTL_SECONDS)
        root_key = _lineage_key(tid, root)
        pipe.sadd(root_key, child)
        pipe.expire(root_key, _LINEAGE_TTL_SECONDS)
        if parent != root:
            parent_key = _lineage_key(tid, parent)
            pipe.sadd(parent_key, child)
            pipe.expire(parent_key, _LINEAGE_TTL_SECONDS)
        pipe.execute()
        return root
    except Exception as e:
        logger.warning(
            "conversation_lineage_record_failed",
            tenant_id=tid,
            child_conversation_id=child,
            parent_conversation_id=parent,
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
    List child conversation ids indexed under a parent or root (sorted).

    Best-effort: returns [] on any failure or when nothing is indexed.
    """
    tid = (tenant_id or "").strip()
    cid = (conversation_id or "").strip()
    if not tid or not cid:
        return []
    try:
        redis = get_sync_redis_client(LINEAGE_CLIENT_ID)
        members = redis.smembers(_lineage_key(tid, cid)) or set()
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


def list_descendant_conversations_sync(
    *,
    tenant_id: str,
    conversation_id: str,
) -> List[str]:
    """
    Isolated descendants of ``conversation_id`` (direct and nested), sorted.

    Walks the children index. Deleting this id should clear these
    conversations too. Does not include ``conversation_id`` itself.
    Best-effort: returns [] on failure or when nothing is indexed.
    """
    tid = (tenant_id or "").strip()
    cid = (conversation_id or "").strip()
    if not tid or not cid:
        return []
    seen: set[str] = set()
    out: List[str] = []
    queue: List[tuple[str, int]] = [(cid, 0)]
    while queue:
        current, depth = queue.pop(0)
        if depth >= _MAX_DESCENDANT_DEPTH:
            continue
        for child in list_child_conversations_sync(tenant_id=tid, conversation_id=current):
            child_id = (child or "").strip()
            if not child_id or child_id == cid or child_id in seen:
                continue
            seen.add(child_id)
            out.append(child_id)
            queue.append((child_id, depth + 1))
    return sorted(out)


def forget_conversation_lineage_sync(
    *,
    tenant_id: str,
    conversation_id: str,
) -> None:
    """
    Drop parentage and children-index rows for one conversation (best-effort).

    Removes this id from its parent and root sets. Never raises.
    """
    tid = (tenant_id or "").strip()
    cid = (conversation_id or "").strip()
    if not tid or not cid:
        return
    try:
        redis = get_sync_redis_client(LINEAGE_CLIENT_ID)
        found = _parentage_of(cid, tid)
        pipe = redis.pipeline()
        pipe.delete(_parentage_key(tid, cid))
        pipe.delete(_lineage_key(tid, cid))
        if found:
            parent = found["parent"]
            root = found["root"]
            pipe.srem(_lineage_key(tid, parent), cid)
            if root != parent:
                pipe.srem(_lineage_key(tid, root), cid)
        pipe.execute()
    except Exception as e:
        logger.warning(
            "conversation_lineage_forget_failed",
            tenant_id=tid,
            conversation_id=cid,
            error=str(e),
            error_type=type(e).__name__,
        )


__all__ = [
    "ISOLATED_ID_PREFIX",
    "IsolatedConversation",
    "mint_isolated_conversation",
    "is_child_conversation_id",
    "root_conversation_id_of",
    "resolve_root_conversation_id",
    "record_conversation_lineage_sync",
    "list_child_conversations_sync",
    "list_descendant_conversations_sync",
    "forget_conversation_lineage_sync",
]
