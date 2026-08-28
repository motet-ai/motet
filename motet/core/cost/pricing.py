"""
Motet - Model Pricing Configuration

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Pricing configuration for LLM models. Pricing is stored in ModelSpec.pricing
    as the single source of truth.

    Implements §4.A (ModelSpec-Based Pricing) with support for:
    - Per-model base pricing (input/output tokens)
    - Cache pricing with configurable discounts
    - Reasoning token pricing for o1/Claude thinking models
    - Tenant-specific pricing overrides via ModelProfile

Dependencies:
    - pydantic: Data validation for pricing models
    - decimal: Precise cost calculations

Usage:
    from motet.core.cost.pricing import ModelPricing, get_model_pricing
    
    # Get pricing for a model (from ModelSpec.pricing)
    pricing = get_model_pricing("openai", "gpt-4o-mini")
    if pricing:
        input_cost = (tokens / 1000) * float(pricing.input_per_1k)

Notes:
    - All prices are in USD per 1000 tokens
    - Use Decimal for precision in pricing calculations
    - Local models have pricing=None (free)
    - Cache discounts reduce input token costs (Anthropic: 90%, OpenAI: 50%)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, Optional

from pydantic import BaseModel, Field
import structlog

logger = structlog.get_logger(__name__)


class ModelPricing(BaseModel):
    """
    Pricing configuration for a model (per 1K tokens).
    
    Stored in ModelSpec.pricing as the single source of truth.
    Can be overridden per-tenant via ModelProfile.pricing_overrides.
    
    Attributes:
        input_per_1k: Cost per 1K input/prompt tokens (USD)
        output_per_1k: Cost per 1K output/completion tokens (USD)
        cache_read_discount_pct: Discount percentage for cached input tokens (0-100)
        cache_write_per_1k: Cost per 1K tokens written to cache (defaults to input rate)
        reasoning_per_1k: Cost per 1K reasoning/thinking tokens (defaults to output rate)
        batch_input_per_1k: Optional batch API input pricing
        batch_output_per_1k: Optional batch API output pricing
        effective_date: ISO date when this pricing became effective
    """
    
    # Base token pricing (per 1K tokens, in USD)
    input_per_1k: Decimal = Field(..., description="Cost per 1K input tokens (USD)")
    output_per_1k: Decimal = Field(..., description="Cost per 1K output tokens (USD)")
    
    # Cache pricing (ADR-0064 R9)
    cache_read_discount_pct: Decimal = Field(
        default=Decimal("50.0"),
        description="Discount percentage for cached input tokens (0-100). "
                    "Anthropic: 90%, OpenAI: 50%."
    )
    cache_write_per_1k: Optional[Decimal] = Field(
        default=None,
        description="Cost per 1K tokens written to cache (defaults to input rate)"
    )
    
    # Reasoning/thinking pricing (ADR-0064 R8)
    reasoning_per_1k: Optional[Decimal] = Field(
        default=None,
        description="Cost per 1K reasoning tokens (defaults to output rate). "
                    "Used for o1/o3 and Claude extended thinking."
    )
    
    # Batch pricing (if provider supports)
    batch_input_per_1k: Optional[Decimal] = None
    batch_output_per_1k: Optional[Decimal] = None
    
    # Effective date for pricing versioning
    effective_date: Optional[str] = Field(
        default=None,
        description="ISO date when this pricing became effective (YYYY-MM-DD)"
    )
    
    model_config = {"frozen": True}


# =============================================================================
# Pricing Lookup Functions
# =============================================================================


def get_model_pricing(provider: str, model: str) -> Optional[ModelPricing]:
    """
    Get pricing for a model from ModelSpec (ADR-0018 §4.A).
    
    ModelSpec.pricing is the single source of truth for model pricing.
    No fallbacks - if pricing is not set on ModelSpec, returns None.
    
    Args:
        provider: Provider name (openai, anthropic, local, etc.)
        model: Model name
        
    Returns:
        ModelPricing if found in ModelSpec, None if not found or model is free (local)
    """
    try:
        from ..models.registry import get_model_spec
        
        spec = get_model_spec(provider, model)
        if spec and spec.pricing:
            logger.debug(
                "using_modelspec_pricing",
                provider=provider,
                model=model,
            )
            return spec.pricing
        
        # No pricing on ModelSpec - model is free or pricing not configured
        logger.debug(
            "no_modelspec_pricing",
            provider=provider,
            model=model,
        )
        return None
        
    except ImportError:
        # models.registry not available (e.g., during testing)
        logger.warning(
            "models_registry_not_available",
            provider=provider,
            model=model,
        )
        return None
    except Exception as e:
        logger.warning(
            "modelspec_pricing_lookup_failed",
            provider=provider,
            model=model,
            error=str(e),
        )
        return None


def get_model_pricing_with_overrides(
    provider: str,
    model: str,
    tenant_pricing_overrides: Optional[Dict[str, ModelPricing]] = None,
) -> Optional[ModelPricing]:
    """
    Get pricing for a model, checking tenant overrides first.
    
    Resolution order:
    1. tenant_pricing_overrides[model] (tenant-specific negotiated rates)
    2. ModelSpec.pricing (from model registry)
    3. None (not found or free)
    
    Args:
        provider: Provider name
        model: Model name
        tenant_pricing_overrides: Optional tenant-specific pricing dict
        
    Returns:
        ModelPricing if found, None if not found or model is free
    """
    # Check tenant-specific pricing override first
    if tenant_pricing_overrides and model in tenant_pricing_overrides:
        logger.debug(
            "using_tenant_pricing_override",
            provider=provider,
            model=model,
        )
        return tenant_pricing_overrides[model]
    
    # Fall back to default pricing
    return get_model_pricing(provider, model)


__all__ = [
    "ModelPricing",
    "get_model_pricing",
    "get_model_pricing_with_overrides",
]
