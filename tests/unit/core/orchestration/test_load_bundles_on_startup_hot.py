"""
Motet - Hot Bundle Startup Catch-Up Tests (#125)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-21

Description:
Unit tests for load_bundles_on_startup() hot-mode rehydration and
hot_reload_bundle version-marker writes (issue #125).

Dependencies:
- pytest
- motet.core.bundles.bundle_reload

Usage:
pytest tests/unit/core/orchestration/test_load_bundles_on_startup_hot.py -v
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from motet.core.bundles import bundle_reload


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


def test_hot_reload_writes_bundle_version_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """hot_reload_bundle persists .bundle_version for startup re-register (#125)."""
    source = tmp_path / "src"
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    _write_min_bundle(source)

    monkeypatch.setattr(bundle_reload, "PLUGIN_ROOT", plugin)
    monkeypatch.setattr(
        bundle_reload,
        "_load_bundle",
        lambda *_a, **_k: {"commands": ["hello-world.hello_world"], "tools": []},
    )
    monkeypatch.setattr(bundle_reload, "_prune_stale_bundle_registrations", lambda *_a, **_k: None)
    monkeypatch.setattr(bundle_reload, "_refresh_search_index", lambda **_k: None)

    result = bundle_reload.hot_reload_bundle.__wrapped__(
        bundle_reload.HotReloadBundleData(
            bundle_id="hello-world",
            bundle_version="ver-1",
            bundle_path=str(source),
        )
    )
    assert result["load_status"] == "loaded"
    marker = plugin / "hello-world" / ".bundle_version"
    assert marker.exists()
    assert marker.read_text() == "ver-1"


def test_load_bundles_on_startup_hot_from_plugin_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Hot registry with no artifact reloads from PLUGIN_ROOT when source gone."""
    plugin = tmp_path / "plugin"
    bundle_dir = plugin / "hello-world"
    _write_min_bundle(bundle_dir)
    monkeypatch.setattr(bundle_reload, "PLUGIN_ROOT", plugin)

    entries = [
        {
            "bundle_id": "hello-world",
            "bundle_version": "ver-hot",
            "mode": "hot",
            "source_fingerprint": "hot:/missing/path/does-not-exist",
        }
    ]
    monkeypatch.setattr(
        "motet.core.distributed.redis_manager.get_sync_redis_client",
        lambda: MagicMock(),
    )
    monkeypatch.setattr(
        "motet.core.bundles.deploy._list_all_bundles",
        lambda _r: entries,
    )
    monkeypatch.setattr(
        "motet.core.workers.worker_utils.get_worker_id",
        lambda: "worker-a",
    )

    loaded_calls: List[str] = []

    def _load(bundle_id: str, path: Path, *_a: Any, **_k: Any) -> Dict[str, Any]:
        loaded_calls.append(bundle_id)
        assert path == bundle_dir
        return {"commands": ["hello-world.hello_world"], "tools": []}

    monkeypatch.setattr(bundle_reload, "_load_bundle", _load)
    monkeypatch.setattr(bundle_reload, "_prune_stale_bundle_registrations", lambda *_a, **_k: None)
    monkeypatch.setattr(bundle_reload, "_refresh_search_index", lambda **_k: None)

    count = bundle_reload.load_bundles_on_startup()
    assert count == 1
    assert loaded_calls == ["hello-world"]
    assert (bundle_dir / ".bundle_version").read_text() == "ver-hot"


def test_load_bundles_on_startup_hot_copies_from_fingerprint_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Hot registry copies from hot:<path> when PLUGIN_ROOT is empty."""
    source = tmp_path / "src"
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    _write_min_bundle(source)
    monkeypatch.setattr(bundle_reload, "PLUGIN_ROOT", plugin)

    entries = [
        {
            "bundle_id": "hello-world",
            "bundle_version": "ver-hot-2",
            "mode": "hot",
            "source_fingerprint": f"hot:{source}",
        }
    ]
    monkeypatch.setattr(
        "motet.core.distributed.redis_manager.get_sync_redis_client",
        lambda: MagicMock(),
    )
    monkeypatch.setattr(
        "motet.core.bundles.deploy._list_all_bundles",
        lambda _r: entries,
    )
    monkeypatch.setattr(
        "motet.core.workers.worker_utils.get_worker_id",
        lambda: "worker-a",
    )
    monkeypatch.setattr(
        bundle_reload,
        "_load_bundle",
        lambda *_a, **_k: {"commands": ["hello-world.hello_world"], "tools": []},
    )
    monkeypatch.setattr(bundle_reload, "_prune_stale_bundle_registrations", lambda *_a, **_k: None)
    monkeypatch.setattr(bundle_reload, "_refresh_search_index", lambda **_k: None)

    count = bundle_reload.load_bundles_on_startup()
    assert count == 1
    assert (plugin / "hello-world" / "manifest.yaml").exists()
    assert (plugin / "hello-world" / ".bundle_version").read_text() == "ver-hot-2"
