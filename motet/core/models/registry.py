"""
Motet - Model Registry

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Model registry for managing model specifications and providing access to
    capabilities metadata. Execution is handled by adapters; this registry is
    spec-only and does not build provider objects.

Dependencies:
    - typing: Type hints and collections
    - specs: Model specifications and registry

Usage:
    from motet.core.models.registry import model_registry, get_model_spec
    
    # Check capabilities/specs
    spec = get_model_spec("openai", "gpt-4o-mini")
    if model_registry.supports("openai", "gpt-4o-mini", "stream"):
        print("Model supports streaming")
    
    # List all models
    models = model_registry.list()

Notes:
    - Spec-only registry; adapters are used for execution
    - Thread-safe for reads, registration should happen at startup
"""

from __future__ import annotations

from typing import Dict, List, Optional
from .specs import ModelSpec, MODEL_REGISTRY


class ModelRegistry:
    """
    Registry for model specifications.
    
    Manages spec lookup and capability checks.
    """
    
    def register_spec(self, provider: str, name: str, spec: ModelSpec) -> None:
        """
        Register a model specification.
        
        Args:
            provider: Model provider name
            name: Model identifier
            spec: Model specification to register
        """
        MODEL_REGISTRY.setdefault(provider, {})[name] = spec

    def list(self, provider: Optional[str] = None) -> List[ModelSpec]:
        """
        List available models.
        
        Args:
            provider: Optional provider filter
            
        Returns:
            List of model specifications
        """
        return list_models(provider)

    def get_spec(self, provider: str, name: str) -> Optional[ModelSpec]:
        """
        Get model specification.
        
        Args:
            provider: Model provider name
            name: Model identifier
            
        Returns:
            Model specification or None if not found
        """
        return get_model_spec(provider, name)

    def supports(self, provider: str, name: str, capability: str) -> bool:
        """
        Check if model supports a capability.
        
        Args:
            provider: Model provider name
            name: Model identifier
            capability: Capability to check (e.g., "stream", "vision")
            
        Returns:
            True if model supports capability, False otherwise
        """
        spec = self.get_spec(provider, name)
        return bool(spec and capability in spec.capabilities)


# Global model registry instance
model_registry = ModelRegistry()


def list_models(provider: Optional[str] = None) -> List[ModelSpec]:
    """
    List available models.
    
    Args:
        provider: Optional provider filter
        
    Returns:
        List of model specifications
    """
    if provider:
        return list(MODEL_REGISTRY.get(provider, {}).values())
    out: List[ModelSpec] = []
    for prov in MODEL_REGISTRY.values():
        out.extend(prov.values())
    return out


def list_models_with_keys(provider: Optional[str] = None) -> List[tuple[str, str, ModelSpec]]:
    """
    List available models with (provider, registry_key, spec).
    Registry key is the unique selection key (may differ from spec.name for aliases).
    
    Args:
        provider: Optional provider filter
        
    Returns:
        List of (provider, registry_key, spec)
    """
    out: List[tuple[str, str, ModelSpec]] = []
    for prov_name, prov_models in MODEL_REGISTRY.items():
        if provider and prov_name != provider:
            continue
        for key, spec in prov_models.items():
            out.append((prov_name, key, spec))
    return out


def get_model_spec(provider: str, name: str) -> Optional[ModelSpec]:
    """
    Get model specification.
    
    Args:
        provider: Model provider name
        name: Model identifier
        
    Returns:
        Model specification or None if not found
    """
    return MODEL_REGISTRY.get(provider, {}).get(name)


def model_supports(provider: str, name: str, capability: str) -> bool:
    """
    Check if model supports a capability.
    
    Args:
        provider: Model provider name
        name: Model identifier
        capability: Capability to check
        
    Returns:
        True if model supports capability, False otherwise
    """
    spec = get_model_spec(provider, name)
    return bool(spec and (capability in spec.capabilities))


__all__ = [
    "ModelRegistry",
    "model_registry",
    "list_models",
    "list_models_with_keys",
    "get_model_spec",
    "model_supports",
]

