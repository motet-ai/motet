"""
Motet - User Workflow Catalog Durability Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Unit tests for Redis-backed ``user.*`` workflow durability (ADR-0129):
    persist → list/fetch → hydrate into WorkflowRegistry → delete, plus
    orphan-id hydrate skip, apply_user_workflow_sync, and tenant isolation
    (issue #234). Fan-out is mocked.

Dependencies:
    - pytest
    - motet.core.workflow.user_catalog
    - motet.core.workflow.Workflow / WorkflowRegistry
"""

from __future__ import annotations

import fnmatch
import json
from typing import Dict, List, Optional, Set, Tuple

import pytest

from motet.core.workflow import Workflow, WorkflowRegistry, WorkflowStep
from motet.core.workflow import user_catalog as catalog


class _FakeRedis:
    """Minimal Redis stub for user_catalog (set/get/delete/sets/incr)."""

    def __init__(self) -> None:
        self.kv: Dict[str, str] = {}
        self.sets: Dict[str, Set[str]] = {}
        self.counters: Dict[str, int] = {}

    def set(self, key: str, value: str) -> bool:
        self.kv[key] = value if isinstance(value, str) else str(value)
        return True

    def get(self, key: str) -> Optional[str]:
        return self.kv.get(key)

    def delete(self, *keys: str) -> int:
        n = 0
        for key in keys:
            if key in self.kv:
                del self.kv[key]
                n += 1
            if key in self.sets:
                del self.sets[key]
                n += 1
            if key in self.counters:
                del self.counters[key]
                n += 1
        return n

    def sadd(self, key: str, *members: str) -> int:
        bucket = self.sets.setdefault(key, set())
        before = len(bucket)
        for m in members:
            bucket.add(m if isinstance(m, str) else str(m))
        return len(bucket) - before

    def srem(self, key: str, *members: str) -> int:
        bucket = self.sets.get(key)
        if not bucket:
            return 0
        n = 0
        for m in members:
            member = m if isinstance(m, str) else str(m)
            if member in bucket:
                bucket.remove(member)
                n += 1
        return n

    def smembers(self, key: str) -> Set[str]:
        return set(self.sets.get(key, set()))

    def incr(self, key: str) -> int:
        self.counters[key] = int(self.counters.get(key, 0)) + 1
        return self.counters[key]

    def scan(self, cursor: int, match: str = "*", count: int = 200) -> Tuple[int, List[str]]:
        keys = list(self.kv) + list(self.sets) + list(self.counters)
        return 0, [key for key in keys if fnmatch.fnmatch(key, match)]


def _sample_workflow(workflow_id: str = "user.acme.catalog_brief") -> Workflow:
    return Workflow(
        workflow_id=workflow_id,
        name="Catalog brief",
        description="durability test workflow",
        required_inputs=["topic"],
        use_for=["tool"],
        steps={
            "calc": WorkflowStep(
                step_id="calc",
                name="Calc",
                command_type="core.tool_execution",
                command_data={
                    "tool_name": "core.math_eval",
                    "parameters": {"expression": "1+1"},
                },
                dependencies=[],
            )
        },
        metadata={"authored_by_principal_id": "principal-a", "tenant_id": "acme"},
    )


@pytest.fixture()
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    redis = _FakeRedis()
    monkeypatch.setattr(
        "motet.core.distributed.redis_manager.get_sync_redis_client",
        lambda _client_id: redis,
    )
    return redis


@pytest.fixture(autouse=True)
def _clean_user_workflows():
    yield
    for wf in list(WorkflowRegistry.list_all()):
        if catalog.is_user_workflow_id(wf.workflow_id):
            WorkflowRegistry.unregister(wf.workflow_id)


def test_persist_fetch_hydrate_delete_roundtrip(fake_redis: _FakeRedis) -> None:
    wf = _sample_workflow()
    wid = wf.workflow_id

    catalog.persist_user_workflow(wf)

    assert wid in catalog.list_user_workflow_ids("acme")
    raw = catalog.fetch_user_workflow_dict(wid, tenant_id="acme")
    assert isinstance(raw, dict)
    assert raw["workflow_id"] == wid
    assert fake_redis.counters.get(catalog.user_workflow_rev_key("acme"), 0) >= 1
    assert catalog.user_workflow_definition_key("acme", wid) in fake_redis.kv

    # Simulate a cold worker: clear local registry, then hydrate from Redis.
    if WorkflowRegistry.get(wid) is not None:
        WorkflowRegistry.unregister(wid)
    loaded = catalog.load_user_workflows_into_registry()
    assert loaded >= 1
    hydrated = WorkflowRegistry.get(wid)
    assert hydrated is not None
    assert hydrated.name == "Catalog brief"

    assert catalog.delete_user_workflow(wid, tenant_id="acme") is True
    assert wid not in catalog.list_user_workflow_ids("acme")
    assert catalog.fetch_user_workflow_dict(wid, tenant_id="acme") is None

    WorkflowRegistry.unregister(wid)
    assert catalog.load_user_workflows_into_registry() == 0
    assert WorkflowRegistry.get(wid) is None


def test_hydrate_skips_orphan_id_without_definition(fake_redis: _FakeRedis) -> None:
    orphan = "user.acme.missing_def"
    fake_redis.sadd(catalog.user_workflow_ids_key("acme"), orphan)

    loaded = catalog.load_user_workflows_into_registry()
    assert loaded == 0
    assert WorkflowRegistry.get(orphan) is None


def test_apply_sync_register_and_unregister(fake_redis: _FakeRedis) -> None:
    wf = _sample_workflow("user.acme.sync_brief")
    catalog.persist_user_workflow(wf)

    if WorkflowRegistry.get(wf.workflow_id) is not None:
        WorkflowRegistry.unregister(wf.workflow_id)

    registered = catalog.apply_user_workflow_sync(
        "register", wf.workflow_id, tenant_id="acme"
    )
    assert registered["op"] == "register"
    assert WorkflowRegistry.get(wf.workflow_id) is not None

    removed = catalog.apply_user_workflow_sync("unregister", wf.workflow_id)
    assert removed["removed"] is True
    assert WorkflowRegistry.get(wf.workflow_id) is None


def test_apply_sync_register_missing_raises(fake_redis: _FakeRedis) -> None:
    with pytest.raises(ValueError, match="not found"):
        catalog.apply_user_workflow_sync("register", "user.acme.does_not_exist")


def test_fan_out_mocked_does_not_require_workers(
    fake_redis: _FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "motet.core.bundles.deploy._resolve_live_targeted_workers",
        lambda *_a, **_k: [],
    )
    result = catalog.fan_out_user_workflow_sync(
        op="register",
        workflow_id="user.acme.catalog_brief",
    )
    assert result.get("acked") == []
    assert result.get("note") == "no live workers"


def test_builder_persist_writes_redis(
    fake_redis: _FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Builder register with persist=True / fan_out=False hits the catalog."""
    import motet.core.commands.builtin.tool  # noqa: F401
    import motet.core.commands.builtin.transform  # noqa: F401
    from motet.core.tools.builtin.math_eval import register as register_math
    from motet.core.tools.registry import ToolRegistry
    from motet.core.workflow.builder import run_workflow_builder

    reg = ToolRegistry()
    register_math(reg)
    yaml_text = """
workflow_id: builder_persist
name: Builder persist
required_inputs: [topic]
steps:
  calc:
    step_id: calc
    command_type: core.tool_execution
    command_data:
      tool_name: core.math_eval
      parameters:
        expression: "2+2"
    dependencies: []
"""
    result = run_workflow_builder(
        mode="register",
        yaml_text=yaml_text,
        scope_slug="acme",
        tool_registry=reg,
        persist=True,
        fan_out=False,
        principal_id="p1",
        tenant_id="t1",
    )
    assert result.ok, result.to_dict()
    wid = result.data["workflow_id"]
    assert wid.startswith("user.")
    raw = catalog.fetch_user_workflow_dict(wid, tenant_id="t1")
    assert raw is not None
    assert raw["workflow_id"] == wid
    key = catalog.user_workflow_definition_key("t1", wid)
    assert key in fake_redis.kv
    assert json.loads(fake_redis.kv[key])["name"] == "Builder persist"


def test_persist_requires_tenant(fake_redis: _FakeRedis) -> None:
    wf = _sample_workflow()
    wf.metadata = {"authored_by_principal_id": "principal-a"}
    with pytest.raises(ValueError, match="tenant_id is required"):
        catalog.persist_user_workflow(wf)


def test_tenant_a_cannot_fetch_tenant_b_definition(fake_redis: _FakeRedis) -> None:
    wf_a = _sample_workflow("user.acme.shared_name")
    wf_a.metadata = {"tenant_id": "acme", "authored_by_principal_id": "pa"}
    wf_b = _sample_workflow("user.acme.shared_name")
    wf_b.metadata = {"tenant_id": "beta", "authored_by_principal_id": "pb"}
    wf_b.name = "Beta brief"
    catalog.persist_user_workflow(wf_a)
    catalog.persist_user_workflow(wf_b)

    raw_a = catalog.fetch_user_workflow_dict("user.acme.shared_name", tenant_id="acme")
    raw_b = catalog.fetch_user_workflow_dict("user.acme.shared_name", tenant_id="beta")
    assert raw_a is not None and raw_a["name"] == "Catalog brief"
    assert raw_b is not None and raw_b["name"] == "Beta brief"
    assert catalog.fetch_user_workflow_dict("user.acme.shared_name", tenant_id="gamma") is None


def test_invoke_and_export_are_tenant_scoped(fake_redis: _FakeRedis) -> None:
    wf_a = _sample_workflow("user.acme.export_a")
    wf_b = _sample_workflow("user.beta.export_b")
    wf_b.metadata = {"tenant_id": "beta", "authored_by_principal_id": "pb"}
    catalog.persist_user_workflow(wf_a)
    catalog.persist_user_workflow(wf_b)
    catalog.load_user_workflows_into_registry()

    resolved = catalog.assert_user_workflow_invokable("user.acme.export_a", "acme")
    assert resolved.workflow_id == "user.acme.export_a"
    with pytest.raises(ValueError, match="not visible"):
        catalog.assert_user_workflow_invokable("user.acme.export_a", "beta")
    with pytest.raises(ValueError, match="requires tenant_id"):
        catalog.assert_user_workflow_invokable("user.acme.export_a", "")

    from motet.core.workflow import WorkflowRegistry

    names_acme = {
        s.name for s in WorkflowRegistry.export_canonical_schemas(tenant_id="acme")
    }
    names_beta = {
        s.name for s in WorkflowRegistry.export_canonical_schemas(tenant_id="beta")
    }
    assert "workflow_user.acme.export_a" in names_acme
    assert "workflow_user.beta.export_b" not in names_acme
    assert "workflow_user.beta.export_b" in names_beta
    assert "workflow_user.acme.export_a" not in names_beta
    hidden = {s.name for s in WorkflowRegistry.export_canonical_schemas()}
    assert "workflow_user.acme.export_a" not in hidden
    assert catalog.user_workflow_visible_to_tenant(wf_a, "acme") is True
    assert catalog.user_workflow_visible_to_tenant(wf_a, "beta") is False


def test_fetch_and_list_require_tenant(fake_redis: _FakeRedis) -> None:
    catalog.persist_user_workflow(_sample_workflow())
    assert catalog.fetch_user_workflow_dict("user.acme.catalog_brief") is None
    assert catalog.list_user_workflow_ids() == []
    assert catalog.list_user_workflow_ids("") == []


def test_list_visible_and_resolve_hide_other_tenants(fake_redis: _FakeRedis) -> None:
    wf_a = _sample_workflow("user.acme.shared_name")
    wf_a.metadata = {"tenant_id": "acme", "authored_by_principal_id": "pa"}
    wf_b = _sample_workflow("user.acme.shared_name")
    wf_b.metadata = {"tenant_id": "beta", "authored_by_principal_id": "pb"}
    wf_b.name = "Beta brief"
    catalog.persist_user_workflow(wf_a)
    catalog.persist_user_workflow(wf_b)
    catalog.load_user_workflows_into_registry()

    ids_acme = {w.workflow_id for w in catalog.list_visible_workflows("acme")}
    ids_beta = {w.workflow_id for w in catalog.list_visible_workflows("beta")}
    assert "user.acme.shared_name" in ids_acme
    assert "user.acme.shared_name" in ids_beta
    names_acme = {
        w.name for w in catalog.list_visible_workflows("acme") if catalog.is_user_workflow_id(w.workflow_id)
    }
    names_beta = {
        w.name for w in catalog.list_visible_workflows("beta") if catalog.is_user_workflow_id(w.workflow_id)
    }
    assert names_acme == {"Catalog brief"}
    assert names_beta == {"Beta brief"}
    assert not any(
        catalog.is_user_workflow_id(w.workflow_id)
        for w in catalog.list_visible_workflows("")
    )

    resolved_acme = catalog.resolve_visible_workflow("user.acme.shared_name", "acme")
    resolved_beta = catalog.resolve_visible_workflow("user.acme.shared_name", "beta")
    assert resolved_acme is not None and resolved_acme.name == "Catalog brief"
    assert resolved_beta is not None and resolved_beta.name == "Beta brief"
    assert catalog.resolve_visible_workflow("user.acme.shared_name", "gamma") is None
    assert catalog.resolve_visible_workflow(
        "user.acme.shared_name", "gamma", allow_catalog=False
    ) is None
    empty_caller = catalog.resolve_visible_workflow("user.acme.shared_name", "")
    assert empty_caller is not None
    assert empty_caller.workflow_id == "user.acme.shared_name"


def test_discovery_doc_id_is_tenant_qualified() -> None:
    assert (
        catalog.user_workflow_discovery_doc_id("acme", "user.alice.weekly_brief")
        == "workflow:acme:user.alice.weekly_brief"
    )
    assert (
        catalog.leftover_user_workflow_discovery_doc_id("user.alice.weekly_brief")
        == "workflow:user.alice.weekly_brief"
    )
