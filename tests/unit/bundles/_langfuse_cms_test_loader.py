"""
Motet - Shared loader for langfuse-cms bundle unit tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-05

Description:
Loads langfuse-cms bundle modules under their canonical package
names for unit tests, mirroring the runtime bundle loader hierarchy.

Dependencies:
- importlib.util / types / sys / pathlib

Usage:
  from _langfuse_cms_test_loader import load_command_module, load_helper_module

  lf = load_helper_module()
  cmd = load_command_module("agent_turn_with_langfuse_prompt")
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
BUNDLE_ROOT = ROOT / "motet-sdk" / "examples" / "bundles" / "langfuse-cms"
BUNDLE_COMMANDS = BUNDLE_ROOT / "commands"
BUNDLE_TOOLS = BUNDLE_ROOT / "tools"

_PACKAGES = (
    ("bundle", None),
    ("bundle.langfuse-cms", None),
    ("bundle.langfuse-cms.commands", str(BUNDLE_COMMANDS)),
    ("bundle.langfuse-cms.tools", str(BUNDLE_TOOLS)),
)


def ensure_bundle_package() -> None:
    """Register bundle package hierarchy for relative / importlib imports."""
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


def load_helper_module() -> Any:
    """Load commands/_langfuse.py under its canonical package name."""
    return _load("bundle.langfuse-cms.commands", BUNDLE_COMMANDS, "_langfuse")


def load_command_module(stem: str) -> Any:
    """Load a command module; ensures the shared helper is available first."""
    load_helper_module()
    return _load("bundle.langfuse-cms.commands", BUNDLE_COMMANDS, stem)


def load_tool_module(stem: str) -> Any:
    """Load a tool module; ensures the shared helper is available first."""
    load_helper_module()
    return _load("bundle.langfuse-cms.tools", BUNDLE_TOOLS, stem)
