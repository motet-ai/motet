"""
Motet - Provider Adapter Registry

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Registry for provider adapter factories.

    This mirrors `motet.core.models.registry.ModelRegistry` but for adapters.
    Adapters are keyed by (provider, adapter_name) — e.g. ("openai", "responses").

Dependencies:
    - motet.core.types: BaseRegistry protocol
    - motet.core.models.adapters.base: LLMProviderAdapter
    - typing: Callable factories and mappings

Usage:
    from motet.core.models.adapters import adapter_registry
    adapter_registry.register("openai", "responses", factory=...)
    adapter = adapter_registry.build("openai", "responses", credentials={...})

Notes:
    - Registration should occur at startup/import time (like model registry).
    - Adapters are not cached; build() returns a new instance.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from ...types import BaseRegistry
from .base import LLMProviderAdapter


class AdapterRegistry(BaseRegistry[LLMProviderAdapter]):
    """Registry for adapter factories keyed by (provider, adapter_name)."""

    def __init__(self) -> None:
        self._factories: Dict[Tuple[str, str], Callable[..., LLMProviderAdapter]] = {}

    def register(self, key1: str, key2: str, factory: Callable[..., LLMProviderAdapter], **metadata: Any) -> None:
        self._factories[(key1, key2)] = factory

    def build(self, key1: str, key2: str, **kwargs: Any) -> LLMProviderAdapter:
        key = (key1, key2)
        if key not in self._factories:
            raise KeyError(f"No adapter registered for provider={key1} adapter_name={key2}")
        return self._factories[key](provider=key1, adapter_name=key2, **kwargs)

    def get(self, key1: str, key2: str) -> Optional[Callable[..., LLMProviderAdapter]]:
        return self._factories.get((key1, key2))

    def list(self, key1_filter: Optional[str] = None) -> List[Tuple[str, str]]:
        if key1_filter is None:
            return sorted(self._factories.keys())
        return sorted([k for k in self._factories.keys() if k[0] == key1_filter])

    def supports(self, key1: str, key2: str) -> bool:
        return (key1, key2) in self._factories


adapter_registry = AdapterRegistry()

