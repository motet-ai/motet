"""
Motet - Edge worker command scope tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-20

Description:
    Unit tests for fail-closed MOTET_EDGE_COMMAND_SCOPE tenant/principal checks
    and the platform bundle-lifecycle allowlist for multi-app edges.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from motet.core.workers.command_tasks import _check_edge_worker_command_scope


def _cmd(
    *,
    tenant: str = "t1",
    principal: str = "p1",
    command_type: str = "core.workflow_execution",
):
    return SimpleNamespace(
        distributed_context=SimpleNamespace(tenant_id=tenant, principal_id=principal),
        get_command_type=lambda: command_type,
    )


def test_edge_scope_tenant_fail_closed_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_EDGE_COMMAND_SCOPE", "tenant")
    monkeypatch.delenv("MOTET_EDGE_TENANT_ID", raising=False)
    monkeypatch.delenv("MOTET_EDGE_SCOPE_FAIL_OPEN", raising=False)
    reason = _check_edge_worker_command_scope("edge_app_builder", _cmd())
    assert "MOTET_EDGE_TENANT_ID is required" in reason


def test_edge_scope_tenant_fail_open_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_EDGE_COMMAND_SCOPE", "tenant")
    monkeypatch.delenv("MOTET_EDGE_TENANT_ID", raising=False)
    monkeypatch.setenv("MOTET_EDGE_SCOPE_FAIL_OPEN", "1")
    assert _check_edge_worker_command_scope("edge_app_builder", _cmd()) == ""


def test_edge_scope_tenant_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_EDGE_COMMAND_SCOPE", "tenant")
    monkeypatch.setenv("MOTET_EDGE_TENANT_ID", "allowed")
    monkeypatch.delenv("MOTET_EDGE_SCOPE_FAIL_OPEN", raising=False)
    reason = _check_edge_worker_command_scope("edge_app_builder", _cmd(tenant="other"))
    assert "does not match" in reason


def test_cloud_worker_always_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_EDGE_COMMAND_SCOPE", "tenant")
    monkeypatch.delenv("MOTET_EDGE_TENANT_ID", raising=False)
    assert _check_edge_worker_command_scope("worker-1", _cmd()) == ""


def test_edge_scope_principal_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_EDGE_COMMAND_SCOPE", "principal")
    monkeypatch.setenv("MOTET_EDGE_PRINCIPAL_ID", "app-builder/motet")
    monkeypatch.delenv("MOTET_EDGE_SCOPE_FAIL_OPEN", raising=False)
    reason = _check_edge_worker_command_scope(
        "edge_app_builder_motet", _cmd(principal="human-uuid")
    )
    assert "does not match" in reason


def test_edge_scope_allows_platform_bundle_lifecycle_when_opted_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOTET_EDGE_COMMAND_SCOPE", "principal")
    monkeypatch.setenv("MOTET_EDGE_PRINCIPAL_ID", "app-builder/motet")
    monkeypatch.setenv("MOTET_EDGE_ALLOW_PLATFORM_LIFECYCLE", "1")
    monkeypatch.delenv("MOTET_EDGE_SCOPE_FAIL_OPEN", raising=False)
    for cmd_type in (
        "core.hot_reload_bundle",
        "core.unload_bundle",
        "core.deploy_bundle",
        "core.undeploy_bundle",
    ):
        assert (
            _check_edge_worker_command_scope(
                "edge_app_builder_motet",
                _cmd(principal="human-uuid", command_type=cmd_type),
            )
            == ""
        )


def test_edge_scope_rejects_platform_lifecycle_without_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOTET_EDGE_COMMAND_SCOPE", "principal")
    monkeypatch.setenv("MOTET_EDGE_PRINCIPAL_ID", "app-builder/motet")
    monkeypatch.delenv("MOTET_EDGE_ALLOW_PLATFORM_LIFECYCLE", raising=False)
    monkeypatch.delenv("MOTET_EDGE_SCOPE_FAIL_OPEN", raising=False)
    reason = _check_edge_worker_command_scope(
        "edge_app_builder_motet",
        _cmd(principal="human-uuid", command_type="core.deploy_bundle"),
    )
    assert "does not match" in reason
