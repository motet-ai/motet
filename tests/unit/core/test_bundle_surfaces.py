"""
Motet - Bundle Surfaces Deploy Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-08

Description:
    Unit tests for config/surfaces.yaml extraction and register_if_absent
    deploy behavior (existing surfaces are no-ops).

Usage:
    pytest tests/unit/core/test_bundle_surfaces.py -q
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Set

import pytest

from motet.core.bundles import deploy as deploy_mod
from motet.core.surfaces import registry as sr


class _FakeRedis:
    def __init__(self) -> None:
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.sets: Dict[str, Set[str]] = {}
        self.kv: Dict[str, str] = {}

    def exists(self, key: str) -> int:
        return 1 if key in self.hashes or key in self.sets or key in self.kv else 0

    def hset(
        self, key: str, mapping: Optional[Dict[str, Any]] = None, **kwargs: Any
    ) -> int:
        data = dict(mapping or {})
        data.update(kwargs)
        bucket = self.hashes.setdefault(key, {})
        for field, value in data.items():
            bucket[str(field)] = "" if value is None else str(value)
        return len(data)

    def hgetall(self, key: str) -> Dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def sadd(self, key: str, *members: str) -> int:
        bucket = self.sets.setdefault(key, set())
        before = len(bucket)
        bucket.update(str(m) for m in members)
        return len(bucket) - before

    def smembers(self, key: str) -> Set[str]:
        return set(self.sets.get(key, set()))

    def srem(self, key: str, *members: str) -> int:
        bucket = self.sets.get(key)
        if not bucket:
            return 0
        removed = 0
        for member in members:
            if str(member) in bucket:
                bucket.remove(str(member))
                removed += 1
        return removed

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self.hashes.pop(key, None) is not None:
                removed += 1
            if self.sets.pop(key, None) is not None:
                removed += 1
            if self.kv.pop(key, None) is not None:
                removed += 1
        return removed

    def get(self, key: str) -> Optional[str]:
        return self.kv.get(key)

    def set(self, key: str, value: str) -> bool:
        self.kv[key] = str(value)
        return True


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    fake = _FakeRedis()
    monkeypatch.setattr(sr, "get_sync_redis_client", lambda _client_id: fake)
    return fake


def test_extract_bundle_surfaces_from_yaml() -> None:
    files = {
        "config/surfaces.yaml": b"""
surfaces:
  - id: partner_portal
    display_name: Partner Portal
    description: Partner channel
  - id: memo_review
    display_name: Memo Review
"""
    }
    surfaces = deploy_mod._extract_bundle_surfaces("acme", files, strict=True)
    assert [s["id"] for s in surfaces] == ["memo_review", "partner_portal"]
    assert surfaces[1]["display_name"] == "Partner Portal"
    assert surfaces[1]["bundle_id"] == "acme"


def test_extract_bundle_surfaces_invalid_id_strict() -> None:
    # Dots are rejected; hyphens are valid (kebab-case product surfaces).
    files = {"config/surfaces.yaml": b"surfaces:\n  - id: bad.id\n"}
    with pytest.raises(ValueError, match="Invalid bundle surface"):
        deploy_mod._extract_bundle_surfaces("acme", files, strict=True)


def test_register_bundle_surfaces_creates_then_noops() -> None:
    surfaces = [
        {
            "id": "partner_portal",
            "display_name": "Partner Portal",
            "description": "v1",
            "bundle_id": "acme",
        }
    ]
    first = deploy_mod._register_bundle_surfaces("acme", surfaces)
    assert first == {"created": 1, "skipped": 0}

    registry = sr.SurfaceRegistry()
    record = registry.get("partner_portal")
    assert record.display_name == "Partner Portal"
    assert record.created_by == "bundle:acme"

    # Redeploy with different display_name must be a no-op
    surfaces[0]["display_name"] = "Partner Portal v2"
    second = deploy_mod._register_bundle_surfaces("acme", surfaces)
    assert second == {"created": 0, "skipped": 1}
    assert registry.get("partner_portal").display_name == "Partner Portal"


def test_catalog_includes_surfaces() -> None:
    files = {
        "manifest.yaml": b"name: acme\nversion: 0.0.1\n",
        "config/surfaces.yaml": b"surfaces:\n  - id: partner_portal\n",
    }
    catalog = deploy_mod._extract_bundle_catalog("acme", files)
    assert catalog["surfaces"][0]["id"] == "partner_portal"
