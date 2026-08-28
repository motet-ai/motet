"""
Motet - Unit-test package fixtures

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-21

Description:
    Package-wide autouse helpers for ``tests/unit/``. Restores ``motet_sdk*``
    ``sys.modules`` (and critical attribute bindings) after each test when the
    ADR-0080 runtime bridge is still installed, so a caller of ``_load_bundle``
    / ``_inject_motet_sdk_runtime_bridge`` cannot poison later files (issue
    #116). Production worker reload still injects without restore; this fixture
    is test-only.

Dependencies:
    - pytest
    - motet.core.bundles.bundle_reload (lazy import)

Usage:
    Collected automatically under ``tests/unit/`` — no imports required.

Notes:
    Snapshot is a cheap dict capture; restore runs only when the runtime
    ``distributed_command`` is bound on ``motet_sdk`` / ``motet_sdk.command``,
    so legitimate mid-test imports of the real SDK are left alone.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, Iterator, Optional

import pytest


def _runtime_bridge_is_active() -> bool:
    """True when inject left the runtime decorator on motet_sdk sys.modules."""
    try:
        from motet.core.commands.decorator import (
            distributed_command as runtime_distributed_command,
        )
    except Exception:
        return False

    for key in ("motet_sdk.command", "motet_sdk"):
        mod: Optional[Any] = sys.modules.get(key)
        if mod is None:
            continue
        if getattr(mod, "distributed_command", None) is runtime_distributed_command:
            return True
    return False


@pytest.fixture(autouse=True)
def _restore_motet_sdk_runtime_bridge_after_unit_test() -> Iterator[None]:
    """
    Safety net: snapshot motet_sdk module map before each unit test; if the
    runtime bridge is still installed after the test, restore the snapshot.
    """
    from motet.core.bundles.bundle_reload import (
        restore_motet_sdk_runtime_bridge,
        snapshot_motet_sdk_runtime_bridge,
    )

    snapshot: Dict[str, Any] = snapshot_motet_sdk_runtime_bridge()
    try:
        yield
    finally:
        if _runtime_bridge_is_active():
            restore_motet_sdk_runtime_bridge(snapshot)
