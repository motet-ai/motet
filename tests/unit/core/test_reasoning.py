"""
Motet - Reasoning Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Unit tests for deleted reasoning entry points: the adaptive package and
    the leftover ``reasoning`` command.

Dependencies:
    - pytest: test framework

Usage:
    pytest tests/unit/core/test_reasoning.py
"""

import importlib

import pytest


@pytest.mark.unit
def test_adaptive_package_is_gone():
    """The turn no longer has a second reasoning entry point."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("motet.core.reasoning.adaptive")


@pytest.mark.unit
def test_reasoning_command_is_gone():
    """Chat turns use agent_turn; there is no leftover reasoning command."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("motet.core.commands.builtin.reasoning")
