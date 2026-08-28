"""
Motet - Model Management

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-27

Description:
    Model management system for the Motet distributed framework.
    Provides model registry, specification, and inference capabilities.

Dependencies:
    - pydantic: Data validation and serialization
    - abc: Abstract base classes
    - typing: Type hints and annotations
    - Model provider integrations

Usage:
    from motet.core.models import model_registry
    from motet.core.models.adapters import adapter_registry
    
    # Use model registry for specs
    spec = model_registry.get_spec("openai", "gpt-4o-mini")
    
    # Execute via adapters
    adapter = adapter_registry.build("openai", "responses", credentials={...})

Notes:
    - Supports multiple model providers (Mock, Local)
    - Includes model specification and metadata
    - Provides unified inference interface
    - Integrates with distributed architecture
    - Refactored into modular structure (2025-10-19)
"""

from __future__ import annotations

# Base classes and specs
from .specs import (
    ModelSpec,
    MODEL_REGISTRY,
    CAP_STREAM,
    CAP_VISION,
    CAP_TOOL_USE,
    CAP_JSON_MODE,
    CAP_SYSTEM_PROMPT,
    CAP_REASONING,
    CAP_PROMPT_CACHING,
)

# Registry and utilities
from .registry import (
    ModelRegistry,
    model_registry,
    list_models,
    get_model_spec,
    model_supports,
)

__all__ = [
    # Specs
    "ModelSpec",
    "MODEL_REGISTRY",
    "CAP_STREAM",
    "CAP_VISION",
    "CAP_TOOL_USE",
    "CAP_JSON_MODE",
    "CAP_SYSTEM_PROMPT",
    "CAP_REASONING",
    "CAP_PROMPT_CACHING",
    # Registry
    "ModelRegistry",
    "model_registry",
    "list_models",
    "get_model_spec",
    "model_supports",
]
