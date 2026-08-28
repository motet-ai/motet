"""
Motet - Image Stack Registry Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

ADR-0101 §"Platform-managed image stacks" — unit tests for
``motet.core.execution.image_stacks``.

The registry is the single source of truth for "is X a known stack and what
image does it resolve to?" — three downstream consumers depend on it
(validate-time lint, deployer build orchestration, ops UI), so the tests
pin both happy-path lookups and the env-loader edge cases (description
overrides, additions vs. overrides of builtins, malformed env keys).
"""

from __future__ import annotations

from typing import Dict

import pytest

from motet.core.execution.image_stacks import (
    ImageStack,
    is_known_stack,
    list_image_stacks,
    resolve_image_stack,
    resolve_image_stack_for_capabilities,
)


# ---------------------------------------------------------------------------
# Builtins always present
# ---------------------------------------------------------------------------


def test_builtins_present_in_empty_env() -> None:
    """All three ADR-0101 builtins MUST be in the registry even with no
    operator config — the names are referenced in docs and the lint
    accepts them as known."""
    stacks = list_image_stacks(env={})
    names = {s.name for s in stacks}
    assert {"python-minimal", "python-office", "python-browser"}.issubset(names)
    for s in stacks:
        if s.name in {"python-minimal", "python-office", "python-browser"}:
            assert s.builtin is True


def test_python_minimal_default_is_pinned() -> None:
    """python-minimal ships with python:3.11-slim as its default ref so that
    the deployer build is functional out of the box without any operator
    env config — the in-repo Dockerfile uses the same default."""
    stack = resolve_image_stack("python-minimal", env={})
    assert stack is not None
    assert stack.is_pinned
    assert stack.oci_image_ref == "python:3.11-slim"
    assert stack.capabilities == ("python",)


def test_aspirational_builtins_are_unpinned_by_default() -> None:
    """python-office / python-browser are reserved names but the platform
    can't ship them pre-pinned — operators must point them at real images."""
    for name in ("python-office", "python-browser"):
        stack = resolve_image_stack(name, env={})
        assert stack is not None
        assert stack.builtin is True
        assert stack.is_pinned is False
        assert stack.oci_image_ref == ""
        assert "python" in stack.capabilities


# ---------------------------------------------------------------------------
# Env overrides / additions
# ---------------------------------------------------------------------------


def test_env_override_updates_builtin_ref_keeps_description() -> None:
    """Operator pinning python-office shouldn't wipe its built-in description."""
    env: Dict[str, str] = {
        "MOTET_IMAGE_STACK_PYTHON_OFFICE": "registry.example.com/motet/office@sha256:" + "a" * 64,
    }
    stack = resolve_image_stack("python-office", env=env)
    assert stack is not None
    assert stack.builtin is True
    assert stack.is_pinned
    assert stack.oci_image_ref.startswith("registry.example.com/motet/office@sha256:")
    assert "LibreOffice" in stack.description  # builtin description preserved


def test_env_can_add_new_stack() -> None:
    """A name the platform doesn't know about becomes a non-builtin entry."""
    env: Dict[str, str] = {
        "MOTET_IMAGE_STACK_PYTHON_DATA_SCIENCE": (
            "registry.example.com/motet/python-ds@sha256:" + "b" * 64
        ),
        "MOTET_IMAGE_STACK_PYTHON_DATA_SCIENCE_DESCRIPTION": "Numpy, pandas, scikit-learn",
        "MOTET_IMAGE_STACK_PYTHON_DATA_SCIENCE_CAPABILITIES": "python,numpy,pandas",
    }
    stack = resolve_image_stack("python-data-science", env=env)
    assert stack is not None
    assert stack.builtin is False
    assert stack.is_pinned
    assert stack.description == "Numpy, pandas, scikit-learn"
    assert stack.capabilities == ("python", "numpy", "pandas")


def test_env_description_alone_does_not_pin() -> None:
    """Setting only the description for an unknown name is a no-op (we don't
    register a stack with no ref AND no builtin presence)."""
    env: Dict[str, str] = {
        "MOTET_IMAGE_STACK_NEVERLAND_DESCRIPTION": "ignored",
    }
    assert resolve_image_stack("neverland", env=env) is None


def test_env_description_overrides_builtin() -> None:
    """Operators can replace a builtin's description (e.g. internal docs link)."""
    env: Dict[str, str] = {
        "MOTET_IMAGE_STACK_PYTHON_MINIMAL_DESCRIPTION": "Acme corp curated min image",
    }
    stack = resolve_image_stack("python-minimal", env=env)
    assert stack is not None
    assert stack.description == "Acme corp curated min image"


def test_empty_env_value_unpins_builtin() -> None:
    """Setting MOTET_IMAGE_STACK_X="" explicitly unpins the stack — useful
    when an operator wants to force the lint to flag bundles using a stack
    they're decommissioning."""
    env: Dict[str, str] = {"MOTET_IMAGE_STACK_PYTHON_MINIMAL": ""}
    stack = resolve_image_stack("python-minimal", env=env)
    assert stack is not None
    assert stack.is_pinned is False


# ---------------------------------------------------------------------------
# Lookup behavior
# ---------------------------------------------------------------------------


def test_resolve_returns_none_for_unknown() -> None:
    assert resolve_image_stack("nope", env={}) is None
    assert resolve_image_stack("", env={}) is None


def test_is_known_stack_matches_resolve() -> None:
    """Convenience helper — must agree with resolve()."""
    assert is_known_stack("python-minimal", env={}) is True
    assert is_known_stack("nope", env={}) is False


def test_resolve_strips_whitespace() -> None:
    """Trailing whitespace on a stack name (e.g. operator typo in YAML)
    should still resolve — this matches how _lint_exec_config_file treats
    other string fields."""
    assert resolve_image_stack(" python-minimal ", env={}) is not None


# ---------------------------------------------------------------------------
# Sort order / shape
# ---------------------------------------------------------------------------


def test_list_is_sorted_by_name() -> None:
    """API/UI consumers rely on stable ordering."""
    env: Dict[str, str] = {
        "MOTET_IMAGE_STACK_ZZZ": "x",
        "MOTET_IMAGE_STACK_AAA": "y",
    }
    stacks = list_image_stacks(env=env)
    names = [s.name for s in stacks]
    assert names == sorted(names)


def test_image_stack_is_pinned_property() -> None:
    """is_pinned is the sole signal for "registered AND has a ref"."""
    assert ImageStack(name="x", oci_image_ref="x:1").is_pinned is True
    assert ImageStack(name="x", oci_image_ref="").is_pinned is False
    assert ImageStack(name="x", oci_image_ref="   ").is_pinned is False


def test_resolve_image_stack_for_capabilities_prefers_smallest_pinned_match() -> None:
    env: Dict[str, str] = {
        "MOTET_IMAGE_STACK_PYTHON_BROWSER": "registry.example.com/browser@sha256:" + "c" * 64,
        "MOTET_IMAGE_STACK_PYTHON_MEDIA": "registry.example.com/media@sha256:" + "d" * 64,
        "MOTET_IMAGE_STACK_PYTHON_MEDIA_CAPABILITIES": "python,ffmpeg",
    }

    browser = resolve_image_stack_for_capabilities(["python", "chromium"], env=env)
    assert browser.matched
    assert browser.stack is not None
    assert browser.stack.name == "python-browser"

    python = resolve_image_stack_for_capabilities(["python"], env=env)
    assert python.matched
    assert python.stack is not None
    assert python.stack.name == "python-minimal"


def test_resolve_image_stack_for_capabilities_reports_missing_when_unpinned() -> None:
    resolution = resolve_image_stack_for_capabilities(["libreoffice"], env={})

    assert not resolution.matched
    assert resolution.stack is None
    assert resolution.required_capabilities == ("libreoffice",)
    assert resolution.missing_capabilities == ("libreoffice",)


# ---------------------------------------------------------------------------
# Malformed env keys are ignored (defensive)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_key",
    [
        "MOTET_IMAGE_STACK_",                  # empty tail
        "SOMETHING_ELSE_PYTHON_MINIMAL",       # wrong prefix
        "MOTET_IMAGE_STACK_-LEADING-DASH",     # leading dash after underscore conv
    ],
)
def test_malformed_env_keys_ignored(bad_key: str) -> None:
    env = {bad_key: "x"}
    # Bad keys MUST NOT crash the registry; builtins must still be present.
    stacks = list_image_stacks(env=env)
    assert any(s.name == "python-minimal" for s in stacks)
