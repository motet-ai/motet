"""
Motet - Conversation Ownership (issue #139)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Authoritative per-conversation ownership binding for memory-bearing paths.
    A conversation_id is a capability: on first use it is bound to
    (motet_id, tenant_id, principal_id). Later access by a different principal
    in the same tenant is rejected. Tenant isolation remains in the memory
    store key prefix; this module closes the cross-principal gap within a
    tenant (issue #139 / Phase 4 precursor).

    Ownership metadata (not principal-scoped KV) leaves the door open for a
    future membership/ACL layer without redesigning the store.

Dependencies:
    - motet.core.distributed.redis_manager: structured data + distributed locks
    - motet.core.conversations.registry: lazy migration from principal lists

Usage:
    from motet.core.conversations.ownership import (
        authorize_conversation_access_sync,
        ConversationAccessDenied,
        ACCESS_DENIED_MESSAGE,
    )

    authorize_conversation_access_sync(
        motet_id="default",
        tenant_id="acme",
        principal_id="user:alice",
        conversation_id="conv-123",
        bind_if_unclaimed=True,
    )

Notes:
    - Logical key: conv:owner:{motet_id}:{conversation_id}
    - Stored as {tenant_id}:conv:owner:… (issue #218). Leftover Phase 2
      {tenant}:imf:conv:owner:… and pre-Phase-2 imf:conv:owner:… keys are not dual-read.
    - First-use bind uses a short distributed lock + re-read to avoid races.
    - Unclaimed ids that already appear in the caller's registry are claimed
      (migration for conversations created before ownership tracking).
    - Read paths may pass bind_if_unclaimed=False; they still succeed when the
      id is in the caller's registry (and lazy-bind) or already owned by them.
    - The API boundary uses require_not_owned_by_other_sync (non-binding) so a
      streaming request can still be rejected with HTTP 403 before headers are
      sent; the command layer performs the authoritative bind.
"""

from __future__ import annotations

from typing import Any, Optional

import structlog

from ..distributed.redis_manager import (
    acquire_distributed_lock_sync,
    get_sync_redis_client,
    retrieve_structured_data_sync,
    store_structured_data_sync,
)
from ..distributed.tenant_keys import tenant_key

logger = structlog.get_logger(__name__)

OWNERSHIP_CLIENT_ID = "conversation_ownership"
ACCESS_DENIED_MESSAGE = "Conversation access denied"
_LOCK_TTL_SECONDS = 15


class ConversationAccessDenied(PermissionError):
    """Raised when a principal attempts to use another principal's conversation_id."""

    def __init__(
        self,
        message: str = ACCESS_DENIED_MESSAGE,
        *,
        conversation_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        owner_principal_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.conversation_id = conversation_id
        self.principal_id = principal_id
        self.owner_principal_id = owner_principal_id


def is_conversation_access_denied(exc_or_message: Any) -> bool:
    """True when *exc_or_message* is or describes a conversation ownership denial."""
    if isinstance(exc_or_message, ConversationAccessDenied):
        return True
    text = str(getattr(exc_or_message, "message", None) or exc_or_message or "")
    return ACCESS_DENIED_MESSAGE in text


def _logical_ownership_key(motet_id: str, tenant_id: str, conversation_id: str) -> str:
    return f"conv:owner:{motet_id}:{conversation_id}"


def _ownership_key(motet_id: str, tenant_id: str, conversation_id: str) -> str:
    return tenant_key(tenant_id, _logical_ownership_key(motet_id, tenant_id, conversation_id))


def _require_ids(
    motet_id: str,
    tenant_id: str,
    principal_id: str,
    conversation_id: str,
) -> tuple[str, str, str, str]:
    mid = (motet_id or "").strip()
    tid = (tenant_id or "").strip()
    pid = (principal_id or "").strip()
    cid = (conversation_id or "").strip()
    if not mid or not tid or not pid or not cid:
        raise ValueError(
            "conversation ownership requires non-empty motet_id, tenant_id, "
            f"principal_id, and conversation_id (got motet_id={motet_id!r}, "
            f"tenant_id={tenant_id!r}, principal_id={principal_id!r}, "
            f"conversation_id={conversation_id!r})"
        )
    return mid, tid, pid, cid


def get_conversation_owner_sync(
    motet_id: str,
    tenant_id: str,
    conversation_id: str,
) -> Optional[str]:
    """Return the bound owner principal_id, or None when unclaimed."""
    mid = (motet_id or "").strip()
    tid = (tenant_id or "").strip()
    cid = (conversation_id or "").strip()
    if not mid or not tid or not cid:
        return None
    key = _ownership_key(mid, tid, cid)
    try:
        raw = retrieve_structured_data_sync(
            OWNERSHIP_CLIENT_ID,
            tenant_key(tid, _logical_ownership_key(mid, tid, cid)),
            format_type="json_string",
        )
    except Exception as e:
        logger.error(
            "conversation_ownership_read_failed",
            key=key,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        raise RuntimeError(f"Failed to read conversation ownership: {e}") from e
    if not raw:
        return None
    owner = str(raw.get("principal_id") or "").strip()
    return owner or None


def _write_owner_sync(
    motet_id: str,
    tenant_id: str,
    conversation_id: str,
    principal_id: str,
) -> None:
    key = _ownership_key(motet_id, tenant_id, conversation_id)
    store_structured_data_sync(
        OWNERSHIP_CLIENT_ID,
        key,
        {
            "principal_id": principal_id,
            "motet_id": motet_id,
            "tenant_id": tenant_id,
            "conversation_id": conversation_id,
        },
        format_type="json_string",
    )


def delete_conversation_owner_sync(
    motet_id: str,
    tenant_id: str,
    conversation_id: str,
) -> bool:
    """Delete the ownership record. Returns True when a key was removed."""
    mid = (motet_id or "").strip()
    tid = (tenant_id or "").strip()
    cid = (conversation_id or "").strip()
    if not mid or not tid or not cid:
        return False
    key = _ownership_key(mid, tid, cid)
    try:
        redis = get_sync_redis_client(OWNERSHIP_CLIENT_ID)
        deleted = redis.delete(tenant_key(tid, _logical_ownership_key(mid, tid, cid)))
        removed = int(deleted) if isinstance(deleted, int) else 0
        return removed > 0
    except Exception as e:
        logger.warning(
            "conversation_ownership_delete_failed",
            key=key,
            error=str(e),
            error_type=type(e).__name__,
        )
        return False


def _principal_has_registry_entry_sync(
    motet_id: str,
    tenant_id: str,
    principal_id: str,
    conversation_id: str,
) -> bool:
    """True when the conversation appears in this principal's registry list."""
    from .registry import has_conversation_sync

    try:
        return has_conversation_sync(
            motet_id=motet_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            conversation_id=conversation_id,
        )
    except Exception as e:
        logger.warning(
            "conversation_ownership_registry_lookup_failed",
            motet_id=motet_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            conversation_id=conversation_id,
            error=str(e),
            error_type=type(e).__name__,
        )
        return False


def _deny(
    *,
    conversation_id: str,
    principal_id: str,
    owner_principal_id: Optional[str],
) -> None:
    logger.warning(
        "conversation_access_denied",
        conversation_id=conversation_id,
        principal_id=principal_id,
        owner_principal_id=owner_principal_id,
    )
    raise ConversationAccessDenied(
        ACCESS_DENIED_MESSAGE,
        conversation_id=conversation_id,
        principal_id=principal_id,
        owner_principal_id=owner_principal_id,
    )


def _claim_owner_sync(
    motet_id: str,
    tenant_id: str,
    principal_id: str,
    conversation_id: str,
) -> str:
    """Atomically claim ownership or return the existing owner after a race."""
    existing = get_conversation_owner_sync(motet_id, tenant_id, conversation_id)
    if existing:
        return existing

    lock_key = f"lock:{_ownership_key(motet_id, tenant_id, conversation_id)}"
    lock = acquire_distributed_lock_sync(
        OWNERSHIP_CLIENT_ID, lock_key, ttl_seconds=_LOCK_TTL_SECONDS
    )
    if lock is None:
        # Another writer is claiming; re-read. If still unset, deny rather than
        # race into an unbound write.
        existing = get_conversation_owner_sync(motet_id, tenant_id, conversation_id)
        if existing:
            return existing
        _deny(
            conversation_id=conversation_id,
            principal_id=principal_id,
            owner_principal_id=None,
        )
        raise AssertionError("unreachable")  # pragma: no cover

    try:
        existing = get_conversation_owner_sync(motet_id, tenant_id, conversation_id)
        if existing:
            return existing
        _write_owner_sync(motet_id, tenant_id, conversation_id, principal_id)
        logger.info(
            "conversation_ownership_bound",
            motet_id=motet_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            conversation_id=conversation_id,
        )
        return principal_id
    finally:
        try:
            lock.release_sync()
        except Exception as e:
            logger.warning(
                "conversation_ownership_lock_release_failed",
                lock_key=lock_key,
                error=str(e),
                error_type=type(e).__name__,
            )


def authorize_conversation_access_sync(
    *,
    motet_id: str,
    tenant_id: str,
    principal_id: str,
    conversation_id: str,
    bind_if_unclaimed: bool = True,
) -> str:
    """
    Authorize *principal_id* for *conversation_id* within motet/tenant.

    Returns the owner principal_id on success.

    Args:
        bind_if_unclaimed: When True (write / agent_turn paths), claim ownership
            if unset. When False (read/clear paths), allow only the bound owner,
            or lazy-claim when the id is already in the caller's registry
            (migration for pre-ownership conversations).
    """
    mid, tid, pid, cid = _require_ids(
        motet_id, tenant_id, principal_id, conversation_id
    )

    owner = get_conversation_owner_sync(mid, tid, cid)
    if owner:
        if owner != pid:
            _deny(
                conversation_id=cid,
                principal_id=pid,
                owner_principal_id=owner,
            )
        return owner

    # Unclaimed
    if bind_if_unclaimed:
        claimed = _claim_owner_sync(mid, tid, pid, cid)
        if claimed != pid:
            _deny(
                conversation_id=cid,
                principal_id=pid,
                owner_principal_id=claimed,
            )
        return claimed

    # Read/clear without binding: migrate from registry membership, else deny.
    if _principal_has_registry_entry_sync(mid, tid, pid, cid):
        claimed = _claim_owner_sync(mid, tid, pid, cid)
        if claimed != pid:
            _deny(
                conversation_id=cid,
                principal_id=pid,
                owner_principal_id=claimed,
            )
        return claimed

    _deny(conversation_id=cid, principal_id=pid, owner_principal_id=None)
    raise AssertionError("unreachable")  # pragma: no cover


def require_not_owned_by_other_sync(
    *,
    motet_id: str,
    tenant_id: str,
    principal_id: str,
    conversation_id: str,
) -> Optional[str]:
    """
    Non-binding guard: deny only when *conversation_id* is already owned by a
    different principal. Returns the owner when it is the caller, else None.

    Used at the API boundary so a cross-principal request fails with HTTP 403
    before dispatch (a streaming response cannot change status after headers
    are sent). Deliberately does not claim ownership: the agent may rewrite the
    id with a configured prefix, and ``agent_turn`` binds the effective id.
    """
    mid, tid, pid, cid = _require_ids(
        motet_id, tenant_id, principal_id, conversation_id
    )
    owner = get_conversation_owner_sync(mid, tid, cid)
    if owner and owner != pid:
        _deny(conversation_id=cid, principal_id=pid, owner_principal_id=owner)
    return owner


def authorize_motet_conversation_access(
    motet: Any,
    *,
    conversation_id: Optional[str] = None,
    bind_if_unclaimed: bool = True,
) -> Optional[str]:
    """
    Authorize using identity on *motet* (MotetContext).

    No-op (returns None) when conversation_id is empty, or when the context
    carries incomplete identity. Raises ConversationAccessDenied on mismatch.

    Incomplete identity is skipped rather than raised because this runs in the
    agent-turn hot path, where internal callers (schedules, system commands)
    may have no principal. Hard-failing them would turn an authorization fix
    into an outage, and the attacker-reachable surface is the HTTP API, which
    always carries a verified principal and is additionally checked at the
    boundary. The warning exists so any such path can be found and fixed.
    """
    cid = (conversation_id if conversation_id is not None else getattr(motet, "conversation_id", None)) or ""
    cid = str(cid).strip()
    if not cid:
        return None

    mid = str(getattr(motet, "motet_id", "") or "").strip()
    tid = str(getattr(motet, "tenant_id", "") or "").strip()
    pid = str(getattr(motet, "principal_id", "") or "").strip()
    if not mid or not tid or not pid:
        logger.warning(
            "conversation_ownership_check_skipped_incomplete_identity",
            conversation_id=cid,
            has_motet_id=bool(mid),
            has_tenant_id=bool(tid),
            has_principal_id=bool(pid),
        )
        return None

    return authorize_conversation_access_sync(
        motet_id=mid,
        tenant_id=tid,
        principal_id=pid,
        conversation_id=cid,
        bind_if_unclaimed=bind_if_unclaimed,
    )


__all__ = [
    "ACCESS_DENIED_MESSAGE",
    "ConversationAccessDenied",
    "OWNERSHIP_CLIENT_ID",
    "authorize_conversation_access_sync",
    "authorize_motet_conversation_access",
    "delete_conversation_owner_sync",
    "get_conversation_owner_sync",
    "is_conversation_access_denied",
    "require_not_owned_by_other_sync",
]
