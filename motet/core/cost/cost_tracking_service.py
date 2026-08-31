"""
Motet - Cost Tracking Service

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-29

Description:
    Centralized cost tracking using canonical protocol.

    Records cost events to Redis Streams for real-time analytics and
    maintains aggregations for cost reporting. Per-conversation running
    totals are incremented at write time (O(1) exact reads regardless of
    tenant event volume) and power cycle reporting (e.g. app-builder
    GitHub comments); isolated child conversation IDs are indexed under
    the stored root for rollups.

    Implements §5.C (CostTrackingService) with:
    - Cost event streaming to Redis
    - Real-time aggregation updates (including cache read + creation tokens)
    - Per-conversation running totals with parent/child rollup
    - execution provenance metadata
    - Multi-tenant cost attribution

Dependencies:
    - motet.core.types: Canonical LLMUsage type
    - motet.core.cost.cost_calculator: CostCalculator for cost calculation
    - motet.core.distributed.redis_manager: Redis storage

Usage:
    from motet.core.cost.cost_tracking_service import (
        CostTrackingService,
        get_cost_tracking_service,
    )
    
    # Track model usage
    service = get_cost_tracking_service()
    cost_usd = service.track_model_usage(
        usage=response.usage,
        execution_provenance={"provider": "openai", "model_name": "gpt-4o-mini", ...},
        tenant_id="default",
        task_id="task-123",
    )

Notes:
    - Cost events are stored in Redis Streams for durability
    - Aggregations are updated atomically using pipelines
    - All Redis operations use get_sync_redis_client() (AGENTS.md)
    - Non-critical path - failures are logged but never raised
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, cast
import structlog

from ..types import LLMUsage
from ..distributed.redis_manager import get_sync_redis_client
from ..distributed.tenant_keys import (
    hgetall_first,
    smembers_union,
    tenant_key,
    write_key,
)
from .cost_calculator import get_cost_calculator, CostCalculator

logger = structlog.get_logger(__name__)

#: TTL for per-conversation running-total keys. Refreshed on every write, so
#: only conversations idle this long expire (the stream remains the audit trail).
_CONVERSATION_TOTALS_TTL_SECONDS = 30 * 24 * 3600  # 30 days


def _to_str(value: Any) -> str:
    """Decode Redis bytes responses to str."""
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


class CostTrackingService:
    """
    Centralized cost tracking using canonical protocol (ADR-0064).
    
    Receives canonical LLMUsage from provider adapters and records
    cost events with full execution provenance.
    
    Uses get_sync_redis_client() from UnifiedRedisManager for all Redis
    operations per AGENTS.md to ensure consistent connection pooling.
    """
    
    def __init__(
        self,
        cost_calculator: Optional[CostCalculator] = None,
        client_id: str = "cost_tracking",
    ):
        """
        Initialize CostTrackingService.
        
        Args:
            cost_calculator: Optional cost calculator (uses singleton if not provided)
            client_id: Redis client identifier for connection pooling
        """
        self.cost_calculator = cost_calculator or get_cost_calculator()
        self.client_id = client_id
    
    def track_model_usage(
        self,
        usage: LLMUsage,
        execution_provenance: Dict[str, Any],
        tenant_id: str,
        conversation_id: Optional[str] = None,
        command_id: Optional[str] = None,
        task_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        root_conversation_id: Optional[str] = None,
    ) -> float:
        """
        Track model usage cost from canonical LLMUsage.
        
        Args:
            usage: Canonical LLMUsage from LLMResponse
            execution_provenance: ADR-0064 provenance metadata (adapter, api_mode, etc.)
            tenant_id: Tenant identifier for cost attribution
            conversation_id: Optional conversation context
            command_id: Optional command context
            task_id: Optional task context
            principal_id: Optional principal (user) who invoked the model command
            
        Returns:
            Calculated cost in USD
        """
        provider = execution_provenance.get("provider", "unknown")
        model = execution_provenance.get("model_name", "unknown")
        
        # Log entry with correlation IDs for distributed tracing
        logger.debug(
            "cost_tracking_started",
            operation="track_model_usage",
            tenant_id=tenant_id,
            task_id=task_id,
            command_id=command_id,
            principal_id=principal_id,
            provider=provider,
            model=model,
            prompt_tokens=usage.prompt_tokens,
            output_tokens=usage.output_tokens,
        )
        
        try:
            # Calculate cost using canonical usage
            cost_usd = self.cost_calculator.calculate_cost_canonical(
                provider=provider,
                model=model,
                usage=usage,
                tenant_id=tenant_id,
            )
            
            # Calculate what cost would be without cache discount
            full_cost_usd = self.cost_calculator.calculate_cost_without_cache_discount(
                provider=provider,
                model=model,
                usage=usage,
                tenant_id=tenant_id,
            )
            
            # Build event data
            event_data = self._build_event_data(
                usage=usage,
                execution_provenance=execution_provenance,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                command_id=command_id,
                task_id=task_id,
                principal_id=principal_id,
                cost_usd=cost_usd,
                full_cost_usd=full_cost_usd,
                root_conversation_id=root_conversation_id,
            )
            
            # Write to Redis stream
            redis = get_sync_redis_client(self.client_id)
            stream_key = write_key(redis, tenant_id, f"cost:model_usage:{tenant_id}")
            
            try:
                # Convert all values to strings for Redis stream
                event_str = {k: str(v) for k, v in event_data.items()}
                redis.xadd(stream_key, cast(Any, event_str), maxlen=100000)
            except Exception as redis_error:
                logger.error(
                    "redis_stream_write_failed",
                    operation="track_model_usage",
                    stream_key=stream_key,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    error=str(redis_error),
                    error_type=type(redis_error).__name__,
                )
                # Continue - aggregation still possible
            
            # Update aggregations (tenant and per-principal)
            self._update_aggregations(tenant_id, event_data)

            # Update per-conversation running totals (exact O(1) rollup reads)
            self._update_conversation_totals(tenant_id, conversation_id, event_data)
            
            # Log success with correlation IDs
            logger.info(
                "cost_event_recorded",
                tenant_id=tenant_id,
                provider=provider,
                model=model,
                cost_usd=cost_usd,
                cache_savings_usd=full_cost_usd - cost_usd,
                task_id=task_id,
            )
            
            return cost_usd
            
        except Exception as e:
            # Non-critical path - log and return 0
            logger.error(
                "cost_tracking_failed",
                operation="track_model_usage",
                tenant_id=tenant_id,
                task_id=task_id,
                command_id=command_id,
                provider=provider,
                model=model,
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            return 0.0
    
    def _build_event_data(
        self,
        usage: LLMUsage,
        execution_provenance: Dict[str, Any],
        tenant_id: str,
        conversation_id: Optional[str],
        command_id: Optional[str],
        task_id: Optional[str],
        principal_id: Optional[str],
        cost_usd: float,
        full_cost_usd: float,
        root_conversation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build event data dictionary for Redis stream."""
        return {
            # Identity
            "tenant_id": tenant_id,
            "principal_id": principal_id or "",
            "conversation_id": conversation_id or "",
            "root_conversation_id": (root_conversation_id or "").strip(),
            "command_id": command_id or "",
            "task_id": task_id or "",
            
            # ADR-0064 Execution Provenance (REQUIRED)
            "provider": execution_provenance.get("provider", "unknown"),
            "model": execution_provenance.get("model_name", "unknown"),
            "adapter": execution_provenance.get("adapter", "unknown"),
            "api_mode": execution_provenance.get("api_mode", "unknown"),
            "adapter_selection_source": execution_provenance.get("adapter_selection_source", "unknown"),
            "inference_backend": execution_provenance.get("inference_backend", "unknown"),
            
            # ADR-0064 Canonical Usage (LLMUsage)
            "prompt_tokens": usage.prompt_tokens or 0,
            "output_tokens": usage.output_tokens or 0,
            "cache_read_tokens": usage.cache_read_tokens or 0,
            "cache_creation_tokens": usage.cache_creation_tokens or 0,
            "reasoning_tokens": usage.reasoning_tokens or 0,
            "total_tokens": usage.total_tokens or 0,
            "tool_time_ms": usage.tool_time_ms or 0,
            
            # Cost calculations
            "cost_usd": cost_usd,
            "full_cost_usd": full_cost_usd,
            "cache_savings_usd": full_cost_usd - cost_usd,
            
            # Tool usage (ADR-0064 built-in tools)
            "tools_enabled": execution_provenance.get("tools_enabled", False),
            "builtin_tools": ",".join(execution_provenance.get("tools", [])),
            
            # Timestamp
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    
    def _update_aggregations(self, tenant_id: str, event: Dict[str, Any]) -> None:
        """Update real-time cost aggregations in Redis (tenant and per-principal)."""
        try:
            redis = get_sync_redis_client(self.client_id)
            date_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            summary_key = write_key(redis, tenant_id, f"cost:summary:{tenant_id}:{date_key}")
            cost_usd = float(event["cost_usd"])
            principal_id = event.get("principal_id") or "anonymous"
            # Sanitize principal_id for use as Redis hash field (no colons)
            principal_field = (principal_id or "anonymous").replace(":", "_") or "anonymous"
            
            # Atomic updates using Redis pipeline for efficiency
            pipe = redis.pipeline()
            
            # Update tenant totals
            pipe.hincrbyfloat(summary_key, "total_cost_usd", cost_usd)
            pipe.hincrbyfloat(summary_key, "model_costs_usd", cost_usd)
            pipe.hincrby(summary_key, "total_requests", 1)
            
            # Update canonical token metrics
            pipe.hincrby(summary_key, "total_prompt_tokens", int(event["prompt_tokens"]))
            pipe.hincrby(summary_key, "total_output_tokens", int(event["output_tokens"]))
            pipe.hincrby(summary_key, "total_cache_read_tokens", int(event["cache_read_tokens"]))
            pipe.hincrby(
                summary_key,
                "total_cache_creation_tokens",
                int(event.get("cache_creation_tokens") or 0),
            )
            pipe.hincrby(summary_key, "total_reasoning_tokens", int(event["reasoning_tokens"]))
            
            # Update cache savings
            pipe.hincrbyfloat(summary_key, "cache_savings_usd", float(event["cache_savings_usd"]))
            
            # Per-principal cost aggregation (cost by principal who called the model)
            by_principal_key = write_key(
                redis, tenant_id, f"cost:summary:{tenant_id}:{date_key}:by_principal"
            )
            pipe.hincrbyfloat(by_principal_key, principal_field, cost_usd)
            pipe.expire(by_principal_key, 604800)
            
            # Set TTL for cleanup (7 days)
            pipe.expire(summary_key, 604800)
            
            pipe.execute()
            
        except Exception as e:
            logger.error(
                "aggregation_update_failed",
                tenant_id=tenant_id,
                error=str(e),
            )

    @staticmethod
    def _conversation_totals_logical(tenant_id: str, conversation_id: str) -> str:
        return f"cost:conversation:{tenant_id}:{conversation_id}"

    @staticmethod
    def _conversation_totals_key(tenant_id: str, conversation_id: str) -> str:
        return tenant_key(tenant_id, f"cost:conversation:{tenant_id}:{conversation_id}")

    def _update_conversation_totals(
        self,
        tenant_id: str,
        conversation_id: Optional[str],
        event: Dict[str, Any],
    ) -> None:
        """Increment per-conversation running totals at event-write time.

        Keys (all refreshed to ``_CONVERSATION_TOTALS_TTL_SECONDS``):
        - ``cost:conversation:{tenant}:{cid}`` hash — cost_usd, full_cost_usd,
          prompt_tokens, output_tokens, total_tokens, event_count
        - ``…:{cid}:models`` / ``…:{cid}:providers`` sets
        - ``…:{root}:children`` set — child cids indexed under the root when
          the event (or parentage record) has ``root_conversation_id``

        Non-critical path: failures are logged, never raised.
        """
        cid = (conversation_id or "").strip()
        if not cid:
            return
        try:
            redis = get_sync_redis_client(self.client_id)
            base = write_key(redis, tenant_id, self._conversation_totals_logical(tenant_id, cid))
            ttl = _CONVERSATION_TOTALS_TTL_SECONDS

            pipe = redis.pipeline()
            pipe.hincrbyfloat(base, "cost_usd", float(event["cost_usd"]))
            pipe.hincrbyfloat(base, "full_cost_usd", float(event["full_cost_usd"]))
            pipe.hincrby(base, "prompt_tokens", int(event["prompt_tokens"]))
            pipe.hincrby(base, "output_tokens", int(event["output_tokens"]))
            pipe.hincrby(base, "total_tokens", int(event["total_tokens"]))
            pipe.hincrby(base, "event_count", 1)
            pipe.expire(base, ttl)

            model = str(event.get("model") or "").strip()
            if model:
                pipe.sadd(f"{base}:models", model)
                pipe.expire(f"{base}:models", ttl)
            provider = str(event.get("provider") or "").strip()
            if provider:
                pipe.sadd(f"{base}:providers", provider)
                pipe.expire(f"{base}:providers", ttl)

            root = str(event.get("root_conversation_id") or "").strip()
            if not root:
                from ..conversations.lineage import root_conversation_id_of

                root = root_conversation_id_of(cid, tenant_id=tenant_id) or ""
            if root and root != cid:
                children_key = write_key(
                    redis,
                    tenant_id,
                    f"{self._conversation_totals_logical(tenant_id, root)}:children",
                )
                pipe.sadd(children_key, cid)
                pipe.expire(children_key, ttl)

            pipe.execute()
        except Exception as e:
            logger.error(
                "conversation_totals_update_failed",
                tenant_id=tenant_id,
                conversation_id=cid,
                error=str(e),
                error_type=type(e).__name__,
            )

    def get_daily_summary(
        self,
        tenant_id: str,
        date_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get daily cost summary for a tenant.
        
        Args:
            tenant_id: Tenant identifier
            date_key: Optional date key (YYYY-MM-DD), defaults to today
            
        Returns:
            Daily cost summary
        """
        try:
            if not date_key:
                date_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            
            redis = get_sync_redis_client(self.client_id)
            summary_key = f"cost:summary:{tenant_id}:{date_key}"
            data = cast(
                Dict[str, str],
                hgetall_first(redis, tenant_key(tenant_id, summary_key)),
            )
            
            if not data:
                return {
                    "tenant_id": tenant_id,
                    "date": date_key,
                    "total_cost_usd": 0.0,
                    "total_requests": 0,
                    "total_tokens": 0,
                }
            
            return {
                "tenant_id": tenant_id,
                "date": date_key,
                "total_cost_usd": float(data.get("total_cost_usd", 0) or 0),
                "model_costs_usd": float(data.get("model_costs_usd", 0) or 0),
                "total_requests": int(data.get("total_requests", 0) or 0),
                "total_prompt_tokens": int(data.get("total_prompt_tokens", 0) or 0),
                "total_output_tokens": int(data.get("total_output_tokens", 0) or 0),
                "total_cache_read_tokens": int(data.get("total_cache_read_tokens", 0) or 0),
                "total_cache_creation_tokens": int(data.get("total_cache_creation_tokens", 0) or 0),
                "total_reasoning_tokens": int(data.get("total_reasoning_tokens", 0) or 0),
                "cache_savings_usd": float(data.get("cache_savings_usd", 0) or 0),
            }
            
        except Exception as e:
            logger.error(
                "daily_summary_failed",
                tenant_id=tenant_id,
                date_key=date_key,
                error=str(e),
            )
            return {
                "tenant_id": tenant_id,
                "date": date_key,
                "error": str(e),
            }
    
    def get_daily_summary_by_principal(
        self,
        tenant_id: str,
        date_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get daily cost summary per principal (who called the model command).
        
        Args:
            tenant_id: Tenant identifier
            date_key: Optional date key (YYYY-MM-DD), defaults to today
            
        Returns:
            Dict mapping principal_id (or "anonymous") to cost_usd for the day
        """
        try:
            if not date_key:
                date_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            
            redis = get_sync_redis_client(self.client_id)
            by_principal_key = f"cost:summary:{tenant_id}:{date_key}:by_principal"
            data = cast(
                Dict[Any, Any],
                hgetall_first(redis, tenant_key(tenant_id, by_principal_key)),
            )
            
            if not data:
                return {"tenant_id": tenant_id, "date": date_key, "by_principal": {}}
            
            by_principal = {}
            for principal_field, cost_str in data.items():
                key = principal_field.decode() if isinstance(principal_field, bytes) else principal_field
                raw = cost_str.decode() if isinstance(cost_str, bytes) else cost_str
                val = float(raw) if raw else 0.0
                by_principal[key] = val
            
            return {
                "tenant_id": tenant_id,
                "date": date_key,
                "by_principal": by_principal,
            }
            
        except Exception as e:
            logger.error(
                "daily_summary_by_principal_failed",
                tenant_id=tenant_id,
                date_key=date_key,
                error=str(e),
            )
            return {
                "tenant_id": tenant_id,
                "date": date_key or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "by_principal": {},
                "error": str(e),
            }
    
    def get_cost_events(
        self,
        tenant_id: str,
        count: int = 100,
        start_id: str = "+",
    ) -> list:
        """
        Get cost events from Redis stream (newest first).
        
        Args:
            tenant_id: Tenant identifier
            count: Maximum number of events to return
            start_id: Stream ID to start from ("+" for latest, default)
            
        Returns:
            List of (event_id, event_data) tuples, newest first
        """
        try:
            redis = get_sync_redis_client(self.client_id)
            stream_key = f"cost:model_usage:{tenant_id}"
            events = cast(
                Any, redis.xrevrange(tenant_key(tenant_id, stream_key), max=start_id, count=count)
            ) or []
            
            # Parse event data
            result = []
            for event_id, data in events:
                parsed = {}
                for k, v in data.items():
                    # Try to parse numeric values
                    try:
                        if "." in v:
                            parsed[k] = float(v)
                        elif v.isdigit() or (v.startswith("-") and v[1:].isdigit()):
                            parsed[k] = int(v)
                        elif v.lower() in ("true", "false"):
                            parsed[k] = v.lower() == "true"
                        else:
                            parsed[k] = v
                    except (ValueError, AttributeError):
                        parsed[k] = v
                
                result.append((event_id, parsed))
            
            return result
            
        except Exception as e:
            logger.error(
                "cost_events_fetch_failed",
                tenant_id=tenant_id,
                error=str(e),
            )
            return []

    def get_conversation_cost_summary(
        self,
        tenant_id: str,
        conversation_id: str,
        *,
        include_children: bool = False,
    ) -> Dict[str, Any]:
        """Read exact per-conversation running totals (O(1), no stream scan).

        Totals are incremented at event-write time by
        ``_update_conversation_totals``, so results are exact regardless of
        tenant event volume. When ``include_children`` is True, also sums the
        isolated child conversation IDs indexed under this conversation's
        ``:children`` set.
        """
        cid = (conversation_id or "").strip()
        empty: Dict[str, Any] = {
            "conversation_id": cid or None,
            "event_count": 0,
            "cost_usd": 0.0,
            "full_cost_usd": 0.0,
            "prompt_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "models": [],
            "providers": [],
            "include_children": include_children,
            "child_conversation_ids": [],
        }
        if not cid:
            return empty

        try:
            redis = get_sync_redis_client(self.client_id)

            ids: List[str] = [cid]
            child_ids: List[str] = []
            if include_children:
                children_logical = f"{self._conversation_totals_logical(tenant_id, cid)}:children"
                child_ids = sorted(
                    smembers_union(redis, tenant_key(tenant_id, children_logical))
                )
                ids.extend(child_ids)

            cost_usd = 0.0
            full_cost_usd = 0.0
            prompt_tokens = 0
            output_tokens = 0
            total_tokens = 0
            event_count = 0
            models: List[str] = []
            providers: List[str] = []
            seen_models: set[str] = set()
            seen_providers: set[str] = set()

            for one_id in ids:
                logical = self._conversation_totals_logical(tenant_id, one_id)
                raw = cast(Dict[Any, Any], hgetall_first(redis, tenant_key(tenant_id, logical))) or {}
                data = {_to_str(k): _to_str(v) for k, v in raw.items()}
                cost_usd += float(data.get("cost_usd") or 0)
                full_cost_usd += float(data.get("full_cost_usd") or 0)
                prompt_tokens += int(data.get("prompt_tokens") or 0)
                output_tokens += int(data.get("output_tokens") or 0)
                total_tokens += int(data.get("total_tokens") or 0)
                event_count += int(data.get("event_count") or 0)
                for member in smembers_union(redis, tenant_key(tenant_id, f"{logical}:models")):
                    if member and member not in seen_models:
                        seen_models.add(member)
                        models.append(member)
                for member in smembers_union(redis, tenant_key(tenant_id, f"{logical}:providers")):
                    if member and member not in seen_providers:
                        seen_providers.add(member)
                        providers.append(member)

            return {
                "conversation_id": cid,
                "event_count": event_count,
                "cost_usd": round(cost_usd, 6),
                "full_cost_usd": round(full_cost_usd, 6),
                "prompt_tokens": prompt_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "models": models,
                "providers": providers,
                "include_children": include_children,
                "child_conversation_ids": child_ids,
            }
        except Exception as e:
            logger.error(
                "conversation_cost_summary_failed",
                tenant_id=tenant_id,
                conversation_id=cid,
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            empty["error"] = str(e)
            return empty


# =============================================================================
# Singleton Instance
# =============================================================================

_cost_tracking_service_instance: Optional[CostTrackingService] = None


def get_cost_tracking_service() -> CostTrackingService:
    """
    Get the singleton CostTrackingService instance.
    
    Returns:
        CostTrackingService singleton instance
    """
    global _cost_tracking_service_instance
    if _cost_tracking_service_instance is None:
        _cost_tracking_service_instance = CostTrackingService()
    return _cost_tracking_service_instance


__all__ = [
    "CostTrackingService",
    "get_cost_tracking_service",
]
