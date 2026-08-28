"""
Motet - Tenant Redis Key Helper Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Unit tests for ADR-0095 Phase 2 tenant key prefixing, issue #218
    collapsed live families, shared ``motet:`` product keys, AAD
    historical-key retries, and tenant inference. Leftover ``imf:``
    classifier tables were removed in issue #232.     Issue #233 adds
    ``event_bus_channel`` and tenant-scoped ``events:``. Issue #228 slice B
    adds task stream / control / live helpers. Issue #235 tenant-prefixes
    MCP I/O streams (``{tid}:{manager}:mcp-…``).

Dependencies:
    - pytest
    - motet.core.distributed.tenant_keys

Usage:
    pytest tests/unit/core/distributed/test_tenant_keys.py -q
"""

from __future__ import annotations

import pytest

from motet.core.distributed.tenant_keys import (
    TENANT_SCOPED_PREFIXES,
    already_tenant_prefixed_key,
    cmd_key_scan_patterns,
    command_id_from_cmd_key,
    expand_legacy_logical_keys,
    family_scan_patterns,
    infer_tenant_id_from_key,
    is_platform_vault_logical_key,
    is_shared_key,
    is_tenant_scoped_key,
    is_reserved_tenant_id,
    is_usable_tenant_id,
    is_vault_kek_logical_key,
    maybe_tenant_key,
    payload_aad_key_candidates,
    product_key,
    vault_index_key,
    vault_read_key_candidates,
    event_bus_channel,
    EVENT_BUS_LOGICAL_CHANNEL,
    EVENT_BUS_PSUBSCRIBE_PATTERN,
    is_unified_task_response_stream,
    task_control_key,
    task_live_key,
    task_response_stream,
    task_response_stream_for,
    task_waiters_key,
    tasks_live_index_key,
    stable_aad_logical_key,
    strip_tenant_prefix,
    tenant_acl_access_string,
    tenant_acl_username,
    tenant_key,
    vault_kek_tenant_id,
    write_key,
)


def test_tenant_key_prefixes_live_logicals_and_is_idempotent() -> None:
    assert tenant_key("acme", "conv:default:p1") == "acme:conv:default:p1"
    assert tenant_key("acme", "acme:conv:default:p1") == "acme:conv:default:p1"
    assert tenant_key("acme", "cmd:meta:abc") == "acme:cmd:meta:abc"
    assert tenant_key("acme", "model_profiles:default") == "acme:model_profiles:default"
    assert tenant_key("acme", "tenant:meta") == "acme:tenant:meta"
    assert tenant_key("acme", "tenant:motet:prod") == "acme:tenant:motet:prod"
    assert tenant_key("acme", "openai_compat:response:r1") == "acme:openai_compat:response:r1"
    assert tenant_key("acme", "auth:refresh_token:p1") == "acme:auth:refresh_token:p1"
    assert tenant_key("acme", "auth:service_account:sa_1") == "acme:auth:service_account:sa_1"
    assert tenant_key("acme", "vault:credential:encryption:tenant:acme") == (
        "acme:vault:credential:encryption:tenant:acme"
    )


def test_tenant_key_rejects_empty() -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        tenant_key("", "conv:default:p")
    with pytest.raises(ValueError, match="logical_key"):
        tenant_key("acme", "")


def test_expand_legacy_logical_keys_for_aad() -> None:
    assert expand_legacy_logical_keys("mem:default:mid", "acme") == (
        "imf:mem:default:acme:mid",
    )
    assert expand_legacy_logical_keys("conv:default:p1", "acme") == (
        "imf:conv:default:acme:p1",
    )
    assert expand_legacy_logical_keys("model_profiles:default", "acme") == (
        "imf:model_profiles:acme:default",
    )
    assert expand_legacy_logical_keys("tenant:meta", "acme") == ("imf:tenant:meta:acme",)
    assert expand_legacy_logical_keys("tenant:motet:prod", "acme") == (
        "imf:tenant:motet:acme:prod",
    )
    assert expand_legacy_logical_keys("vault:credential:foo", "acme") == (
        "imf:vault:credential:foo",
    )


def test_tenant_key_write_shape() -> None:
    assert tenant_key("t1", "conv:f1:p1") == "t1:conv:f1:p1"
    assert tenant_key("t1", "cmd:meta:abc") == "t1:cmd:meta:abc"
    assert tenant_key("t1", "vault:credential:foo") == "t1:vault:credential:foo"
    assert tenant_key("t1", "tenant:meta") == "t1:tenant:meta"


def test_strip_tenant_prefix() -> None:
    assert strip_tenant_prefix("t1", "t1:imf:mem:default:t1:abc") == "imf:mem:default:t1:abc"
    assert strip_tenant_prefix("t1", "imf:mem:default:t1:abc") == "imf:mem:default:t1:abc"


def test_payload_aad_key_candidates_include_stable_and_historical() -> None:
    assert stable_aad_logical_key("t1:mem:default:abc", "t1") == "mem:default:abc"
    assert stable_aad_logical_key("t1:imf:mem:default:t1:abc", "t1") == "mem:default:abc"
    candidates = payload_aad_key_candidates("t1:mem:default:abc", "t1")
    assert candidates[0] == "mem:default:abc"
    assert "t1:mem:default:abc" in candidates
    assert "t1:imf:mem:default:t1:abc" in candidates
    assert "imf:mem:default:t1:abc" in candidates
    artifact = payload_aad_key_candidates("acme:art:123", "acme")
    assert artifact[0] == "art:123"
    assert "acme:art:123" in artifact


def test_shared_key_families() -> None:
    assert is_shared_key("celery-task-meta-abc")
    assert is_shared_key("motet:mcp:signals:mgr-1")
    assert is_shared_key("motet:events:channel")
    assert is_shared_key("worker:registration:w1")
    assert is_shared_key("worker:registered")
    assert is_shared_key("manager:status:mcp-local-default:mcp")
    assert is_shared_key("manager:registered")
    assert is_shared_key("healthcheck:probe")
    assert is_shared_key("motet:surface:index")
    assert is_shared_key("motet:tenant:index")
    assert is_shared_key("motet:vault:credential:openai_api_key")
    assert not is_shared_key("conv:default:t1:p1")
    assert not is_shared_key("cmd:meta:abc")
    assert not is_shared_key("acme:events:channel")
    assert is_tenant_scoped_key("events:channel")
    assert already_tenant_prefixed_key("acme:events:channel")
    assert is_tenant_scoped_key("task:abc:response")
    assert is_tenant_scoped_key("task:control:abc")
    assert already_tenant_prefixed_key("acme:task:abc:response")
    assert not is_shared_key("task:control:abc")


def test_vault_index_key() -> None:
    assert vault_index_key("acme") == "acme:vault:index"
    assert vault_index_key("  acme  ") == "acme:vault:index"
    assert vault_index_key(None) == "motet:vault:index"
    assert vault_index_key("") == "motet:vault:index"
    assert vault_index_key("motet") == "motet:vault:index"


def test_product_key() -> None:
    assert product_key("events:channel") == "motet:events:channel"
    assert product_key("motet:events:channel") == "motet:events:channel"
    assert product_key("tenant:index") == "motet:tenant:index"
    assert product_key("mcp:signals:mgr-1") == "motet:mcp:signals:mgr-1"
    assert product_key("function_discovery:manifest") == "motet:function_discovery:manifest"
    assert product_key("surface:index") == "motet:surface:index"
    assert product_key("auth:check:ping") == "motet:auth:check:ping"


def test_event_bus_channel() -> None:
    assert EVENT_BUS_LOGICAL_CHANNEL == "events:channel"
    assert EVENT_BUS_PSUBSCRIBE_PATTERN == "*:events:channel"
    assert event_bus_channel("acme") == "acme:events:channel"
    assert event_bus_channel("  acme  ") == "acme:events:channel"
    assert event_bus_channel(None) == "motet:events:channel"
    assert event_bus_channel("") == "motet:events:channel"
    assert event_bus_channel("motet") == "motet:events:channel"
    assert event_bus_channel("default") == "default:events:channel"
    assert event_bus_channel("motet-global") == "motet-global:events:channel"


def test_task_key_helpers() -> None:
    assert task_response_stream("acme", "t-1") == "acme:task:t-1:response"
    assert task_response_stream("acme", "t-1", loop_id="loop-a") == (
        "acme:task:t-1:response:loop-a"
    )
    assert task_response_stream(None, "t-1") == "task:t-1:response"
    assert task_response_stream("", "t-1") == "task:t-1:response"
    assert task_response_stream("motet", "t-1") == "task:t-1:response"
    assert task_control_key("acme", "scope-1") == "acme:task:control:scope-1"
    assert task_control_key(None, "scope-1") == "task:control:scope-1"
    assert task_waiters_key("acme", "scope-1") == "acme:task:waiters:scope-1"
    assert task_live_key("acme", "t-1") == "acme:task:live:t-1"
    assert tasks_live_index_key("acme", "u1") == "acme:tasks:live:u1"
    assert tasks_live_index_key(None, "u1") == "tasks:live:global:u1"
    assert tasks_live_index_key("", "u1") == "tasks:live:global:u1"
    assert is_unified_task_response_stream("task:t-1:response")
    assert is_unified_task_response_stream("acme:task:t-1:response")
    assert not is_unified_task_response_stream("cmd:meta:abc")
    with pytest.raises(ValueError, match="task_id"):
        task_response_stream("acme", "")


def test_task_response_stream_for_ignores_mock_stream_key() -> None:
    class _Ctx:
        stream_key = object()
        tenant_id = "acme"
        task_id = "t-1"

    assert task_response_stream_for(_Ctx()) == "acme:task:t-1:response"

    class _Bare:
        task_id = "t-1"

    assert task_response_stream_for(_Bare()) == "task:t-1:response"

    class _Explicit:
        stream_key = "custom:stream"
        tenant_id = "acme"
        task_id = "t-1"

    assert task_response_stream_for(_Explicit()) == "custom:stream"


def test_tenant_scoped_prefix_set() -> None:
    assert "imf:conv:" not in TENANT_SCOPED_PREFIXES
    assert "imf:vault:" not in TENANT_SCOPED_PREFIXES
    assert "mem:" in TENANT_SCOPED_PREFIXES
    assert "memvec:" in TENANT_SCOPED_PREFIXES
    assert "cmd:meta:" in TENANT_SCOPED_PREFIXES
    assert "cmd:outcome:" in TENANT_SCOPED_PREFIXES
    assert "budget:" in TENANT_SCOPED_PREFIXES
    assert "art:" in TENANT_SCOPED_PREFIXES
    assert "vault:" in TENANT_SCOPED_PREFIXES
    assert "model_profiles:" in TENANT_SCOPED_PREFIXES
    assert "openai_compat:" in TENANT_SCOPED_PREFIXES
    assert "events:" in TENANT_SCOPED_PREFIXES
    assert "task:" in TENANT_SCOPED_PREFIXES
    assert "tasks:live:" in TENANT_SCOPED_PREFIXES
    assert "user_wf:" in TENANT_SCOPED_PREFIXES
    assert "mcp:" in TENANT_SCOPED_PREFIXES
    assert "user_workflow:" not in TENANT_SCOPED_PREFIXES
    assert "user_workflows:" not in TENANT_SCOPED_PREFIXES
    assert is_tenant_scoped_key("user_wf:user.acme.brief")
    assert is_tenant_scoped_key("user_wf:index")
    assert already_tenant_prefixed_key("acme:user_wf:user.acme.brief")
    assert not is_shared_key("user_wf:user.acme.brief")
    assert is_shared_key("edge_device:wg_peer_ip_counter")


def test_tenant_scoped_and_already_prefixed() -> None:
    assert is_tenant_scoped_key("conv:default:p1")
    assert is_tenant_scoped_key("mem:default:mid")
    assert is_tenant_scoped_key("tenant:meta")
    assert is_tenant_scoped_key("vault:credential:foo")
    assert is_tenant_scoped_key("cmd:meta:abc")
    assert not is_tenant_scoped_key("motet:mcp:signals:m")
    assert already_tenant_prefixed_key("t1:conv:default:p1")
    assert already_tenant_prefixed_key("acme:mem:default:mid")
    assert already_tenant_prefixed_key("acme:tenant:meta")
    assert already_tenant_prefixed_key("acme:vault:credential:encryption:tenant:acme")
    assert already_tenant_prefixed_key("acme:cost:conversation:acme:cid")
    assert already_tenant_prefixed_key("acme:idx:art:tenant:acme")
    assert not already_tenant_prefixed_key("conv:default:p1")
    assert not already_tenant_prefixed_key("t1:motet:mcp:signals:m")
    # First segment is a family name, not a tenant (cost: vs conversation:).
    assert not already_tenant_prefixed_key("cost:conversation:acme:cid")
    assert not already_tenant_prefixed_key("idx:art:tenant:acme")
    assert not already_tenant_prefixed_key("meta:art:abc")
    # conversation: transcripts must not be classified as conv: registry keys.
    assert is_tenant_scoped_key("conversation:cid:transcript_seq")
    assert not already_tenant_prefixed_key("conversation:cid:transcript_seq")


def test_infer_tenant_id_from_known_shapes() -> None:
    assert infer_tenant_id_from_key("budget:config:acme") == "acme"
    assert infer_tenant_id_from_key("budget:usage:daily:acme:2026-08-16") == "acme"
    assert infer_tenant_id_from_key("cost:summary:acme:2026-08-16") == "acme"
    assert infer_tenant_id_from_key("turn_checkpoint:acme:default:cp1") == "acme"
    assert infer_tenant_id_from_key("cmd:meta:abc") is None
    assert infer_tenant_id_from_key("memvec:mid") is None


def test_usable_tenant_id_rejects_sentinels() -> None:
    assert is_usable_tenant_id("demo")
    assert is_usable_tenant_id("tenant-a")
    assert is_usable_tenant_id("motet-global")
    assert not is_reserved_tenant_id("motet-global")
    assert is_reserved_tenant_id("motet")
    assert is_reserved_tenant_id("imf")
    assert is_reserved_tenant_id("worker")
    assert is_reserved_tenant_id("mcp")
    assert not is_usable_tenant_id("motet")
    assert not is_usable_tenant_id("IMF")
    assert not is_usable_tenant_id(None)
    assert not is_usable_tenant_id("")
    assert not is_usable_tenant_id("None")
    assert not is_usable_tenant_id("none")
    assert not is_usable_tenant_id("null")
    assert not is_usable_tenant_id("<MagicMock id='123'>")
    assert not is_usable_tenant_id("encryption:tenant:<MagicMock>")


def test_infer_vault_kek_from_credential_id() -> None:
    assert is_vault_kek_logical_key("imf:vault:credential:encryption:tenant:demo")
    assert is_vault_kek_logical_key("vault:credential:encryption:tenant:demo")
    assert is_vault_kek_logical_key("imf:vault:metadata:encryption:tenant:demo")
    assert is_vault_kek_logical_key("imf:vault:audit:encryption:tenant:demo")
    assert is_vault_kek_logical_key("imf:vault:cache:system:encryption:tenant:demo")
    assert not is_vault_kek_logical_key("imf:vault:credential:openai_api_key")
    assert not is_vault_kek_logical_key("imf:vault:metadata:langfuse")

    assert vault_kek_tenant_id("imf:vault:credential:encryption:tenant:demo") == "demo"
    assert vault_kek_tenant_id("vault:credential:encryption:tenant:demo") == "demo"
    assert vault_kek_tenant_id("imf:vault:metadata:encryption:tenant:acme") == "acme"
    assert vault_kek_tenant_id(
        "imf:vault:cache:system:encryption:tenant:demo:t:demo"
    ) == "demo"
    assert vault_kek_tenant_id("imf:vault:metadata:encryption:tenant:None") is None
    assert vault_kek_tenant_id("imf:vault:credential:openai_api_key") is None

    assert infer_tenant_id_from_key("imf:vault:credential:encryption:tenant:demo") == "demo"
    assert infer_tenant_id_from_key("vault:credential:encryption:tenant:demo") == "demo"
    assert infer_tenant_id_from_key("imf:vault:metadata:encryption:tenant:demo") == "demo"
    assert infer_tenant_id_from_key("imf:vault:audit:encryption:tenant:demo") == "demo"
    assert infer_tenant_id_from_key("imf:vault:credential:openai_api_key") is None
    assert infer_tenant_id_from_key("imf:vault:metadata:langfuse") is None
    assert infer_tenant_id_from_key("vault:cache:alice:openai_api_key:t:acme") == "acme"
    assert infer_tenant_id_from_key("imf:vault:metadata:encryption:tenant:None") is None


def test_command_id_from_legacy_and_prefixed_cmd_keys() -> None:
    assert command_id_from_cmd_key("cmd:meta:abc-123") == "abc-123"
    assert command_id_from_cmd_key("motet-global:cmd:meta:abc-123") == "abc-123"
    assert command_id_from_cmd_key(b"acme:cmd:data:abc-123", kind="data") == "abc-123"
    assert command_id_from_cmd_key("imf:conv:default:acme:p1") == ""
    assert cmd_key_scan_patterns("meta") == ("cmd:meta:*", "*:cmd:meta:*")


def test_maybe_tenant_key_and_family_scan() -> None:
    assert maybe_tenant_key("", "art:abc") == "art:abc"
    assert maybe_tenant_key("None", "art:abc") == "art:abc"
    assert maybe_tenant_key("acme", "art:abc") == "acme:art:abc"


def test_vault_read_key_candidates_includes_leftovers() -> None:
    keys = vault_read_key_candidates("vault:metadata:encryption:tenant:default", "default")
    assert keys[0] == "default:vault:metadata:encryption:tenant:default"
    assert "motet:vault:metadata:encryption:tenant:default" in keys
    assert "None:vault:metadata:encryption:tenant:default" in keys
    assert "vault:metadata:encryption:tenant:default" in keys
    assert "imf:vault:metadata:encryption:tenant:default" in keys
    assert "default:imf:vault:metadata:encryption:tenant:default" in keys
    assert family_scan_patterns("art:") == ("art:*", "*:art:*")
    assert family_scan_patterns("imf:vault:metadata:") == (
        "imf:vault:metadata:*",
        "*:imf:vault:metadata:*",
    )
    assert family_scan_patterns("vault:metadata:") == (
        "vault:metadata:*",
        "*:vault:metadata:*",
    )
    assert family_scan_patterns("auth:service_account:") == (
        "auth:service_account:*",
        "*:auth:service_account:*",
    )


def test_write_key_always_prefixed() -> None:
    class _Fake:
        def exists(self, key: str) -> bool:
            return False

    assert write_key(_Fake(), "acme", "cost:summary:acme:d") == "acme:cost:summary:acme:d"
    assert write_key(_Fake(), "acme", "acme:cost:summary:acme:d") == "acme:cost:summary:acme:d"


def test_tenant_acl_access_string() -> None:
    assert tenant_acl_access_string("acme") == (
        "on ~acme:* &acme:* +@read +@write -@dangerous"
    )
    with pytest.raises(ValueError, match="tenant_id"):
        tenant_acl_access_string("  ")


def test_mcp_io_stream_physical_key_matches_tenant_acl_glob() -> None:
    """Issue #235: tenant-prefixed MCP I/O keys sit inside ~{tid}:*."""
    from fnmatch import fnmatch

    from motet.core.tools.mcp_motet.protocol import (
        StreamType,
        Visibility,
        generate_stream_name,
    )

    acme = generate_stream_name(
        "weather",
        Visibility.MOTET,
        "weather:acme:production",
        StreamType.REQUESTS,
        manager_id="mcp-local-default",
    )
    other = generate_stream_name(
        "weather",
        Visibility.MOTET,
        "weather:other:production",
        StreamType.REQUESTS,
        manager_id="mcp-local-default",
    )
    assert acme == "acme:mcp:mcp-local-default:mcp-weather-motet-acme:production-requests"
    assert fnmatch(acme, "acme:*")
    assert not fnmatch(other, "acme:*")
    assert is_shared_key("motet:mcp:signals:mcp-local-default")
    assert not is_shared_key(acme)
    assert is_tenant_scoped_key("mcp:mcp-local-default:mcp-weather-motet-acme:production-requests")
    assert already_tenant_prefixed_key(acme)
    assert is_reserved_tenant_id("mcp")
    assert not is_usable_tenant_id("mcp")


def test_tenant_acl_username() -> None:
    assert tenant_acl_username("acme") == "motet-t-acme"
    assert tenant_acl_username("acme", app_prefix="prod") == "prod-t-acme"
    with pytest.raises(ValueError, match="tenant_id"):
        tenant_acl_username("  ")


def test_platform_vault_logical_key() -> None:
    assert is_platform_vault_logical_key("imf:vault:metadata:openai_api_key")
    assert is_platform_vault_logical_key("motet:vault:metadata:openai_api_key")
    assert is_platform_vault_logical_key("vault:metadata:openai_api_key")
    assert is_platform_vault_logical_key("imf:vault:credential:github_personal_token")
    assert is_platform_vault_logical_key("imf:vault:metadata:system_config")
    assert not is_platform_vault_logical_key(
        "imf:vault:credential:encryption:tenant:demo"
    )
    assert not is_platform_vault_logical_key("imf:vault:metadata:custom_api")
    assert not is_platform_vault_logical_key("cmd:meta:abc")
