"""
Motet - Core Module

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Core module for the Motet distributed framework.
    Provides essential imports and re-exports for the core package.

Dependencies:
    - Type definitions and configuration
    - Memory management and orchestration
    - Observability and tracing

Usage:
    from motet.core import MotetStack, Message, Config

Notes:
    - Re-exports core types and classes
    - Central module for core functionality
"""

from typing import Any
import importlib

from . import types as _types

__all__ = list(getattr(_types, "__all__", [])) + [
    "Config",
    "InMemoryStore",
    "MotetStack",
    "tracing",
]

_LAZY_IMPORTS = {
    "Config": ("motet.core.config", "Config"),
    "InMemoryStore": ("motet.core.memory", "InMemoryStore"),
    "MotetStack": ("motet.core.stack", "MotetStack"),
    "tracing": ("motet.core.observability", "trace_store"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_IMPORTS:
        module_name, attr_name = _LAZY_IMPORTS[name]
        module = importlib.import_module(module_name)
        try:
            return getattr(module, attr_name)
        except AttributeError:
            # The target may be a submodule nothing has imported yet, in which
            # case it is not yet an attribute of its parent package.
            return importlib.import_module(f"{module_name}.{attr_name}")
    if hasattr(_types, name):
        return getattr(_types, name)
    raise AttributeError(f"module 'motet.core' has no attribute '{name}'")


