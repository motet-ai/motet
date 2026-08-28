"""
Motet - Budget Enforcer

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Budget enforcement for tenant-level cost control.

    Implements budget enforcement with:
    - Pre-execution budget checking (block/throttle/warn)
    - Post-execution usage recording
    - Daily and monthly budget limits
    - Redis-based real-time tracking

    Uses canonical LLMUsage for cost estimation.

Dependencies:
    - motet.core.types: Canonical LLMUsage type
    - motet.core.cost.cost_calculator: CostCalculator for cost estimation
    - motet.core.distributed.redis_manager: Redis storage

Usage:
    from motet.core.cost.budget_enforcer import (
        BudgetEnforcer,
        get_budget_enforcer,
        EnforcementAction,
    )
    
    # Check if request should proceed
    enforcer = get_budget_enforcer()
    action = enforcer.check_budget("tenant-123")
    
    if action == EnforcementAction.BLOCK:
        raise BudgetExceededError("Daily limit exceeded")
    
    # After successful request, record usage
    enforcer.record_usage(
        tenant_id="tenant-123",
        usage=response.usage,
        provider="openai",
        model="gpt-4o-mini",
    )

Notes:
    - Budget configs are stored in Redis with configurable limits
    - Daily/monthly limits are enforced independently
    - Alert thresholds trigger warnings before blocking
    - Uses get_sync_redis_client() for all Redis operations (AGENTS.md)
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, cast
import structlog

from ..types import LLMUsage
from ..distributed.redis_manager import get_sync_redis_client
from ..distributed.tenant_keys import hgetall_first, tenant_key, write_key
from .cost_calculator import get_cost_calculator

logger = structlog.get_logger(__name__)


class EnforcementAction(str, Enum):
    """Budget enforcement actions."""
    ALLOW = "allow"      # Proceed normally
    WARN = "warn"        # Proceed but log warning (approaching limit)
    THROTTLE = "throttle"  # Proceed but rate-limit (near limit)
    BLOCK = "block"      # Block request (limit exceeded)


class BudgetExceededError(Exception):
    """Raised when a request would exceed budget limits."""
    
    def __init__(
        self,
        message: str,
        tenant_id: str,
        current_usage: float,
        limit: float,
        limit_type: str,
    ):
        super().__init__(message)
        self.tenant_id = tenant_id
        self.current_usage = current_usage
        self.limit = limit
        self.limit_type = limit_type


class BudgetEnforcer:
    """
    Real-time budget enforcement and usage tracking (ADR-0018).
    
    Uses Redis for persistent budget tracking with:
    - Daily and monthly budget limits
    - Alert threshold configuration
    - Usage recording for budget tracking
    
    All Redis operations use get_sync_redis_client() per AGENTS.md.
    """
    
    def __init__(self, client_id: str = "budget_enforcer"):
        """
        Initialize BudgetEnforcer.
        
        Args:
            client_id: Redis client identifier for connection pooling
        """
        self.client_id = client_id
        self.cost_calculator = get_cost_calculator()
    
    def check_budget(
        self,
        tenant_id: str,
        estimated_usage: Optional[LLMUsage] = None,
        provider: str = "openai",
        model: str = "gpt-4o-mini",
    ) -> EnforcementAction:
        """
        Check if request should proceed based on budget.
        
        Args:
            tenant_id: Tenant identifier
            estimated_usage: Optional estimated canonical usage for pre-check
            provider: Provider for cost estimation
            model: Model for cost estimation
            
        Returns:
            EnforcementAction indicating whether to allow, warn, throttle, or block
        """
        try:
            budget_config = self._get_budget_config(tenant_id)
            if not budget_config:
                # No budget limits configured - allow
                return EnforcementAction.ALLOW
            
            current_usage = self._get_current_usage(tenant_id)
            current_daily = current_usage.get("daily_cost_usd", 0.0)
            current_monthly = current_usage.get("monthly_cost_usd", 0.0)
            
            # Estimate additional cost if usage provided
            estimated_cost = 0.0
            if estimated_usage:
                # Pre-call estimate: suppress cost_calculated so the log stream
                # keeps one line per actual LLM call (see CostCalculator).
                estimated_cost = self.cost_calculator.calculate_cost_canonical(
                    provider=provider,
                    model=model,
                    usage=estimated_usage,
                    tenant_id=tenant_id,
                    log_event=False,
                )
            
            # Check daily limit
            daily_limit = budget_config.get("daily_limit_usd")
            if daily_limit:
                total_projected = current_daily + estimated_cost
                
                if total_projected >= daily_limit:
                    logger.warning(
                        "budget_daily_limit_exceeded",
                        tenant_id=tenant_id,
                        current_daily=current_daily,
                        projected=total_projected,
                        limit=daily_limit,
                    )
                    return EnforcementAction.BLOCK
                
                # Check throttle threshold (95%)
                if (total_projected / daily_limit * 100) >= 95:
                    logger.warning(
                        "budget_daily_throttle_threshold",
                        tenant_id=tenant_id,
                        current_daily=current_daily,
                        limit=daily_limit,
                        usage_pct=total_projected / daily_limit * 100,
                    )
                    return EnforcementAction.THROTTLE
                
                # Check alert threshold (default 80%)
                alert_threshold = budget_config.get("alert_threshold_pct", 80.0)
                if (total_projected / daily_limit * 100) >= alert_threshold:
                    logger.warning(
                        "budget_daily_alert_threshold",
                        tenant_id=tenant_id,
                        current_daily=current_daily,
                        limit=daily_limit,
                        usage_pct=total_projected / daily_limit * 100,
                    )
                    return EnforcementAction.WARN
            
            # Check monthly limit
            monthly_limit = budget_config.get("monthly_limit_usd")
            if monthly_limit:
                if current_monthly >= monthly_limit:
                    logger.warning(
                        "budget_monthly_limit_exceeded",
                        tenant_id=tenant_id,
                        current_monthly=current_monthly,
                        limit=monthly_limit,
                    )
                    return EnforcementAction.BLOCK
            
            return EnforcementAction.ALLOW
            
        except Exception as e:
            # Log but don't block on budget check failures
            logger.error(
                "budget_check_failed",
                tenant_id=tenant_id,
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            # Default to allow on error (fail-open for budget checks)
            return EnforcementAction.ALLOW
    
    def record_usage(
        self,
        tenant_id: str,
        usage: LLMUsage,
        provider: str,
        model: str,
        task_id: Optional[str] = None,
        command_id: Optional[str] = None,
        principal_id: Optional[str] = None,
    ) -> float:
        """
        Record usage against tenant budget using canonical LLMUsage.
        
        Called after successful LLM calls to update budget tracking.
        When principal_id is provided, usage is also attributed to that principal.
        
        Args:
            tenant_id: Tenant identifier
            usage: Canonical LLMUsage from LLMResponse
            provider: Provider name
            model: Model name
            task_id: Optional task ID for correlation
            command_id: Optional command ID for correlation
            principal_id: Optional principal (user) who invoked the model command
            
        Returns:
            Cost in USD that was recorded
        """
        try:
            # Recomputation for budget attribution; CostTrackingService already
            # logged cost_calculated for this call.
            cost_usd = self.cost_calculator.calculate_cost_canonical(
                provider=provider,
                model=model,
                usage=usage,
                tenant_id=tenant_id,
                log_event=False,
            )
            
            now = datetime.now(timezone.utc)
            date_key = now.strftime("%Y-%m-%d")
            month_key = now.strftime("%Y-%m")
            
            redis = get_sync_redis_client(self.client_id)
            # Sanitize principal_id for Redis hash field (no colons)
            principal_field = (principal_id or "anonymous").replace(":", "_") or "anonymous"
            
            pipe = redis.pipeline()
            
            # Update daily usage in Redis (tenant-level)
            daily_key = write_key(redis, tenant_id, f"budget:usage:daily:{tenant_id}:{date_key}")
            pipe.hincrbyfloat(daily_key, "cost_usd", cost_usd)
            pipe.hincrby(daily_key, "requests", 1)
            pipe.hincrby(daily_key, "tokens", usage.total_tokens or 0)
            pipe.expire(daily_key, 86400 * 7)  # Keep 7 days
            
            # Per-principal daily usage (cost by principal who called the model)
            daily_by_principal_key = write_key(
                redis, tenant_id, f"budget:usage:daily:{tenant_id}:{date_key}:by_principal"
            )
            pipe.hincrbyfloat(daily_by_principal_key, principal_field, cost_usd)
            pipe.expire(daily_by_principal_key, 86400 * 7)
            
            # Update monthly usage (tenant-level)
            monthly_key = write_key(redis, tenant_id, f"budget:usage:monthly:{tenant_id}:{month_key}")
            pipe.hincrbyfloat(monthly_key, "cost_usd", cost_usd)
            pipe.hincrby(monthly_key, "requests", 1)
            pipe.hincrby(monthly_key, "tokens", usage.total_tokens or 0)
            pipe.expire(monthly_key, 86400 * 90)  # Keep 90 days
            
            # Per-principal monthly usage
            monthly_by_principal_key = write_key(
                redis, tenant_id, f"budget:usage:monthly:{tenant_id}:{month_key}:by_principal"
            )
            pipe.hincrbyfloat(monthly_by_principal_key, principal_field, cost_usd)
            pipe.expire(monthly_by_principal_key, 86400 * 90)
            
            pipe.execute()
            
            logger.debug(
                "budget_usage_recorded",
                tenant_id=tenant_id,
                principal_id=principal_id,
                provider=provider,
                model=model,
                cost_usd=cost_usd,
                task_id=task_id,
                command_id=command_id,
            )
            
            return cost_usd
            
        except Exception as e:
            # Log but don't fail on usage recording
            logger.error(
                "budget_usage_record_failed",
                tenant_id=tenant_id,
                provider=provider,
                model=model,
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            return 0.0
    
    def get_usage_summary(
        self,
        tenant_id: str,
        date_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get usage summary for a tenant.
        
        Args:
            tenant_id: Tenant identifier
            date_key: Optional date key (YYYY-MM-DD), defaults to today
            
        Returns:
            Usage summary dict with daily and monthly stats
        """
        try:
            if not date_key:
                date_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            
            month_key = date_key[:7]  # YYYY-MM
            
            redis = get_sync_redis_client(self.client_id)
            
            # Get daily usage
            daily_key = f"budget:usage:daily:{tenant_id}:{date_key}"
            daily_data = cast(Dict[str, str], hgetall_first(redis, tenant_key(tenant_id, daily_key)))
            
            # Get monthly usage
            monthly_key = f"budget:usage:monthly:{tenant_id}:{month_key}"
            monthly_data = cast(Dict[str, str], hgetall_first(redis, tenant_key(tenant_id, monthly_key)))
            
            # Get budget config
            budget_config = self._get_budget_config(tenant_id)
            
            return {
                "tenant_id": tenant_id,
                "date": date_key,
                "daily": {
                    "cost_usd": float(daily_data.get("cost_usd", 0) or 0),
                    "requests": int(daily_data.get("requests", 0) or 0),
                    "tokens": int(daily_data.get("tokens", 0) or 0),
                },
                "monthly": {
                    "cost_usd": float(monthly_data.get("cost_usd", 0) or 0),
                    "requests": int(monthly_data.get("requests", 0) or 0),
                    "tokens": int(monthly_data.get("tokens", 0) or 0),
                },
                "limits": {
                    "daily_limit_usd": budget_config.get("daily_limit_usd") if budget_config else None,
                    "monthly_limit_usd": budget_config.get("monthly_limit_usd") if budget_config else None,
                    "alert_threshold_pct": budget_config.get("alert_threshold_pct", 80.0) if budget_config else 80.0,
                },
            }
            
        except Exception as e:
            logger.error(
                "budget_usage_summary_failed",
                tenant_id=tenant_id,
                error=str(e),
                exc_info=True,
            )
            return {
                "tenant_id": tenant_id,
                "error": str(e),
            }
    
    def set_budget_config(
        self,
        tenant_id: str,
        daily_limit_usd: Optional[float] = None,
        monthly_limit_usd: Optional[float] = None,
        alert_threshold_pct: float = 80.0,
    ) -> None:
        """
        Set budget configuration for a tenant.
        
        Args:
            tenant_id: Tenant identifier
            daily_limit_usd: Daily spending limit in USD (None = unlimited)
            monthly_limit_usd: Monthly spending limit in USD (None = unlimited)
            alert_threshold_pct: Percentage of limit to trigger warning (default 80%)
        """
        try:
            redis = get_sync_redis_client(self.client_id)
            key = write_key(redis, tenant_id, f"budget:config:{tenant_id}")
            
            config = {
                "daily_limit_usd": str(daily_limit_usd) if daily_limit_usd else "",
                "monthly_limit_usd": str(monthly_limit_usd) if monthly_limit_usd else "",
                "alert_threshold_pct": str(alert_threshold_pct),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            
            redis.hset(key, mapping=config)
            
            logger.info(
                "budget_config_set",
                tenant_id=tenant_id,
                daily_limit_usd=daily_limit_usd,
                monthly_limit_usd=monthly_limit_usd,
                alert_threshold_pct=alert_threshold_pct,
            )
            
        except Exception as e:
            logger.error(
                "budget_config_set_failed",
                tenant_id=tenant_id,
                error=str(e),
                exc_info=True,
            )
            raise
    
    def _get_budget_config(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        """Get budget configuration for a tenant."""
        try:
            redis = get_sync_redis_client(self.client_id)
            key = f"budget:config:{tenant_id}"
            data = cast(Dict[str, str], hgetall_first(redis, tenant_key(tenant_id, key)))
            
            if not data:
                return None
            
            # Parse config values
            config: Dict[str, Any] = {}
            if data.get("daily_limit_usd"):
                config["daily_limit_usd"] = float(data["daily_limit_usd"])
            if data.get("monthly_limit_usd"):
                config["monthly_limit_usd"] = float(data["monthly_limit_usd"])
            config["alert_threshold_pct"] = float(data.get("alert_threshold_pct", 80.0))
            
            return config
            
        except Exception as e:
            logger.error(
                "budget_config_get_failed",
                tenant_id=tenant_id,
                error=str(e),
            )
            return None
    
    def _get_current_usage(self, tenant_id: str) -> Dict[str, float]:
        """Get current usage for a tenant."""
        try:
            now = datetime.now(timezone.utc)
            date_key = now.strftime("%Y-%m-%d")
            month_key = now.strftime("%Y-%m")
            
            redis = get_sync_redis_client(self.client_id)
            
            # Get daily and monthly usage
            daily_key = f"budget:usage:daily:{tenant_id}:{date_key}"
            monthly_key = f"budget:usage:monthly:{tenant_id}:{month_key}"
            daily_data = hgetall_first(redis, tenant_key(tenant_id, daily_key))
            monthly_data = hgetall_first(redis, tenant_key(tenant_id, monthly_key))
            daily_cost = daily_data.get("cost_usd") if daily_data else None
            monthly_cost = monthly_data.get("cost_usd") if monthly_data else None
            
            return {
                "daily_cost_usd": float(daily_cost or 0),
                "monthly_cost_usd": float(monthly_cost or 0),
            }
            
        except Exception as e:
            logger.error(
                "budget_current_usage_failed",
                tenant_id=tenant_id,
                error=str(e),
            )
            return {"daily_cost_usd": 0.0, "monthly_cost_usd": 0.0}


# =============================================================================
# Singleton Instance
# =============================================================================

_budget_enforcer_instance: Optional[BudgetEnforcer] = None


def get_budget_enforcer() -> BudgetEnforcer:
    """
    Get the singleton BudgetEnforcer instance.
    
    Returns:
        BudgetEnforcer singleton instance
    """
    global _budget_enforcer_instance
    if _budget_enforcer_instance is None:
        _budget_enforcer_instance = BudgetEnforcer()
    return _budget_enforcer_instance


__all__ = [
    "EnforcementAction",
    "BudgetExceededError",
    "BudgetEnforcer",
    "get_budget_enforcer",
]
