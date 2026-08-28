"""
Motet - Llama.cpp Model Profiles

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-04

Description:
    Family-specific llama.cpp model profile package. Profiles define how each
    local GGUF model family maps Motet's canonical-ish local request dictionaries
    onto the prompt and tool semantics expected by llama.cpp chat templates.

Dependencies:
    - base: Shared profile protocol and default behavior.
    - registry: Family resolution and singleton profile lookup.

Usage:
    from motet.core.models.local.profiles import profile_for_model

    profile = profile_for_model("phi-4-mini")

Notes:
    - Importing this package is lightweight and does not load model engines.
    - Profiles are stateless and safe to reuse across requests.
"""

from __future__ import annotations

from .base import DefaultLlamaCppModelProfile, LlamaCppModelProfile
from .registry import (
    profile_for_family,
    profile_for_model,
    registered_profiles,
    resolve_local_model_family,
)

__all__ = [
    "DefaultLlamaCppModelProfile",
    "LlamaCppModelProfile",
    "profile_for_family",
    "profile_for_model",
    "registered_profiles",
    "resolve_local_model_family",
]
