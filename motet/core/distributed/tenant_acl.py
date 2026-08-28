"""
Motet - Tenant Valkey ACL Provisioning

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Apply per-tenant Valkey ACL users so ``~{tenant_id}:*`` / ``&{tenant_id}:*``
    are enforced for tenant-scoped application clients. The default user stays
    unrestricted so Celery, MCP, and the event bus keep working (Phase 3).

    Local/dev uses ``ACL SETUSER``. ElastiCache uses the AWS RBAC API
    (``create_tenant_valkey_user``); Redis ``ACL SETUSER`` is not the AWS path.

Dependencies:
    - structlog: structured logging
    - motet.core.distributed.tenant_keys: username and access-string helpers
    - motet.core.tenancy.tenant_registry: catalog index for bulk apply

Usage:
    from motet.core.distributed.tenant_acl import (
        apply_tenant_acl_user,
        provision_tenant_acl,
        sync_catalog_tenant_acl,
    )

    provision_tenant_acl(redis, "acme")
    sync_catalog_tenant_acl(redis)

Notes:
    - Best-effort from tenant create: missing ACL support must not fail catalog writes.
    - Do not attach a tenant user as the only login on Celery-using workers.
    - FT.SEARCH still uses TAG filters; search bypasses key-pattern ACL.
"""

from __future__ import annotations

from typing import Any, Iterable, List, Sequence

import structlog

from motet.core.distributed.tenant_keys import (
    tenant_acl_access_string,
    tenant_acl_username,
)

logger = structlog.get_logger(__name__)


def acl_setuser_args(tenant_id: str, *, app_prefix: str = "motet") -> List[str]:
    """
    ``ACL SETUSER`` tokens for a tenant-scoped application user.

    Matches the ElastiCache access string, plus ``+@connection`` / ``+@pubsub``
    / ``+@transaction`` so redis-py can HELLO/AUTH/MULTI against local Valkey.
    """
    username = tenant_acl_username(tenant_id, app_prefix=app_prefix)
    tid = (tenant_id or "").strip()
    if not tid:
        raise ValueError("tenant_id is required for ACL SETUSER")
    return [
        username,
        "reset",
        "on",
        "nopass",
        f"~{tid}:*",
        "resetchannels",
        f"&{tid}:*",
        "+@read",
        "+@write",
        "+@connection",
        "+@pubsub",
        "+@transaction",
        "-@dangerous",
    ]


def apply_tenant_acl_user(
    client: Any,
    tenant_id: str,
    *,
    app_prefix: str = "motet",
) -> str:
    """Create or replace the tenant ACL user on a Valkey that supports ACL."""
    args = acl_setuser_args(tenant_id, app_prefix=app_prefix)
    username = args[0]
    execute = getattr(client, "execute_command", None)
    if execute is None:
        raise RuntimeError("Redis client does not support execute_command (ACL SETUSER)")
    execute("ACL", "SETUSER", *args)
    logger.info(
        "tenant_acl_user_applied",
        tenant_id=tenant_id,
        username=username,
        access_string=tenant_acl_access_string(tenant_id),
    )
    return username


def provision_tenant_acl(
    client: Any,
    tenant_id: str,
    *,
    app_prefix: str = "motet",
) -> bool:
    """
    Best-effort ACL SETUSER for *tenant_id*.

    Returns True when the user was applied. False when the server or client
    has no ACL (fakeredis, ElastiCache command restrictions, unit fakes).
    """
    tid = (tenant_id or "").strip()
    if not tid:
        return False
    try:
        apply_tenant_acl_user(client, tid, app_prefix=app_prefix)
        return True
    except Exception as exc:
        logger.warning(
            "tenant_acl_provision_skipped",
            tenant_id=tid,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return False


def catalog_tenant_ids(client: Any) -> List[str]:
    """Tenant ids from the unprefixed catalog index."""
    from motet.core.distributed.tenant_keys import product_key, smembers_union

    try:
        raw = smembers_union(client, product_key("tenant:index"))
    except Exception as exc:
        logger.warning(
            "tenant_acl_catalog_index_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return []
    ids: List[str] = []
    for member in raw:
        if isinstance(member, (bytes, bytearray)):
            text = member.decode("utf-8", errors="replace").strip()
        else:
            text = str(member).strip()
        if text:
            ids.append(text)
    return sorted(set(ids))


def sync_catalog_tenant_acl(
    client: Any,
    tenant_ids: Sequence[str] | None = None,
    *,
    app_prefix: str = "motet",
) -> List[str]:
    """Apply ACL users for catalog tenants. Returns usernames that applied."""
    ids: Iterable[str] = tenant_ids if tenant_ids is not None else catalog_tenant_ids(client)
    applied: List[str] = []
    for tenant_id in ids:
        if provision_tenant_acl(client, tenant_id, app_prefix=app_prefix):
            applied.append(tenant_acl_username(tenant_id, app_prefix=app_prefix))
    return applied
