"""
Motet - Bundle reload integration test for runner-driven tools (ADR-0101 Slice B).

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-21

Description:
    Drives ``_load_bundle`` against the on-disk basic-skill-example bundle
    and verifies that runner-driven tools registered from
    ``skills/<dir>/runners.yaml`` show up in the loaded["tools"] list and
    the live tool registry. This is the closest unit-level test to a real
    deploy: the only thing skipped is the artifact-fetch / unpack path.

    Also restores the motet_sdk runtime bridge after ``_load_bundle`` so this
    module cannot poison later unit files that expect the SDK no-op decorator
    (issue #116).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator
from unittest.mock import patch

import pytest

from motet.core.bundles.bundle_reload import (
    _load_bundle,
    _remove_tree_if_present,
    restore_motet_sdk_runtime_bridge,
    snapshot_motet_sdk_runtime_bridge,
)
from motet.core.tools import registry as tool_registry


REPO_ROOT = Path(__file__).resolve().parents[4]
BUNDLE_DIR = REPO_ROOT / "tests" / "bundles" / "basic-skill-example"
BUNDLE_ID = "basic-skill-example"
SKILL_NAME = "basic-script-skill"
RUNNER_NAME = "echo"
TOOL_NAME = f"{BUNDLE_ID}.{SKILL_NAME}.{RUNNER_NAME}"


@pytest.fixture(autouse=True)
def cleanup_runner_tool() -> Iterator[None]:
    """Make sure the test does not leak the registered tool to siblings."""
    yield
    try:
        tool_registry.unregister(TOOL_NAME)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def restore_motet_sdk_bridge_after_load_bundle() -> Iterator[None]:
    """
    ``_load_bundle`` injects the ADR-0080 runtime bridge without restore.

    Snapshot/restore around each test so orchestration → app-builder command
    ordering stays isolated (issue #116). Production worker reload is unchanged.
    """
    snapshot: Dict[str, Any] = snapshot_motet_sdk_runtime_bridge()
    try:
        yield
    finally:
        restore_motet_sdk_runtime_bridge(snapshot)


def test_load_bundle_registers_runner_tool_and_includes_in_tools_list() -> None:
    assert BUNDLE_DIR.is_dir(), "expected on-disk basic-skill-example bundle"

    loaded = _load_bundle(
        bundle_id=BUNDLE_ID,
        bundle_dir=BUNDLE_DIR,
        targeting_raw=None,
        bundle_version="0.1.0",
    )

    assert TOOL_NAME in loaded["tools"], loaded
    tool = tool_registry.get(TOOL_NAME)
    assert tool is not None
    assert tool.name == TOOL_NAME
    schema = tool.tool_schema.model_json_schema()
    assert "text" in schema["properties"]


def test_remove_tree_if_present_tolerates_concurrent_removal(tmp_path: Path) -> None:
    path = tmp_path / "bundle"
    path.mkdir()

    with patch("motet.core.bundles.bundle_reload.shutil.rmtree", side_effect=FileNotFoundError("assets")):
        _remove_tree_if_present(path)
