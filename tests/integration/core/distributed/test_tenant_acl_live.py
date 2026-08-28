"""
Motet - Tenant Valkey ACL Live Isolation Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-17

Description:
    Against live Valkey 9.1, create two tenant ACL users and assert a tenant
    client can read its own prefixed keys and cannot read another tenant's.

Dependencies:
    - pytest
    - redis
    - Live Valkey via MOTET_REDIS_URL (requires_redis)

Usage:
    pytest tests/integration/core/distributed/test_tenant_acl_live.py -q

Notes:
    - Skips when the server rejects ACL SETUSER (ElastiCache command path).
    - Default user is left unrestricted.
"""

from __future__ import annotations

import os
import uuid
from typing import Any
from urllib.parse import urlparse

import pytest

from motet.core.distributed.tenant_acl import apply_tenant_acl_user
from motet.core.distributed.tenant_keys import tenant_acl_username, tenant_key


def _redis_url() -> str:
    return os.environ.get("MOTET_REDIS_URL", "redis://localhost:6379/0")


def _admin_client() -> Any:
    import redis

    return redis.Redis.from_url(_redis_url(), decode_responses=True)


def _tenant_client(username: str) -> Any:
    import redis

    parsed = urlparse(_redis_url())
    return redis.Redis(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        db=int((parsed.path or "/0").lstrip("/") or "0"),
        username=username,
        password="",
        decode_responses=True,
    )


@pytest.mark.requires_redis
def test_tenant_acl_user_cannot_read_other_tenant_keys() -> None:
    admin = _admin_client()
    try:
        admin.ping()
    except Exception as exc:
        pytest.skip(f"Valkey not reachable: {exc}")

    suffix = uuid.uuid4().hex[:8]
    tenant_a = f"acl-a-{suffix}"
    tenant_b = f"acl-b-{suffix}"
    try:
        user_a = apply_tenant_acl_user(admin, tenant_a)
        user_b = apply_tenant_acl_user(admin, tenant_b)
    except Exception as exc:
        pytest.skip(f"ACL SETUSER not available: {exc}")

    key_a = tenant_key(tenant_a, "conv:default:iso")
    key_b = tenant_key(tenant_b, "conv:default:iso")
    admin.set(key_a, "alpha")
    admin.set(key_b, "bravo")
    client_a = None
    try:
        client_a = _tenant_client(user_a)
        assert client_a.get(key_a) == "alpha"
        with pytest.raises(Exception) as denied:
            client_a.get(key_b)
        assert "NOPERM" in str(denied.value).upper() or "no permission" in str(
            denied.value
        ).lower()
        assert tenant_acl_username(tenant_b) == user_b
    finally:
        admin.delete(key_a, key_b)
        try:
            admin.execute_command("ACL", "DELUSER", user_a, user_b)
        except Exception:
            pass
        if client_a is not None:
            client_a.close()
        admin.close()
