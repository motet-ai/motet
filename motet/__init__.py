"""
Motet - Main Package

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Main package for the Motet - a comprehensive distributed AI framework
    designed with logical modular architecture for building sophisticated AI systems
    with reasoning, memory, and multi-agent capabilities.

Dependencies:
    - Core orchestration and agent modules
    - Type definitions and configuration
    - Observability and tracing

Usage:
    from motet import MotetStack, Message
    
    stack = MotetStack()
    response = await stack.chat([Message(role="user", content="Hello!")])

Notes:
    - Exposes main MotetStack class and core types
    - ``__version__`` is the product version from package metadata
      (root pyproject.toml), not a per-file or FSL-conversion date
"""

from typing import Any
import importlib

from .core import types as _types
from ._version import get_version

__version__ = get_version()
__author__ = "Matt Chisholm"
__email__ = "matt@motet.dev"

__all__ = [
    "MotetStack",
    "Config",
    "Message",
    "Response",
    "Tool",
    "MemoryItem",
    "AgentCapability",
    "ModelProvider",
    "tracing",
    "motet",
]

_LAZY_IMPORTS = {
    "MotetStack": ("motet.core.stack", "MotetStack"),
    "Config": ("motet.core.config", "Config"),
    "tracing": ("motet.core.observability", "trace_store"),
    "motet": ("motet.core.commands.motet_namespace", "motet"),
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
    raise AttributeError(f"module 'motet' has no attribute '{name}'")