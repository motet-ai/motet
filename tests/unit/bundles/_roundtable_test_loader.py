"""
Motet - Shared loader for roundtable bundle unit tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
Loads roundtable bundle modules under their canonical package names
(``bundle.roundtable.tools.<stem>``) for unit tests, mirroring what the bundle
loader (``motet/core/bundles/bundle_reload.py``) does at runtime so the
relative import of ``._transcript`` inside the tools resolves identically.

Dependencies:
- importlib.util: spec_from_file_location loading of bundle module files
- types / sys: namespace-style parent package registration in sys.modules
- pathlib: repo-root-relative path to the example bundle directory

Usage:
  from _roundtable_test_loader import load_tool_module

  mod = load_tool_module("invite")
  result = mod.invite({"agent_id": "roundtable.researcher", "question": "..."})

Notes:
- Mirrors _expert_panel_test_loader; kept separate so each bundle registers
  only its own package paths.
- The bundle has no commands directory (the facilitator calls agents.turn via
  its tools), so only the tools package is registered.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
BUNDLE_ROOT = ROOT / "motet-sdk" / "examples" / "bundles" / "roundtable"
BUNDLE_TOOLS = BUNDLE_ROOT / "tools"

_PACKAGES = (
    ("bundle", None),
    ("bundle.roundtable", None),
    ("bundle.roundtable.tools", str(BUNDLE_TOOLS)),
)


def ensure_bundle_package() -> None:
    """Register bundle / bundle.roundtable / tools packages."""
    for name, path in _PACKAGES:
        module = sys.modules.get(name)
        if module is None:
            module = types.ModuleType(name)
            sys.modules[name] = module
        if not hasattr(module, "__path__"):
            module.__path__ = []  # type: ignore[attr-defined]
        if path and path not in module.__path__:  # type: ignore[operator]
            module.__path__.append(path)  # type: ignore[attr-defined]


def load_tool_module(stem: str) -> Any:
    """Load a roundtable tool module under its canonical package name."""
    ensure_bundle_package()
    name = f"bundle.roundtable.tools.{stem}"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, BUNDLE_TOOLS / f"{stem}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
