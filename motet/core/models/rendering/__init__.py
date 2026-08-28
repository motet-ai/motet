"""
Motet - Model Message Rendering Package

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Canonical multimodal rendering entrypoints for model message payloads.
    Exposes `get_renderer()` to materialize artifact-backed media parts into a provider-agnostic form.

Dependencies:
    - motet.core.models.rendering.base: Renderer protocol
    - motet.core.models.rendering.canonical: Canonical renderer implementation

Usage:
    from motet.core.models.rendering import get_renderer
    renderer = get_renderer("canonical")
"""

from __future__ import annotations

from .base import ProviderMessageRenderer
from .canonical import CanonicalMultimodalRenderer


def get_renderer(provider: str) -> ProviderMessageRenderer:
    """Return a canonical multimodal renderer for the given provider name."""
    p = (provider or "").strip().lower()
    if p == "canonical":
        return CanonicalMultimodalRenderer()

    raise ValueError(f"Unsupported provider for multimodal rendering: {provider}")


__all__ = ["ProviderMessageRenderer", "CanonicalMultimodalRenderer", "get_renderer"]


