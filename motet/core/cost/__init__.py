"""
Motet - Cost Tracking Package

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Cost tracking, budget enforcement, and cost-aware routing for the
Motet distributed framework.

    Implements (Distributed Cost Tracking Architecture).

Dependencies:
    - pydantic: Data validation for pricing and cost models
    - motet.core.types: Canonical LLMUsage for cost calculation
    - motet.core.distributed.redis_manager: Redis storage for cost data

Usage:
    from motet.core.cost import (
        CostCalculator,
        get_cost_calculator,
        ModelPricing,
    )
    
    # Calculate cost using canonical LLMUsage
    calculator = get_cost_calculator()
    cost_usd = calculator.calculate_cost_canonical(
        provider="openai",
        model="gpt-4o-mini",
        usage=response.usage,
        tenant_id="default"
    )

Notes:
    - CostCalculator is the single source of truth for LLM cost calculations
    - Pricing is sourced from ModelPricing configuration (not hardcoded)
    - Supports tenant-specific pricing overrides via ModelProfile
    - Used by (budget enforcement and cost-aware routing)
"""

from .pricing import (
    ModelPricing,
    get_model_pricing,
    get_model_pricing_with_overrides,
)
from .cost_calculator import (
    CostCalculator,
    get_cost_calculator,
)
from .budget_enforcer import (
    EnforcementAction,
    BudgetExceededError,
    BudgetEnforcer,
    get_budget_enforcer,
)
from .cost_tracking_service import (
    CostTrackingService,
    get_cost_tracking_service,
)
from .tracking_hooks import (
    track_model_result,
    check_budget_before_inference,
)

__all__ = [
    # Pricing models
    "ModelPricing",
    "get_model_pricing",
    "get_model_pricing_with_overrides",
    # Cost calculator
    "CostCalculator",
    "get_cost_calculator",
    # Budget enforcement
    "EnforcementAction",
    "BudgetExceededError",
    "BudgetEnforcer",
    "get_budget_enforcer",
    # Cost tracking service
    "CostTrackingService",
    "get_cost_tracking_service",
    # Tracking hooks
    "track_model_result",
    "check_budget_before_inference",
]
