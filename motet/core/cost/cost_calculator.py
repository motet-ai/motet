"""
Motet - Cost Calculator

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Centralized cost calculation using canonical LLMUsage.

    This is the single source of truth for LLM cost calculations across
    the entire system. Used for budget enforcement and
    cost-aware routing.

Dependencies:
    - motet.core.types: Canonical LLMUsage type
    - motet.core.cost.pricing: ModelPricing and pricing registry
    - decimal: Precise cost calculations

Usage:
    from motet.core.cost import CostCalculator, get_cost_calculator
    from motet.core.types import LLMUsage
    
    # Get singleton calculator
    calculator = get_cost_calculator()
    
    # Calculate cost from canonical usage
    usage = LLMUsage(
        prompt_tokens=1000,
        output_tokens=500,
        cache_read_tokens=200,
        reasoning_tokens=100
    )
    cost_usd = calculator.calculate_cost_canonical(
        provider="openai",
        model="gpt-4o-mini",
        usage=usage,
        tenant_id="default"
    )

Notes:
    - calculate_cost_canonical is the cost API
    - calculate_model_cost wraps calculate_cost_canonical
    - Supports cache token discounts and reasoning token pricing
    - Returns 0.0 for unknown models or local (free) models
"""

from __future__ import annotations

from typing import Dict, Optional
import structlog

from ..types import LLMUsage
from .pricing import (
    ModelPricing,
    get_model_pricing,
    get_model_pricing_with_overrides,
)

logger = structlog.get_logger(__name__)


class CostCalculator:
    """
    Centralized cost calculation using canonical LLMUsage (ADR-0064).
    
    Pricing is sourced from ModelPricing (ADR-0018 §4.A) with optional overrides
    from tenant-specific pricing for enterprise customers with negotiated rates.
    
    This is the single source of truth for LLM cost calculations across
    the entire system.
    """
    
    def __init__(
        self,
        tenant_pricing_overrides: Optional[Dict[str, Dict[str, ModelPricing]]] = None,
    ):
        """
        Initialize CostCalculator.
        
        Args:
            tenant_pricing_overrides: Optional dict of tenant_id -> model -> ModelPricing
                                     for tenant-specific negotiated rates.
        """
        self._tenant_pricing_overrides = tenant_pricing_overrides or {}
    
    def calculate_cost_canonical(
        self,
        provider: str,
        model: str,
        usage: LLMUsage,
        tenant_id: Optional[str] = None,
        *,
        log_event: bool = True,
    ) -> float:
        """
        Calculate cost using canonical LLMUsage (ADR-0064 R9).
        
        Pricing is resolved from:
        1. Tenant-specific pricing overrides (if tenant_id provided and override exists)
        2. Default pricing registry
        3. Zero (if model not found or pricing is None - e.g., local models)
        
        Args:
            provider: LLM provider name (openai, anthropic, gemini, local)
            model: Model name
            usage: Canonical LLMUsage from LLMResponse
            tenant_id: Optional tenant for pricing override lookup
            log_event: Emit the ``cost_calculated`` debug event. Callers doing
                counterfactual or duplicate recomputations (cache-savings
                baseline, budget enforcement) pass False so the log stream has
                exactly one ``cost_calculated`` line per actual LLM call.
            
        Returns:
            Cost in USD (float)
        """
        # Get pricing, checking tenant overrides first
        tenant_overrides = None
        if tenant_id and tenant_id in self._tenant_pricing_overrides:
            tenant_overrides = self._tenant_pricing_overrides[tenant_id]
        
        pricing = get_model_pricing_with_overrides(
            provider=provider,
            model=model,
            tenant_pricing_overrides=tenant_overrides,
        )
        
        if pricing is None:
            # Model not found or free (local)
            logger.debug(
                "no_pricing_found",
                provider=provider,
                model=model,
                returning_zero_cost=True,
            )
            return 0.0
        
        # Extract canonical usage fields (with defaults)
        prompt_tokens = usage.prompt_tokens or 0
        output_tokens = usage.output_tokens or 0
        
        # Cache tokens (ADR-0064 R9 canonical fields)
        cache_read_tokens = usage.cache_read_tokens or 0
        cache_creation_tokens = usage.cache_creation_tokens or 0
        
        # Reasoning tokens (ADR-0064 R8 canonical field)
        reasoning_tokens = usage.reasoning_tokens or 0
        
        # Calculate input cost with cache discount
        # Non-cached input tokens: full price
        # Cached read tokens: discounted price
        # Cache creation tokens: may have separate rate
        non_cached_input = max(0, prompt_tokens - cache_read_tokens)
        
        cache_discount = float(pricing.cache_read_discount_pct) / 100.0
        cache_write_rate = pricing.cache_write_per_1k or pricing.input_per_1k
        
        input_cost = (
            # Non-cached input at full price
            (non_cached_input / 1000) * float(pricing.input_per_1k) +
            # Cached input at discounted price
            (cache_read_tokens / 1000) * float(pricing.input_per_1k) * (1 - cache_discount) +
            # Cache creation at cache write rate
            (cache_creation_tokens / 1000) * float(cache_write_rate)
        )
        
        # Calculate output cost with reasoning tokens
        # Regular output tokens: output rate
        # Reasoning tokens: reasoning rate (defaults to output rate)
        reasoning_rate = pricing.reasoning_per_1k or pricing.output_per_1k
        
        output_cost = (
            (output_tokens / 1000) * float(pricing.output_per_1k) +
            (reasoning_tokens / 1000) * float(reasoning_rate)
        )
        
        total_cost = input_cost + output_cost
        
        # Calculate cache savings for analytics
        cache_savings = (cache_read_tokens / 1000) * float(pricing.input_per_1k) * cache_discount
        
        if log_event:
            logger.debug(
                "cost_calculated",
                provider=provider,
                model=model,
                prompt_tokens=prompt_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                reasoning_tokens=reasoning_tokens,
                input_cost=round(input_cost, 8),
                output_cost=round(output_cost, 8),
                total_cost=round(total_cost, 8),
                cache_savings=round(cache_savings, 8),
            )
        
        return total_cost
    
    def calculate_cost_without_cache_discount(
        self,
        provider: str,
        model: str,
        usage: LLMUsage,
        tenant_id: Optional[str] = None,
    ) -> float:
        """
        Calculate what cost would be without cache discount.
        
        Useful for calculating cache savings metrics.
        
        Args:
            provider: LLM provider name
            model: Model name
            usage: Canonical LLMUsage from LLMResponse
            tenant_id: Optional tenant for pricing override lookup
            
        Returns:
            Cost in USD (float) without cache discount applied
        """
        # Create usage without cache fields
        full_usage = LLMUsage(
            prompt_tokens=usage.prompt_tokens,
            output_tokens=usage.output_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            # Zero out cache fields
            cache_read_tokens=0,
            cache_creation_tokens=usage.cache_creation_tokens,
        )
        # Counterfactual baseline: suppress the log event, otherwise every call
        # emits a second cost_calculated line with cache_read_tokens=0 that
        # looks like a cache miss.
        return self.calculate_cost_canonical(
            provider, model, full_usage, tenant_id, log_event=False
        )
    
    def calculate_cache_savings(
        self,
        provider: str,
        model: str,
        usage: LLMUsage,
        tenant_id: Optional[str] = None,
    ) -> float:
        """
        Calculate cost savings from prompt caching.
        
        Args:
            provider: LLM provider name
            model: Model name
            usage: Canonical LLMUsage from LLMResponse
            tenant_id: Optional tenant for pricing override lookup
            
        Returns:
            Cache savings in USD (float)
        """
        actual_cost = self.calculate_cost_canonical(provider, model, usage, tenant_id)
        full_cost = self.calculate_cost_without_cache_discount(provider, model, usage, tenant_id)
        return full_cost - actual_cost
    
    # =========================================================================
    # Legacy Methods (for backwards compatibility)
    # =========================================================================
    
    def calculate_model_cost(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        """
        Legacy method - prefer calculate_cost_canonical().
        
        Calculates cost from raw token counts without cache/reasoning support.
        
        Args:
            provider: Provider name
            model: Model name
            input_tokens: Input/prompt token count
            output_tokens: Output/completion token count
            
        Returns:
            Cost in USD
        """
        usage = LLMUsage(prompt_tokens=input_tokens, output_tokens=output_tokens)
        return self.calculate_cost_canonical(provider, model, usage)
    
    def calculate_model_cost_with_cache(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
    ) -> float:
        """
        Legacy method - prefer calculate_cost_canonical().
        
        Calculates cost with cache token support.
        
        Args:
            provider: Provider name
            model: Model name
            input_tokens: Input/prompt token count
            output_tokens: Output/completion token count
            cached_input_tokens: Tokens read from cache
            
        Returns:
            Cost in USD
        """
        usage = LLMUsage(
            prompt_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cached_input_tokens,
        )
        return self.calculate_cost_canonical(provider, model, usage)


# =============================================================================
# Singleton Instance
# =============================================================================

_cost_calculator_instance: Optional[CostCalculator] = None


def get_cost_calculator() -> CostCalculator:
    """
    Get the singleton CostCalculator instance.
    
    Returns:
        CostCalculator singleton instance
    """
    global _cost_calculator_instance
    if _cost_calculator_instance is None:
        _cost_calculator_instance = CostCalculator()
    return _cost_calculator_instance


def configure_cost_calculator(
    tenant_pricing_overrides: Optional[Dict[str, Dict[str, ModelPricing]]] = None,
) -> CostCalculator:
    """
    Configure and return the singleton CostCalculator instance.
    
    Call this at application startup to configure tenant-specific pricing.
    
    Args:
        tenant_pricing_overrides: Dict of tenant_id -> model -> ModelPricing
        
    Returns:
        Configured CostCalculator singleton instance
    """
    global _cost_calculator_instance
    _cost_calculator_instance = CostCalculator(
        tenant_pricing_overrides=tenant_pricing_overrides,
    )
    return _cost_calculator_instance


__all__ = [
    "CostCalculator",
    "get_cost_calculator",
    "configure_cost_calculator",
]
