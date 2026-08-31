"""
Motet - Cost Tracking Hooks

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-29

Description:
    Helper functions to integrate cost tracking into model inference results.

    These hooks can be called after model inference to:
    1. Track costs in Redis
    2. Record usage for budget enforcement

    Designed to be non-blocking and failure-tolerant.

Dependencies:
    - motet.core.types: Canonical LLMUsage type
    - motet.core.cost: CostCalculator, BudgetEnforcer, CostTrackingService

Usage:
    from motet.core.cost.tracking_hooks import track_model_result

    # After model inference
    result = motet.do(model_inference, data=...)

    # Track costs (non-blocking)
    track_model_result(
        result=result,
        tenant_id="default",
        task_id=motet.task_id,
        conversation_id=motet.conversation_id,
    )

Notes:
    - All tracking is best-effort and non-blocking
    - Failures are logged but never raised to avoid impacting core flows
    - Integrates with (Redis cost streams and budget enforcement)
"""

from __future__ import annotations

from typing import Any, Dict, Optional
import structlog

from ..types import LLMUsage

logger = structlog.get_logger(__name__)


def track_model_result(
    result: Dict[str, Any],
    *,
    tenant_id: str,
    task_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    command_id: Optional[str] = None,
    principal_id: Optional[str] = None,
    root_conversation_id: Optional[str] = None,
    enable_redis_tracking: bool = True,
    enable_budget_recording: bool = True,
) -> float:
    """
    Track costs from a model inference result.

    This is the main integration point for cost tracking. Call this after
    model_inference or model_stream to record costs.

    Args:
        result: Result dict from model_inference or model_stream command
        tenant_id: Tenant identifier for cost attribution
        task_id: Optional task ID for correlation
        conversation_id: Optional conversation ID for correlation
        command_id: Optional command ID for correlation
        principal_id: Optional principal (user) who invoked the model command for cost attribution
        enable_redis_tracking: Track to Redis streams (ADR-0018)
        enable_budget_recording: Record usage for budget enforcement (ADR-0018)

    Returns:
        Calculated cost in USD (0.0 if tracking fails)
    """
    try:
        # Extract execution provenance and usage from result
        provider = result.get("provider", "unknown")
        model_name = result.get("model_name", "unknown")

        # Build canonical usage from result
        usage = LLMUsage(
            prompt_tokens=result.get("prompt_tokens"),
            output_tokens=result.get("completion_tokens"),
            total_tokens=result.get("total_tokens"),
            cache_read_tokens=result.get("cache_read_tokens"),
            cache_creation_tokens=result.get("cache_creation_tokens"),
            reasoning_tokens=result.get("reasoning_tokens"),
        )

        # Build execution provenance
        execution_provenance = {
            "provider": provider,
            "model_name": model_name,
            "adapter": result.get("adapter"),
            "api_mode": result.get("api_mode"),
            "adapter_selection_source": result.get("adapter_selection_source"),
            "inference_backend": result.get("inference_backend"),
            "tools_enabled": result.get("tools_enabled", False),
            "tools": result.get("tools", []),
        }

        cost_usd = 0.0

        # Track to Redis (ADR-0018)
        if enable_redis_tracking:
            cost_usd = _track_to_redis(
                usage=usage,
                execution_provenance=execution_provenance,
                tenant_id=tenant_id,
                task_id=task_id,
                conversation_id=conversation_id,
                command_id=command_id,
                principal_id=principal_id,
                root_conversation_id=root_conversation_id,
            )

        # Record for budget tracking (ADR-0018)
        if enable_budget_recording:
            _record_budget_usage(
                usage=usage,
                provider=provider,
                model=model_name,
                tenant_id=tenant_id,
                task_id=task_id,
                command_id=command_id,
                principal_id=principal_id,
            )

        return cost_usd

    except Exception as e:
        # Non-critical - log and continue
        logger.warning(
            "track_model_result_failed",
            error=str(e),
            error_type=type(e).__name__,
            tenant_id=tenant_id,
            task_id=task_id,
        )
        return 0.0


def _track_to_redis(
    usage: LLMUsage,
    execution_provenance: Dict[str, Any],
    tenant_id: str,
    task_id: Optional[str],
    conversation_id: Optional[str],
    command_id: Optional[str],
    principal_id: Optional[str] = None,
    root_conversation_id: Optional[str] = None,
) -> float:
    """Track cost to Redis streams (ADR-0018)."""
    try:
        from .cost_tracking_service import get_cost_tracking_service

        service = get_cost_tracking_service()
        return service.track_model_usage(
            usage=usage,
            execution_provenance=execution_provenance,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            command_id=command_id,
            task_id=task_id,
            principal_id=principal_id,
            root_conversation_id=root_conversation_id,
        )
    except Exception as e:
        logger.warning(
            "redis_cost_tracking_failed",
            error=str(e),
            tenant_id=tenant_id,
        )
        return 0.0


def _record_budget_usage(
    usage: LLMUsage,
    provider: str,
    model: str,
    tenant_id: str,
    task_id: Optional[str],
    command_id: Optional[str],
    principal_id: Optional[str] = None,
) -> None:
    """Record usage for budget enforcement (ADR-0018)."""
    try:
        from .budget_enforcer import get_budget_enforcer

        enforcer = get_budget_enforcer()
        enforcer.record_usage(
            tenant_id=tenant_id,
            usage=usage,
            provider=provider,
            model=model,
            task_id=task_id,
            command_id=command_id,
            principal_id=principal_id,
        )
    except Exception as e:
        logger.warning(
            "budget_usage_recording_failed",
            error=str(e),
            tenant_id=tenant_id,
        )


def check_budget_before_inference(
    tenant_id: str,
    provider: str = "openai",
    model: str = "gpt-4o-mini",
    estimated_tokens: int = 1000,
) -> bool:
    """
    Check budget before model inference (ADR-0018).

    Returns True if inference should proceed, False if blocked.
    Raises BudgetExceededError if budget is exhausted.

    Args:
        tenant_id: Tenant identifier
        provider: Provider for cost estimation
        model: Model for cost estimation
        estimated_tokens: Estimated token count for request

    Returns:
        True if request should proceed, False if blocked

    Raises:
        BudgetExceededError: If budget limit is exceeded
    """
    try:
        from .budget_enforcer import get_budget_enforcer, EnforcementAction, BudgetExceededError

        enforcer = get_budget_enforcer()

        # Estimate usage
        estimated_usage = LLMUsage(
            prompt_tokens=estimated_tokens,
            output_tokens=int(estimated_tokens * 0.5),  # Rough estimate
        )

        action = enforcer.check_budget(
            tenant_id=tenant_id,
            estimated_usage=estimated_usage,
            provider=provider,
            model=model,
        )

        if action == EnforcementAction.BLOCK:
            summary = enforcer.get_usage_summary(tenant_id)
            raise BudgetExceededError(
                message=f"Daily budget limit exceeded for tenant {tenant_id}",
                tenant_id=tenant_id,
                current_usage=summary.get("daily", {}).get("cost_usd", 0),
                limit=summary.get("limits", {}).get("daily_limit_usd", 0),
                limit_type="daily",
            )

        if action == EnforcementAction.WARN:
            logger.warning(
                "budget_warning",
                tenant_id=tenant_id,
                message="Approaching budget limit",
            )

        if action == EnforcementAction.THROTTLE:
            logger.warning(
                "budget_throttle",
                tenant_id=tenant_id,
                message="Near budget limit, consider reducing usage",
            )

        return True

    except Exception as e:
        if "BudgetExceededError" in type(e).__name__:
            raise
        logger.warning(
            "budget_check_failed",
            error=str(e),
            tenant_id=tenant_id,
        )
        # Fail open on check errors
        return True


__all__ = [
    "track_model_result",
    "check_budget_before_inference",
]
