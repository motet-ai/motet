"""
Motet - Hot Deploy Command Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-21

Description:
Unit tests for the dev-only hot deploy command path in deploy.py,
including restart recovery (#125): stale-worker bypass of no_change and
hot-mode propagate_bundle.

Dependencies:
- pytest: Test framework
- motet.core.bundles.deploy: hot_deploy_bundle command and data model

Usage:
pytest tests/unit/core/orchestration/test_deploy_hot_command.py

Notes:
- Uses lightweight fake Redis and fake MotetContext behavior.
- Avoids integration dependencies (workers/redis services).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from motet.core.bundles import deploy as deploy_mod


class _FakeRedis:
    """Small in-memory subset of Redis commands used by hot_deploy_bundle."""

    def __init__(self) -> None:
        self._strings: Dict[str, Any] = {}
        self._hashes: Dict[str, Dict[str, Any]] = {}

    def set(self, key: str, value: Any, ex: Optional[int] = None) -> None:
        self._strings[key] = value

    def get(self, key: str) -> Any:
        return self._strings.get(key)

    def hset(self, key: str, field: Any = None, value: Any = None, mapping: Optional[Dict[str, Any]] = None) -> None:
        self._hashes.setdefault(key, {})
        if mapping is not None:
            self._hashes[key].update(mapping)
            return
        self._hashes[key][field] = value

    def hgetall(self, key: str) -> Dict[str, Any]:
        return dict(self._hashes.get(key, {}))

    def expire(self, key: str, seconds: int) -> None:
        # TTL behavior is not required for these unit tests.
        return None


class _FakeMotet:
    """Minimal motet object for hot_deploy_bundle tests."""

    def __init__(self, redis_client: _FakeRedis) -> None:
        self.redis = redis_client
        self.command_id = "cmd-hot-1"

    def apply(self, _command: Any, inputs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # Simulate successful reload on each worker.
        return [
            {
                "registered_commands": [f"{item['bundle_id']}.hello_world"],
                "registered_tools": [],
            }
            for item in inputs
        ]


def _write_min_bundle(root: Path, name: str = "hello-world") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.yaml").write_text(
        f'format_version: "1"\nname: "{name}"\nversion: "0.1.0"\n',
        encoding="utf-8",
    )
    (root / "commands").mkdir(exist_ok=True)
    (root / "commands" / "hello_world.py").write_text(
        "from motet.core.commands.decorator import distributed_command\n"
        "from pydantic import BaseModel\n"
        "class Input(BaseModel):\n"
        "    value: str\n"
        "@distributed_command()\n"
        "def hello_world(data: Input):\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )


def test_hot_deploy_bundle_happy_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """hot_deploy_bundle dispatches worker hot reload and returns complete status."""
    monkeypatch.setenv("MOTET_ENABLE_HOT_DEPLOY", "true")

    bundle_root = tmp_path / "bundle"
    _write_min_bundle(bundle_root)

    fake_redis = _FakeRedis()
    fake_motet = _FakeMotet(fake_redis)

    monkeypatch.setattr(deploy_mod, "get_motet_context", lambda: fake_motet)
    monkeypatch.setattr(deploy_mod, "_resolve_live_targeted_workers", lambda _r, _t: ["worker-a", "worker-b"])

    result = deploy_mod.hot_deploy_bundle.__wrapped__(
        deploy_mod.HotDeployBundleData(bundle_path=str(bundle_root), lint=False)
    )

    assert result["deploy_status"] == deploy_mod.BundleDeployStatus.COMPLETE.value
    assert result["bundle_id"] == "hello-world"
    assert result["acked_workers"] == ["worker-a", "worker-b"]
    assert result["failed_workers"] == []
    assert "hello-world.hello_world" in result["catalog"].get("commands", [])


def test_hot_deploy_bundle_no_change(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """hot_deploy_bundle short-circuits with no_change for unchanged local content."""
    monkeypatch.setenv("MOTET_ENABLE_HOT_DEPLOY", "true")

    bundle_root = tmp_path / "bundle"
    _write_min_bundle(bundle_root)

    fake_redis = _FakeRedis()
    fake_motet = _FakeMotet(fake_redis)
    monkeypatch.setattr(deploy_mod, "get_motet_context", lambda: fake_motet)
    monkeypatch.setattr(deploy_mod, "_resolve_live_targeted_workers", lambda _r, _t: ["worker-a"])
    # Readiness unavailable → staleness check is a no-op; hash short-circuit applies.
    monkeypatch.setattr(deploy_mod, "_hot_workers_stale_after_restart", lambda *_a, **_k: False)

    first = deploy_mod.hot_deploy_bundle.__wrapped__(
        deploy_mod.HotDeployBundleData(bundle_path=str(bundle_root), lint=False)
    )
    assert first["deploy_status"] == deploy_mod.BundleDeployStatus.COMPLETE.value

    second = deploy_mod.hot_deploy_bundle.__wrapped__(
        deploy_mod.HotDeployBundleData(bundle_path=str(bundle_root), lint=False)
    )
    assert second["deploy_status"] == deploy_mod.BundleDeployStatus.NO_CHANGE.value
    assert second["acked_workers"] == []


def test_hot_deploy_forces_reload_when_workers_stale_after_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same content hash still reloads when live workers restarted after loaded_at (#125)."""
    monkeypatch.setenv("MOTET_ENABLE_HOT_DEPLOY", "true")

    bundle_root = tmp_path / "bundle"
    _write_min_bundle(bundle_root)

    fake_redis = _FakeRedis()
    fake_motet = _FakeMotet(fake_redis)
    monkeypatch.setattr(deploy_mod, "get_motet_context", lambda: fake_motet)
    monkeypatch.setattr(deploy_mod, "_resolve_live_targeted_workers", lambda _r, _t: ["worker-a"])

    first = deploy_mod.hot_deploy_bundle.__wrapped__(
        deploy_mod.HotDeployBundleData(bundle_path=str(bundle_root), lint=False)
    )
    assert first["deploy_status"] == deploy_mod.BundleDeployStatus.COMPLETE.value

    monkeypatch.setattr(deploy_mod, "_hot_workers_stale_after_restart", lambda *_a, **_k: True)
    second = deploy_mod.hot_deploy_bundle.__wrapped__(
        deploy_mod.HotDeployBundleData(bundle_path=str(bundle_root), lint=False)
    )
    assert second["deploy_status"] == deploy_mod.BundleDeployStatus.COMPLETE.value
    assert second["acked_workers"] == ["worker-a"]


def test_hot_workers_stale_after_restart_compares_startup_to_loaded_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Staleness is true when worker startup_time is newer than loaded_at."""
    from types import SimpleNamespace

    fake_redis = _FakeRedis()
    deploy_mod._store_worker_state(
        fake_redis,
        "hello-world",
        "worker-a",
        registered_commands=["hello-world.hello_world"],
        registered_tools=[],
    )
    # Force an old loaded_at so startup looks newer.
    state = deploy_mod._get_worker_state(fake_redis, "hello-world")
    state["worker-a"]["loaded_at"] = "2020-01-01T00:00:00Z"
    fake_redis.hset(
        deploy_mod._worker_state_key("hello-world"),
        "worker-a",
        __import__("json").dumps(state["worker-a"]),
    )

    class _FakeSvc:
        def get_worker_info(self, worker_id: str) -> Any:
            assert worker_id == "worker-a"
            return SimpleNamespace(startup_time=1_700_000_000.0)

    monkeypatch.setattr(
        "motet.core.distributed.worker_readiness.WorkerReadinessService",
        lambda: _FakeSvc(),
    )
    assert deploy_mod._hot_workers_stale_after_restart(
        fake_redis, "hello-world", ["worker-a"]
    )


def test_propagate_bundle_hot_mode_uses_hot_reload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """propagate_bundle dispatches hot_reload_bundle for mode=hot registries (#125)."""
    bundle_root = tmp_path / "bundle"
    _write_min_bundle(bundle_root)

    fake_redis = _FakeRedis()
    fake_motet = _FakeMotet(fake_redis)
    deploy_mod._record_hot_deploy_metadata(
        redis_client=fake_redis,
        bundle_id="hello-world",
        bundle_version="abc123",
        source_fingerprint=f"hot:{bundle_root}",
        targeting=None,
        deploy_job_id="job-1",
        status=deploy_mod.BundleDeployStatus.COMPLETE.value,
    )

    applied: List[Any] = []

    def _apply(command: Any, inputs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        applied.append(command)
        assert inputs[0]["bundle_path"] == str(bundle_root)
        return [{"registered_commands": ["hello-world.hello_world"], "registered_tools": []}]

    fake_motet.apply = _apply  # type: ignore[method-assign]
    monkeypatch.setattr(deploy_mod, "get_motet_context", lambda: fake_motet)
    monkeypatch.setattr(deploy_mod, "_resolve_live_targeted_workers", lambda _r, _t: ["worker-a"])

    from motet.core.bundles.bundle_reload import hot_reload_bundle

    result = deploy_mod.propagate_bundle.__wrapped__(
        deploy_mod.PropagateBundleData(bundle_id="hello-world")
    )
    assert result["deploy_status"] == deploy_mod.BundleDeployStatus.COMPLETE.value
    assert result["mode"] == "hot"
    assert result["acked_workers"] == ["worker-a"]
    assert applied == [hot_reload_bundle]
