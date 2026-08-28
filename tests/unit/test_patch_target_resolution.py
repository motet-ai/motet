"""
Motet - Mock Patch Target Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-02

Description:
    Verifies that every literal `patch("motet...")` target in the test suite
    still resolves to a real module and attribute.

    `mock.patch` addresses its target as a string, so moving a module leaves
    the string pointing at nothing. The failure mode is bad: `patch` raises at
    test *runtime*, so a stale target either fails a test for a reason
    unrelated to what it covers, or — if the test was already going to be
    skipped or the patch sits on a branch that is not taken — reports nothing
    at all. Neither the linter nor a passing suite tells you a move broke
    something.

    This matters most during package reorganization, when many module paths
    change at once and the patch strings are the references least likely to be
    caught by a search-and-replace over imports.

Dependencies:
    - ast: finds patch call sites without executing test modules
    - importlib: resolves the longest importable prefix of each target

Usage:
    pytest tests/unit/test_patch_target_resolution.py

Notes:
    - Only literal first arguments to `patch(...)` are checked. Targets built
      from f-strings or module-level constants are skipped: resolving them
      means evaluating test module state, which would mean importing it.
    - A prefix that raises something other than ModuleNotFoundError (a missing
      optional dependency, say) is treated as unverifiable rather than failed,
      so an absent extra does not turn into a spurious failure here.
    - KNOWN_STALE quarantines targets that were already broken before this
      test existed. The list is asserted not to grow; entries should be fixed
      and removed, not appended to.
"""

import ast
import importlib
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = REPO_ROOT / "tests"

# Pre-existing breakage, quarantined when this guard was added (2026-08-02).
# These reference modules and attributes that no longer exist; they are not
# regressions from any current work. Fix and delete - do not extend.
KNOWN_STALE = {
    # motet.core.tools.mcp_manager / mcp_discovery were removed; the MCP path
    # now runs through mcp_motet.proxy.
    "motet.core.tools.mcp_manager.libtmux",
    "motet.core.tools.mcp_manager.LibTmuxMCPManager",
    "motet.core.tools.mcp_discovery.get_mcp_auto_discovery_service",
    # state_registry no longer exposes get_redis_client; the fixture holding
    # this also calls `pytest.mock.patch`, which is not a real API.
    "motet.core.distributed.state_registry.get_redis_client",
}


def _literal_patch_targets(tree: ast.AST) -> Iterator[Tuple[str, int]]:
    """Yield (target, lineno) for each literal `patch("motet...")` first arg."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        else:
            continue
        # patch.object takes (target_obj, "attr") - not a dotted string path.
        if name != "patch":
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            if first.value.startswith("motet."):
                yield first.value, node.lineno


def _collect() -> List[Tuple[str, int, str]]:
    found: List[Tuple[str, int, str]] = []
    for path in TESTS_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - surfaces as a collection error
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        for target, lineno in _literal_patch_targets(tree):
            found.append((rel, lineno, target))
    return found


PATCH_TARGETS = _collect()
CHECKABLE = [t for t in PATCH_TARGETS if t[2] not in KNOWN_STALE]


def _why_unresolvable(dotted: str) -> Optional[str]:
    """Return a reason string if `dotted` does not resolve, else None."""
    parts = dotted.split(".")
    module = None
    boundary = 0
    for i in range(len(parts), 0, -1):
        try:
            module = importlib.import_module(".".join(parts[:i]))
            boundary = i
            break
        except ModuleNotFoundError:
            continue
        except Exception:
            # Importable in principle but unavailable here (optional extra,
            # missing service). Not something this test can adjudicate.
            return None
    if module is None:
        return "no importable module prefix"

    obj = module
    for part in parts[boundary:]:
        if not hasattr(obj, part):
            return f"'{part}' not found on {'.'.join(parts[:boundary])}"
        obj = getattr(obj, part)
    return None


def test_patch_targets_are_discovered():
    """Guard the parser: finding nothing would make every other case vacuous."""
    assert len(PATCH_TARGETS) > 100, (
        f"only {len(PATCH_TARGETS)} patch targets found - the AST scan is "
        f"probably out of date with how tests call patch()"
    )


def test_known_stale_list_has_not_grown():
    """KNOWN_STALE is a shrinking quarantine, not a dumping ground."""
    assert len(KNOWN_STALE) <= 4, (
        "KNOWN_STALE grew. A newly broken patch target should be fixed at the "
        "call site rather than quarantined here."
    )


@pytest.mark.parametrize(
    "source_file,lineno,target",
    CHECKABLE,
    ids=[f"{f.rsplit('/', 1)[-1]}:{ln}" for f, ln, _ in CHECKABLE],
)
def test_patch_target_resolves(source_file, lineno, target):
    """Each literal patch target must name a real module and attribute."""
    reason = _why_unresolvable(target)
    assert reason is None, (
        f"{source_file}:{lineno} patches '{target}' but {reason}. "
        f"If a module moved, update this string - mock.patch will not tell "
        f"you at import time."
    )
