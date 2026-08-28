"""
Motet - Lazy Export Target Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-02

Description:
    Verifies that every `_LAZY_IMPORTS` entry in the package `__init__` files
    actually resolves. Those maps address their targets as *strings*
    (`"Command": ("motet.core.commands", "Command")`), so when a module moves,
    nothing reports the break: the linter sees a string literal, import-time
    execution never touches the entry, and the attribute only fails when
    something reads it at runtime.

    That is not hypothetical. Extracting the command framework into
    `motet.core.commands` silently broke `motet.core.orchestration.Command`,
    `CommandContext`, and `CommandStatus`, and the full unit suite stayed
    green. A separate pre-existing break — `motet.tracing`, pointing at a
    submodule that had never been imported — had gone unnoticed since the
    March 2026 rebrand.

Dependencies:
    - ast: reads the lazy maps without importing the packages first
    - importlib: resolves each declared target

Usage:
    pytest tests/unit/test_lazy_export_targets.py

Notes:
    - Discovers the maps by parsing, not by importing, so a package whose
      `__init__` raises still gets its declarations checked.
    - Accepts a target that names a submodule as well as one that names an
      attribute, matching what the loaders accept.
"""

import ast
import importlib
from pathlib import Path
from typing import List, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _lazy_import_entries() -> List[Tuple[str, int, str, str]]:
    """Parse every `_LAZY_IMPORTS` mapping under `motet/` into flat entries."""
    entries: List[Tuple[str, int, str, str]] = []
    for init_path in REPO_ROOT.glob("motet/**/__init__.py"):
        try:
            tree = ast.parse(init_path.read_text())
        except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if "_LAZY_IMPORTS" not in names:
                continue
            if not isinstance(node.value, ast.Dict):
                continue
            for value in node.value.values:
                if not isinstance(value, ast.Tuple) or len(value.elts) != 2:
                    continue
                if not all(
                    isinstance(e, ast.Constant) and isinstance(e.value, str)
                    for e in value.elts
                ):
                    continue
                module_name, attr_name = (e.value for e in value.elts)
                rel = init_path.relative_to(REPO_ROOT).as_posix()
                entries.append((rel, value.lineno, module_name, attr_name))
    return entries


LAZY_ENTRIES = _lazy_import_entries()


def test_lazy_import_maps_are_discovered():
    """Guard the parser itself: silently finding nothing would pass every test."""
    assert LAZY_ENTRIES, "no _LAZY_IMPORTS entries found - parser is out of date"


@pytest.mark.parametrize(
    "source_file,lineno,module_name,attr_name",
    LAZY_ENTRIES,
    ids=[f"{m}.{a}" for _, _, m, a in LAZY_ENTRIES],
)
def test_lazy_export_target_resolves(source_file, lineno, module_name, attr_name):
    """Each declared lazy target must import and expose its symbol."""
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        pytest.fail(
            f"{source_file}:{lineno} declares '{module_name}' but it does not "
            f"import: {exc}"
        )

    if hasattr(module, attr_name):
        return

    # The loaders also accept a target naming a submodule, which is not an
    # attribute of its parent until something imports it.
    try:
        importlib.import_module(f"{module_name}.{attr_name}")
    except ImportError:
        pytest.fail(
            f"{source_file}:{lineno} declares '{attr_name}' in '{module_name}', "
            f"but it is neither an attribute nor an importable submodule. "
            f"A module probably moved without this string being updated."
        )
