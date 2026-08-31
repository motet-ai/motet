"""
Motet - Cost Tracking API

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-29

Description:
    REST API endpoints for cost tracking, budget management, and usage analytics.

    Implements (Distributed Cost Tracking Architecture) with:
    - Cost summary endpoints (daily, monthly)
    - Budget configuration management
    - Usage event streaming (includes cache read + creation tokens)
    - Cost analytics
    - True cross-tenant aggregation via the all-tenants sentinel,
    replacing the older convention where an unset tenant meant motet-global

Dependencies:
    - fastapi: API framework
    - motet.core.cost: Cost tracking services
    - motet.core.tenancy: Tenant catalog used to expand the all-tenants sentinel
    - motet.interfaces.api.shared.auth: Authentication and cross-tenant scope

Usage:
    # Get daily cost summary
    GET /api/v1/cost/summary
    
    # Get budget configuration
    GET /api/v1/cost/budget
    
    # Set budget limits
    PUT /api/v1/cost/budget

Notes:
    - All endpoints require authentication
    - Costs are scoped to tenant from JWT
    - An explicit tenant_id other than the caller's own is 403 unless
      can_access_all_tenants (issue #143)
    - Budget limits can only be set by admins
    - The all-tenants sentinel only widens results for callers with global
      scope; everyone else collapses to their own tenant
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
import structlog

from ..shared.auth import can_access_all_tenants, get_current_principal, require_tenant_access
from ....core.tenancy import ALL_TENANTS
from ....core.types import Principal

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/cost", tags=["cost"])

_TENANT_ID_DESCRIPTION = (
    "Tenant ID to query (defaults to the authenticated user's tenant). "
    f"Pass '{ALL_TENANTS}' to aggregate across every tenant in the catalog; "
    "callers without global scope fall back to their own tenant. Pass "
    "'motet-global' for the platform tenant only."
)


def _resolve_cost_tenants(
    tenant_id: Optional[str], principal: Principal
) -> tuple[List[str], bool]:
    """Resolve the tenant ids a cost query should cover.

    The ALL_TENANTS sentinel expands to the whole catalog, but only for callers
    with global scope; everyone else silently collapses to their own tenant so
    the sentinel can never widen access.

    Args:
        tenant_id: Raw tenant_id query parameter, if any
        principal: Authenticated principal issuing the request

    Returns:
        Tuple of (tenant ids to query, whether the result is an aggregate)
    """
    fallback = principal.tenant_id or "default"
    if tenant_id != ALL_TENANTS:
        # Issue #143: an explicit tenant other than the caller's own is a
        # cross-tenant read. Reject with 403 rather than silently substituting
        # so misconfigured clients fail loudly.
        authorized = require_tenant_access(principal, tenant_id, fallback=fallback)
        return [authorized], False
    if not can_access_all_tenants(principal):
        return [fallback], False

    try:
        from ....core.tenancy import TenantRegistry

        catalog_ids = [record.id for record in TenantRegistry().list_tenants()]
    except Exception as e:
        # An unreachable catalog must not fail the cost page; degrade to the
        # caller's own tenant and make the degradation visible in logs.
        logger.warning(
            "cost_tenant_catalog_unavailable",
            error=str(e),
            error_type=type(e).__name__,
            principal_id=principal.id,
        )
        return [fallback], False

    if not catalog_ids:
        return [fallback], False
    return catalog_ids, True


def _aggregate_label(tenant_ids: List[str], aggregated: bool) -> str:
    """Return the tenant_id value to echo back in a response."""
    return ALL_TENANTS if aggregated else tenant_ids[0]


# =============================================================================
# Request/Response Models
# =============================================================================

class DailyCostSummary(BaseModel):
    """Daily cost summary response."""
    
    tenant_id: str = Field(..., description="Tenant identifier")
    date: str = Field(..., description="Date (YYYY-MM-DD)")
    total_cost_usd: float = Field(..., description="Total cost in USD")
    model_costs_usd: float = Field(0.0, description="Model inference costs in USD")
    infrastructure_costs_usd: float = Field(0.0, description="Infrastructure costs in USD")
    total_requests: int = Field(0, description="Total number of requests")
    total_prompt_tokens: int = Field(0, description="Total prompt/input tokens")
    total_output_tokens: int = Field(0, description="Total output/completion tokens")
    total_cache_read_tokens: int = Field(0, description="Tokens read from provider prompt cache")
    total_cache_creation_tokens: int = Field(
        0,
        description="Tokens written to provider prompt cache (cache creation / writes)",
    )
    total_reasoning_tokens: int = Field(0, description="Reasoning/thinking tokens")
    cache_savings_usd: float = Field(0.0, description="Cost savings from caching")
    aggregated_tenant_ids: Optional[List[str]] = Field(
        None,
        description=(
            "Tenants summed into this response when tenant_id is the all-tenants "
            "sentinel; null for a single-tenant query"
        ),
        json_schema_extra={"example": ["acme", "demo", "motet-global"]},
    )


class DailyCostSummaryByPrincipal(BaseModel):
    """Daily cost summary broken down by principal (user who called the model)."""
    
    tenant_id: str = Field(..., description="Tenant identifier")
    date: str = Field(..., description="Date (YYYY-MM-DD)")
    by_principal: Dict[str, float] = Field(
        ...,
        description="Principal ID (or 'anonymous') to cost in USD for the day",
        json_schema_extra={"example": {"user-123": 0.05, "anonymous": 0.02}},
    )
    aggregated_tenant_ids: Optional[List[str]] = Field(
        None,
        description=(
            "Tenants summed into this response when tenant_id is the all-tenants "
            "sentinel; null for a single-tenant query"
        ),
        json_schema_extra={"example": ["acme", "demo", "motet-global"]},
    )


class BudgetConfig(BaseModel):
    """Budget configuration."""
    
    tenant_id: str = Field(..., description="Tenant identifier")
    daily_limit_usd: Optional[float] = Field(
        None,
        description="Daily spending limit in USD (null = unlimited)",
        json_schema_extra={"example": 100.0},
    )
    monthly_limit_usd: Optional[float] = Field(
        None,
        description="Monthly spending limit in USD (null = unlimited)",
        json_schema_extra={"example": 1000.0},
    )
    alert_threshold_pct: float = Field(
        80.0,
        description="Percentage of limit to trigger warning (0-100)",
        json_schema_extra={"example": 80.0},
    )


class BudgetConfigUpdate(BaseModel):
    """Budget configuration update request."""
    
    daily_limit_usd: Optional[float] = Field(
        None,
        description="Daily spending limit in USD (null = unlimited)",
        json_schema_extra={"example": 100.0},
    )
    monthly_limit_usd: Optional[float] = Field(
        None,
        description="Monthly spending limit in USD (null = unlimited)",
        json_schema_extra={"example": 1000.0},
    )
    alert_threshold_pct: Optional[float] = Field(
        None,
        description="Percentage of limit to trigger warning (0-100)",
        json_schema_extra={"example": 80.0},
    )


class UsageSummary(BaseModel):
    """Usage summary with budget status."""
    
    tenant_id: str = Field(..., description="Tenant identifier")
    date: str = Field(..., description="Date (YYYY-MM-DD)")
    daily: Dict[str, Any] = Field(..., description="Daily usage stats")
    monthly: Dict[str, Any] = Field(..., description="Monthly usage stats")
    limits: Dict[str, Any] = Field(..., description="Budget limits")
    budget_status: str = Field(
        "ok",
        description=(
            "Budget status: ok, warning, critical, exceeded, or not_applicable "
            "for aggregate queries (budgets are configured per tenant)"
        ),
    )
    aggregated_tenant_ids: Optional[List[str]] = Field(
        None,
        description=(
            "Tenants summed into this response when tenant_id is the all-tenants "
            "sentinel; null for a single-tenant query"
        ),
        json_schema_extra={"example": ["acme", "demo", "motet-global"]},
    )


class CostEvent(BaseModel):
    """Cost event from Redis stream."""
    
    event_id: str = Field(..., description="Redis stream event ID")
    timestamp: str = Field(..., description="Event timestamp (ISO8601)")
    provider: str = Field(..., description="LLM provider")
    model: str = Field(..., description="Model name")
    cost_usd: float = Field(..., description="Cost in USD")
    cache_savings_usd: float = Field(
        0,
        description="USD saved vs full price due to prompt-cache hits (full_cost - cost)",
        json_schema_extra={"example": 0.0012},
    )
    prompt_tokens: int = Field(0, description="Prompt tokens")
    output_tokens: int = Field(0, description="Output tokens")
    reasoning_tokens: int = Field(
        0,
        description="Reasoning / thinking tokens reported by the provider",
        json_schema_extra={"example": 1200},
    )
    cache_read_tokens: int = Field(0, description="Tokens read from provider prompt cache")
    cache_creation_tokens: int = Field(
        0,
        description="Tokens written to provider prompt cache (cache creation / writes)",
    )
    tenant_id: Optional[str] = Field(
        None,
        description="Tenant that incurred this model-usage cost",
        json_schema_extra={"example": "motet-global"},
    )
    task_id: Optional[str] = Field(None, description="Task ID")
    command_id: Optional[str] = Field(
        None,
        description="Model command ID that produced this usage event",
        json_schema_extra={"example": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"},
    )
    conversation_id: Optional[str] = Field(None, description="Conversation ID")
    principal_id: Optional[str] = Field(
        None,
        description="Principal (user) who invoked the model command for this event",
    )


class ConversationCostResponse(BaseModel):
    """Priced rollup for one conversation (and isolate_conversation children)."""

    conversation_id: str = Field(
        ...,
        description="Conversation ID",
        json_schema_extra={"example": "conv-123"},
    )
    event_count: int = Field(
        0,
        description="Number of priced usage events recorded for this conversation",
        json_schema_extra={"example": 4},
    )
    cost_usd: Optional[float] = Field(
        None,
        description=(
            "Estimated USD when priced model calls were recorded. "
            "Omitted when unknown; never means free."
        ),
        json_schema_extra={"example": 0.0412},
    )
    include_children: bool = Field(
        True,
        description="Whether isolate_conversation child ids are included in the rollup",
    )


class CostEventsResponse(BaseModel):
    """Response for cost events list."""
    
    events: List[CostEvent] = Field(..., description="List of cost events")
    count: int = Field(..., description="Number of events returned")
    has_more: bool = Field(False, description="Whether more events are available")
    aggregated_tenant_ids: Optional[List[str]] = Field(
        None,
        description=(
            "Tenants merged into this response when tenant_id is the all-tenants "
            "sentinel; null for a single-tenant query"
        ),
        json_schema_extra={"example": ["acme", "demo", "motet-global"]},
    )


# =============================================================================
# Endpoints
# =============================================================================

@router.get(
    "/summary",
    response_model=DailyCostSummary,
    summary="Get daily cost summary",
    description="Returns cost summary for the current day or specified date.",
    responses={
        200: {"description": "Cost summary returned successfully"},
        401: {"description": "Not authenticated"},
        403: {"description": "Not authorized to read another tenant's costs"},
        500: {"description": "Internal server error"},
    },
)
async def get_cost_summary(
    date: Optional[str] = Query(
        None,
        description="Date in YYYY-MM-DD format (defaults to today)",
        json_schema_extra={"example": "2026-01-28"},
    ),
    tenant_id: Optional[str] = Query(
        None,
        description=_TENANT_ID_DESCRIPTION,
        json_schema_extra={"example": "motet-global"},
    ),
    principal: Principal = Depends(get_current_principal),
) -> DailyCostSummary:
    """
    Get daily cost summary for the authenticated tenant.
    
    Returns aggregated cost metrics including:
    - Total cost in USD
    - Token usage breakdown
    - Cache savings
    - Request counts
    
    Use tenant_id to query one tenant ('motet-global' is the platform tenant), or
    the all-tenants sentinel to sum every tenant the caller may see.
    """
    # Resolve before try so exception logging always has a tenant (e.g. import failure).
    tenant_ids, aggregated = _resolve_cost_tenants(tenant_id, principal)
    effective_tenant_id = _aggregate_label(tenant_ids, aggregated)
    try:
        from ....core.cost import get_cost_tracking_service
        
        service = get_cost_tracking_service()
        summaries = [
            service.get_daily_summary(tid, date_key=date) for tid in tenant_ids
        ]

        def _total(field: str, default: Any = 0) -> Any:
            return sum(s.get(field, default) or default for s in summaries)

        default_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return DailyCostSummary(
            tenant_id=effective_tenant_id,
            date=summaries[0].get("date", default_date) if summaries else default_date,
            total_cost_usd=_total("total_cost_usd", 0.0),
            model_costs_usd=_total("model_costs_usd", 0.0),
            infrastructure_costs_usd=0.0,  # TODO: Infrastructure cost tracking
            total_requests=_total("total_requests"),
            total_prompt_tokens=_total("total_prompt_tokens"),
            total_output_tokens=_total("total_output_tokens"),
            total_cache_read_tokens=_total("total_cache_read_tokens"),
            total_cache_creation_tokens=_total("total_cache_creation_tokens"),
            total_reasoning_tokens=_total("total_reasoning_tokens"),
            cache_savings_usd=_total("cache_savings_usd", 0.0),
            aggregated_tenant_ids=tenant_ids if aggregated else None,
        )
        
    except Exception as e:
        logger.error(
            "cost_summary_endpoint_failed",
            error=str(e),
            tenant_id=effective_tenant_id,
        )
        raise HTTPException(status_code=500, detail=f"Failed to get cost summary: {str(e)}")


@router.get(
    "/summary/by_principal",
    response_model=DailyCostSummaryByPrincipal,
    summary="Get daily cost summary by principal",
    description="Returns cost for the day broken down by principal (user who called the model).",
    responses={
        200: {"description": "Cost by principal returned successfully"},
        401: {"description": "Not authenticated"},
        403: {"description": "Not authorized to read another tenant's costs"},
        500: {"description": "Internal server error"},
    },
)
async def get_cost_summary_by_principal(
    date: Optional[str] = Query(
        None,
        description="Date in YYYY-MM-DD format (defaults to today)",
        json_schema_extra={"example": "2026-02-03"},
    ),
    tenant_id: Optional[str] = Query(
        None,
        description=_TENANT_ID_DESCRIPTION,
        json_schema_extra={"example": "motet-global"},
    ),
    principal: Principal = Depends(get_current_principal),
) -> DailyCostSummaryByPrincipal:
    """
    Get daily cost summary per principal (who invoked the model command).
    
    Use tenant_id to query one tenant ('motet-global' is the platform tenant), or
    the all-tenants sentinel to sum every tenant the caller may see. A principal
    active in several tenants is summed into a single row.
    """
    tenant_ids, aggregated = _resolve_cost_tenants(tenant_id, principal)
    effective_tenant_id = _aggregate_label(tenant_ids, aggregated)
    try:
        from ....core.cost import get_cost_tracking_service

        service = get_cost_tracking_service()
        default_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        by_principal: Dict[str, float] = {}
        resolved_date = default_date
        for tid in tenant_ids:
            summary = service.get_daily_summary_by_principal(tid, date_key=date)
            resolved_date = summary.get("date", default_date)
            for principal_id, cost in (summary.get("by_principal") or {}).items():
                by_principal[principal_id] = by_principal.get(principal_id, 0.0) + cost

        return DailyCostSummaryByPrincipal(
            tenant_id=effective_tenant_id,
            date=resolved_date,
            by_principal=by_principal,
            aggregated_tenant_ids=tenant_ids if aggregated else None,
        )

    except Exception as e:
        logger.error(
            "cost_summary_by_principal_endpoint_failed",
            error=str(e),
            tenant_id=effective_tenant_id,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get cost summary by principal: {str(e)}",
        )


@router.get(
    "/usage",
    response_model=UsageSummary,
    summary="Get usage summary with budget status",
    description="Returns usage summary including budget status and limits.",
    responses={
        200: {"description": "Usage summary returned successfully"},
        401: {"description": "Not authenticated"},
        403: {"description": "Not authorized to read another tenant's costs"},
        500: {"description": "Internal server error"},
    },
)
async def get_usage_summary(
    date: Optional[str] = Query(
        None,
        description="Date in YYYY-MM-DD format (defaults to today)",
    ),
    tenant_id: Optional[str] = Query(
        None,
        description=_TENANT_ID_DESCRIPTION,
        json_schema_extra={"example": "motet-global"},
    ),
    principal: Principal = Depends(get_current_principal),
) -> UsageSummary:
    """
    Get usage summary with budget status for the authenticated tenant.
    
    Returns:
    - Daily and monthly usage stats
    - Budget limits and thresholds
    - Budget status (ok, warning, critical, exceeded)

    Budgets are configured per tenant, so an all-tenants query sums usage but
    returns no limits and a ``not_applicable`` status rather than inventing a
    combined budget.
    """
    tenant_ids, aggregated = _resolve_cost_tenants(tenant_id, principal)
    effective_tenant_id = _aggregate_label(tenant_ids, aggregated)
    try:
        from ....core.cost import get_budget_enforcer
        
        enforcer = get_budget_enforcer()
        default_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if aggregated:
            daily_totals: Dict[str, Any] = {}
            monthly_totals: Dict[str, Any] = {}
            resolved_date = default_date
            for tid in tenant_ids:
                summary = enforcer.get_usage_summary(tid, date_key=date)
                resolved_date = summary.get("date", default_date)
                for source, totals in (
                    (summary.get("daily") or {}, daily_totals),
                    (summary.get("monthly") or {}, monthly_totals),
                ):
                    for field, value in source.items():
                        if isinstance(value, (int, float)):
                            totals[field] = totals.get(field, 0) + value

            return UsageSummary(
                tenant_id=effective_tenant_id,
                date=resolved_date,
                daily=daily_totals,
                monthly=monthly_totals,
                limits={},
                budget_status="not_applicable",
                aggregated_tenant_ids=tenant_ids,
            )

        summary = enforcer.get_usage_summary(effective_tenant_id, date_key=date)
        
        # Determine budget status
        daily = summary.get("daily", {})
        limits = summary.get("limits", {})
        daily_cost = daily.get("cost_usd", 0)
        daily_limit = limits.get("daily_limit_usd")
        alert_threshold = limits.get("alert_threshold_pct", 80.0)
        
        budget_status = "ok"
        if daily_limit and daily_limit > 0:
            usage_pct = (daily_cost / daily_limit) * 100
            if usage_pct >= 100:
                budget_status = "exceeded"
            elif usage_pct >= 95:
                budget_status = "critical"
            elif usage_pct >= alert_threshold:
                budget_status = "warning"
        
        return UsageSummary(
            tenant_id=effective_tenant_id,
            date=summary.get("date", default_date),
            daily=daily,
            monthly=summary.get("monthly", {}),
            limits=limits,
            budget_status=budget_status,
        )
        
    except Exception as e:
        logger.error(
            "usage_summary_endpoint_failed",
            error=str(e),
            tenant_id=effective_tenant_id,
        )
        raise HTTPException(status_code=500, detail=f"Failed to get usage summary: {str(e)}")


@router.get(
    "/budget",
    response_model=BudgetConfig,
    summary="Get budget configuration",
    description="Returns current budget configuration for the tenant.",
    responses={
        200: {"description": "Budget configuration returned successfully"},
        401: {"description": "Not authenticated"},
        500: {"description": "Internal server error"},
    },
)
async def get_budget_config(
    principal: Principal = Depends(get_current_principal),
) -> BudgetConfig:
    """
    Get budget configuration for the authenticated tenant.
    """
    try:
        from ....core.cost import get_budget_enforcer
        
        tenant_id = principal.tenant_id or "default"
        
        enforcer = get_budget_enforcer()
        summary = enforcer.get_usage_summary(tenant_id)
        limits = summary.get("limits", {})
        
        return BudgetConfig(
            tenant_id=tenant_id,
            daily_limit_usd=limits.get("daily_limit_usd"),
            monthly_limit_usd=limits.get("monthly_limit_usd"),
            alert_threshold_pct=limits.get("alert_threshold_pct", 80.0),
        )
        
    except Exception as e:
        logger.error(
            "budget_config_endpoint_failed",
            error=str(e),
            tenant_id=principal.tenant_id,
        )
        raise HTTPException(status_code=500, detail=f"Failed to get budget config: {str(e)}")


@router.put(
    "/budget",
    response_model=BudgetConfig,
    summary="Update budget configuration",
    description="Update budget configuration for the tenant. Requires admin role.",
    responses={
        200: {"description": "Budget configuration updated successfully"},
        401: {"description": "Not authenticated"},
        403: {"description": "Not authorized (admin role required)"},
        500: {"description": "Internal server error"},
    },
)
async def update_budget_config(
    config: BudgetConfigUpdate,
    principal: Principal = Depends(get_current_principal),
) -> BudgetConfig:
    """
    Update budget configuration for the authenticated tenant.
    
    Requires admin role.
    """
    # Check for admin role
    if "admin" not in principal.roles and "budget_admin" not in principal.roles:
        raise HTTPException(
            status_code=403,
            detail="Admin role required to update budget configuration",
        )
    
    try:
        from ....core.cost import get_budget_enforcer
        
        tenant_id = principal.tenant_id or "default"
        
        enforcer = get_budget_enforcer()
        
        # Get current config
        current_summary = enforcer.get_usage_summary(tenant_id)
        current_limits = current_summary.get("limits", {})
        
        # Apply updates
        daily_limit = (
            config.daily_limit_usd
            if config.daily_limit_usd is not None
            else current_limits.get("daily_limit_usd")
        )
        monthly_limit = (
            config.monthly_limit_usd
            if config.monthly_limit_usd is not None
            else current_limits.get("monthly_limit_usd")
        )
        alert_threshold = (
            config.alert_threshold_pct
            if config.alert_threshold_pct is not None
            else current_limits.get("alert_threshold_pct", 80.0)
        )
        
        # Save config
        enforcer.set_budget_config(
            tenant_id=tenant_id,
            daily_limit_usd=daily_limit,
            monthly_limit_usd=monthly_limit,
            alert_threshold_pct=alert_threshold,
        )
        
        logger.info(
            "budget_config_updated",
            tenant_id=tenant_id,
            daily_limit_usd=daily_limit,
            monthly_limit_usd=monthly_limit,
            alert_threshold_pct=alert_threshold,
            updated_by=principal.id,
        )
        
        return BudgetConfig(
            tenant_id=tenant_id,
            daily_limit_usd=daily_limit,
            monthly_limit_usd=monthly_limit,
            alert_threshold_pct=alert_threshold,
        )
        
    except Exception as e:
        logger.error(
            "budget_config_update_failed",
            error=str(e),
            tenant_id=principal.tenant_id,
        )
        raise HTTPException(status_code=500, detail=f"Failed to update budget config: {str(e)}")


@router.get(
    "/events",
    response_model=CostEventsResponse,
    summary="Get cost events",
    description="Returns recent cost events from the Redis stream.",
    responses={
        200: {"description": "Cost events returned successfully"},
        401: {"description": "Not authenticated"},
        403: {"description": "Not authorized to read another tenant's costs"},
        500: {"description": "Internal server error"},
    },
)
async def get_cost_events(
    count: int = Query(100, description="Maximum number of events to return", ge=1, le=1000),
    start_id: str = Query("+", description="Redis stream ID to start from ('+' for latest)"),
    tenant_id: Optional[str] = Query(
        None,
        description=_TENANT_ID_DESCRIPTION,
        json_schema_extra={"example": "motet-global"},
    ),
    principal: Principal = Depends(get_current_principal),
) -> CostEventsResponse:
    """
    Get recent cost events for the authenticated tenant.
    
    Returns events from the Redis cost stream with pagination support. An
    all-tenants query merges each tenant's stream and returns the newest events
    across all of them.
    """
    tenant_ids, aggregated = _resolve_cost_tenants(tenant_id, principal)
    effective_tenant_id = _aggregate_label(tenant_ids, aggregated)
    try:
        from ....core.cost import get_cost_tracking_service
        
        service = get_cost_tracking_service()
        raw_events: List[tuple] = []
        for tid in tenant_ids:
            raw_events.extend(
                service.get_cost_events(
                    tenant_id=tid,
                    count=count + 1,  # Get one extra to check for more
                    start_id=start_id,
                )
            )

        if aggregated:
            # Streams are per tenant, so a merged view needs an explicit sort;
            # timestamps are ISO8601 and therefore lexicographically ordered.
            raw_events.sort(key=lambda item: item[1].get("timestamp", ""), reverse=True)
        
        # Check if there are more events
        has_more = len(raw_events) > count
        if has_more:
            raw_events = raw_events[:count]
        
        events = []
        for event_id, data in raw_events:
            events.append(CostEvent(
                event_id=event_id,
                timestamp=data.get("timestamp", ""),
                provider=data.get("provider", "unknown"),
                model=data.get("model", "unknown"),
                cost_usd=float(data.get("cost_usd", 0)),
                cache_savings_usd=float(data.get("cache_savings_usd", 0)),
                prompt_tokens=int(data.get("prompt_tokens", 0)),
                output_tokens=int(data.get("output_tokens", 0)),
                reasoning_tokens=int(data.get("reasoning_tokens", 0)),
                cache_read_tokens=int(data.get("cache_read_tokens", 0)),
                cache_creation_tokens=int(data.get("cache_creation_tokens", 0)),
                tenant_id=data.get("tenant_id") or None,
                task_id=data.get("task_id") or None,
                command_id=data.get("command_id") or None,
                conversation_id=data.get("conversation_id") or None,
                principal_id=data.get("principal_id") or None,
            ))
        
        return CostEventsResponse(
            events=events,
            count=len(events),
            has_more=has_more,
            aggregated_tenant_ids=tenant_ids if aggregated else None,
        )
        
    except Exception as e:
        logger.error(
            "cost_events_endpoint_failed",
            error=str(e),
            tenant_id=effective_tenant_id,
        )
        raise HTTPException(status_code=500, detail=f"Failed to get cost events: {str(e)}")


@router.get(
    "/conversation/{conversation_id}",
    response_model=ConversationCostResponse,
    summary="Get conversation cost summary",
    description=(
        "Estimated USD for one conversation. Includes isolate_conversation "
        "children. cost_usd is omitted when unknown (not free)."
    ),
    responses={
        200: {"description": "Conversation cost returned"},
        401: {"description": "Not authenticated"},
        403: {"description": "Not authorized to read another tenant's costs"},
    },
)
async def get_conversation_cost(
    conversation_id: str,
    include_children: bool = Query(
        True,
        description="Include isolate_conversation child conversation ids in the rollup",
    ),
    tenant_id: Optional[str] = Query(
        None,
        description=_TENANT_ID_DESCRIPTION,
        json_schema_extra={"example": "motet-global"},
    ),
    principal: Principal = Depends(get_current_principal),
) -> ConversationCostResponse:
    """Return the priced conversation rollup for Chat Explorer and ops clients."""
    tenant_ids, aggregated = _resolve_cost_tenants(tenant_id, principal)
    if aggregated or len(tenant_ids) != 1:
        raise HTTPException(
            status_code=400,
            detail="Conversation cost is scoped to one tenant",
        )
    cid = (conversation_id or "").strip()
    if not cid:
        raise HTTPException(status_code=400, detail="conversation_id is required")
    try:
        from motet.core.conversations.transcript_storage import coerce_cost_usd
        from motet.core.cost import get_cost_tracking_service

        summary = get_cost_tracking_service().get_conversation_cost_summary(
            tenant_ids[0],
            cid,
            include_children=include_children,
        )
        event_count = int(summary.get("event_count") or 0)
        priced = coerce_cost_usd(summary.get("cost_usd")) if event_count > 0 else None
        return ConversationCostResponse(
            conversation_id=cid,
            event_count=event_count,
            cost_usd=priced,
            include_children=include_children,
        )
    except Exception as e:
        logger.error(
            "conversation_cost_endpoint_failed",
            error=str(e),
            conversation_id=cid,
            tenant_id=tenant_ids[0],
        )
        raise HTTPException(status_code=500, detail=f"Failed to get conversation cost: {str(e)}")


__all__ = ["router"]
