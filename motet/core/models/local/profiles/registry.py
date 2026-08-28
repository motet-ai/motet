"""
Motet - Llama.cpp Model Profile Registry

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Registry for resolving local model identifiers to llama.cpp family profiles.
    Resolves local model identifiers with longest-substring family matching.

Dependencies:
    - DefaultLlamaCppModelProfile: Generic profile implementation for families whose
      templates work with native system/tool handling.
    - Gemma4Profile, HermesProfile, Llama3Profile, Phi4Profile, QwenProfile:
      Specialized family implementations.

Usage:
    from motet.core.models.local.profiles.registry import profile_for_model

    profile = profile_for_model("gemma-4-e4b")
    stops = profile.stop_sequences()

Notes:
    - Family keys are sorted longest-first during resolution so ``gemma-4`` wins
      over ``gemma`` and ``ministral`` wins over ``mistral``.
    - The registry returns singleton profile instances because profiles are
      stateless.
"""

from __future__ import annotations

from typing import Dict, Optional

from .base import DefaultLlamaCppModelProfile, LlamaCppModelProfile
from .gemma4 import Gemma4Profile
from .hermes import HermesProfile
from .llama3 import Llama3Profile
from .phi import Phi4Profile
from .qwen import QwenProfile


DEFAULT_PROFILE = DefaultLlamaCppModelProfile()

_PROFILES: Dict[str, LlamaCppModelProfile] = {
    "hermes": HermesProfile(),
    "gemma-4": Gemma4Profile(),
    "gemma": DefaultLlamaCppModelProfile(
        family="gemma",
        chat_format="gemma",
        stops=["<end_of_turn>"],
        supports_system=False,
    ),
    "phi-4": Phi4Profile(),
    "phi-3": DefaultLlamaCppModelProfile(
        family="phi-3",
        chat_format="phi-3",
        stops=["<|end|>"],
        supports_system=True,
    ),
    "llama-3": Llama3Profile(),
    "ministral": DefaultLlamaCppModelProfile(
        family="ministral",
        chat_format="mistral-instruct",
        stops=["</s>"],
        supports_system=True,
    ),
    "mistral": DefaultLlamaCppModelProfile(
        family="mistral",
        chat_format="mistral-instruct",
        stops=["</s>"],
        supports_system=True,
    ),
    "mixtral": DefaultLlamaCppModelProfile(
        family="mixtral",
        chat_format="mistral-instruct",
        stops=["</s>"],
        supports_system=True,
    ),
    "qwen": QwenProfile(),
}


def resolve_local_model_family(model_id: Optional[str]) -> Optional[str]:
    """Resolve a local model id to a known profile family key."""
    if not model_id:
        return None
    name = model_id.lower()
    for family in sorted(_PROFILES.keys(), key=len, reverse=True):
        if family in name:
            return family
    return None


def profile_for_family(family: Optional[str]) -> LlamaCppModelProfile:
    """Return the profile for a family key, or the default profile."""
    if not family:
        return DEFAULT_PROFILE
    return _PROFILES.get(family, DEFAULT_PROFILE)


def profile_for_model(model_id: Optional[str]) -> LlamaCppModelProfile:
    """Return the profile for a local model id."""
    return profile_for_family(resolve_local_model_family(model_id))


def registered_profiles() -> Dict[str, LlamaCppModelProfile]:
    """Return a copy of registered profile singletons for tests/introspection."""
    return dict(_PROFILES)
