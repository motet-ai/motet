"""
Motet - Shared loader for deep-research bundle unit tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-03

Description:
Loads deep-research bundle modules under their canonical package names
(``bundle.deep-research.commands.<stem>`` / ``...tools.<stem>``) for unit
tests. Bundle modules use relative imports (``from .search_source import
search_source``), so their parent packages must be registered in
``sys.modules`` as namespace-style packages with a ``__path__`` pointing at
the bundle directory — mirroring what the bundle loader
(``motet/core/bundles/bundle_reload.py``) does at runtime.

Dependencies:
- importlib.util: spec_from_file_location loading of bundle module files
- types / sys: namespace-style parent package registration in sys.modules
- pathlib: repo-root-relative path to the example bundle directories

Usage:
  from _deep_research_test_loader import load_command_module, load_tool_module

  mod = load_command_module("plan_queries")
  result = mod.plan_queries(mod.PlanQueriesData(topic="x"), mock_motet)

Notes:
- Mirrors _app_builder_test_loader; kept separate so each bundle registers
  only its own package paths.
- ``load_*_module`` pops any cached module for the stem first so each call
  executes fresh code; already-cached sibling modules are reused by relative
  imports, same as the runtime loader behavior.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
BUNDLE_ROOT = ROOT / "motet-sdk" / "examples" / "bundles" / "deep-research"
BUNDLE_COMMANDS = BUNDLE_ROOT / "commands"
BUNDLE_TOOLS = BUNDLE_ROOT / "tools"

_PACKAGES = (
    ("bundle", None),
    ("bundle.deep-research", None),
    ("bundle.deep-research.commands", str(BUNDLE_COMMANDS)),
    ("bundle.deep-research.tools", str(BUNDLE_TOOLS)),
)


def ensure_bundle_package() -> None:
    """Register bundle / bundle.deep-research / commands / tools packages."""
    for name, path in _PACKAGES:
        module = sys.modules.get(name)
        if module is None:
            module = types.ModuleType(name)
            sys.modules[name] = module
        if not hasattr(module, "__path__"):
            module.__path__ = []  # type: ignore[attr-defined]
        if path and path not in module.__path__:  # type: ignore[operator]
            module.__path__.append(path)  # type: ignore[attr-defined]


def _load(package: str, directory: Path, stem: str) -> Any:
    ensure_bundle_package()
    name = f"{package}.{stem}"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, directory / f"{stem}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_command_module(stem: str) -> Any:
    """Load a deep-research command module under its canonical package name."""
    return _load("bundle.deep-research.commands", BUNDLE_COMMANDS, stem)


def load_tool_module(stem: str) -> Any:
    """Load a deep-research tool module under its canonical package name."""
    return _load("bundle.deep-research.tools", BUNDLE_TOOLS, stem)
