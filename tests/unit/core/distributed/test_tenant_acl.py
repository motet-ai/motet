"""
Motet - Tenant Valkey ACL Unit Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-19

Description:
    Unit tests for local ACL SETUSER token generation and best-effort
    provisioning used when a tenant catalog row is created.

Dependencies:
    - pytest
    - motet.core.distributed.tenant_acl

Usage:
    pytest tests/unit/core/distributed/test_tenant_acl.py -q
"""

from __future__ import annotations

from typing import Any, List, Set

from motet.core.distributed.tenant_acl import (
    acl_setuser_args,
    apply_tenant_acl_user,
    catalog_tenant_ids,
    provision_tenant_acl,
    sync_catalog_tenant_acl,
)


class _AclRedis:
    def __init__(self) -> None:
        self.commands: List[tuple[Any, ...]] = []
        self.sets = {"motet:tenant:index": {"acme", "demo"}}

    def execute_command(self, *args: Any) -> str:
        self.commands.append(args)
        return "OK"

    def smembers(self, key: str) -> Set[str]:
        return set(self.sets.get(key, set()))


class _NoAclRedis:
    def smembers(self, key: str) -> Set[str]:
        return {"acme"}


def test_acl_setuser_args_scope_tenant_and_deny_dangerous() -> None:
    args = acl_setuser_args("acme")
    assert args[0] == "motet-t-acme"
    assert "reset" in args
    assert "nopass" in args
    assert "~acme:*" in args
    assert "&acme:*" in args
    assert "+@read" in args
    assert "+@write" in args
    assert "-@dangerous" in args
    assert "-@dangerous" == args[-1]


def test_apply_tenant_acl_user_issues_setuser() -> None:
    client = _AclRedis()
    assert apply_tenant_acl_user(client, "acme") == "motet-t-acme"
    assert client.commands[0][0] == "ACL"
    assert client.commands[0][1] == "SETUSER"
    assert client.commands[0][2] == "motet-t-acme"


def test_provision_skips_clients_without_acl() -> None:
    assert provision_tenant_acl(_NoAclRedis(), "acme") is False


def test_sync_catalog_uses_index() -> None:
    client = _AclRedis()
    assert catalog_tenant_ids(client) == ["acme", "demo"]
    applied = sync_catalog_tenant_acl(client)
    assert applied == ["motet-t-acme", "motet-t-demo"]


def test_catalog_tenant_ids_reads_motet_index_only() -> None:
    leftover = _AclRedis()
    leftover.sets = {"imf:tenant:index": {"acme"}}
    assert catalog_tenant_ids(leftover) == []
    current = _AclRedis()
    current.sets = {"motet:tenant:index": {"demo"}}
    assert catalog_tenant_ids(current) == ["demo"]
    both = _AclRedis()
    both.sets = {
        "motet:tenant:index": {"demo"},
        "imf:tenant:index": {"acme"},
    }
    assert catalog_tenant_ids(both) == ["demo"]
