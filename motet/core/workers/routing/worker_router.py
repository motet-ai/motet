"""
Motet - Unified Worker Router

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Central routing engine that consolidates all routing logic from the previous
    scattered architecture. Provides clean, pluggable routing with comprehensive
    support for tenant routing, specific worker targeting, and dramatic scaling.

Dependencies:
    - asyncio: Asynchronous I/O
    - os: Environment guardrails for lifecycle routing
    - pydantic: Data validation and serialization
    - Routing strategies and filters
    - Worker communication and coordination

Usage:
    from motet.core.workers.routing.worker_router import WorkerRouter
    
    # Create router
    router = WorkerRouter()
    
    # Route command
    worker = await router.route_command(command, context)

Notes:
    - Provides unified routing engine
    - Includes pluggable routing strategies
    - Supports tenant routing and worker targeting
    - Integrates with distributed architecture
"""

import os
import asyncio
import time
from typing import Dict, List, Optional, Any, Set, Tuple
from pydantic import BaseModel, Field

import structlog

from .strategies.base import RoutingStrategy, RoutingContext, RoutingPriority
from .strategies import get_strategy, list_strategies
from ..worker_utils import get_lifecycle_worker_id
from .filters import ReadinessFilter, CapabilityFilter
from .filters.circuit_breaker import CircuitBreakerFilter
from .filters.edge_worker_affinity import EdgeWorkerAffinityFilter
from .filter_trace import FilterTrace

logger = structlog.get_logger(__name__)
DEBUG_MODE = os.getenv("MOTET_DEBUG_MODE", "false").lower() == "true"


class RoutingDecision(BaseModel):
    """Result of a routing decision"""
    selected_worker: Optional[Dict[str, Any]]
    strategy_used: str
    decision_time_ms: float
    available_workers: int
    filtered_workers: int
    selection_reason: str
    fallback_used: bool = False
    error: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkerRouter:
    """
    Unified routing engine that consolidates all routing logic.
    
    This router provides:
    - All sophisticated routing strategies from the unused CommandRouter
    - Worker readiness filtering and health checks
    - Tenant-based routing for multi-tenant deployments
    - Specific worker targeting with proper validation
    - Dramatic scaling capabilities with optimized selection
    - Clean, pluggable architecture without monkey patching
    """
    
    def __init__(self, 
                 readiness_service,
                 default_strategy: str = "least_loaded",
                 enable_caching: bool = True,
                 cache_ttl_seconds: int = 30):
        """
        Initialize the unified worker router.
        
        Args:
            readiness_service: Worker readiness service for health checks
            default_strategy: Default routing strategy name
            enable_caching: Enable worker list caching for performance
            cache_ttl_seconds: Cache TTL for worker lists
        """
        self.readiness_service = readiness_service
        self.default_strategy = default_strategy
        self.enable_caching = enable_caching
        self.cache_ttl_seconds = cache_ttl_seconds
        
        # Initialize filters
        self.readiness_filter = ReadinessFilter(readiness_service)
        self.capability_filter = CapabilityFilter()
        self.circuit_breaker_filter = CircuitBreakerFilter()
        self.edge_worker_affinity_filter = EdgeWorkerAffinityFilter()
        
        # Initialize state registry for state-aware routing
        self.state_registry = None
        try:
            from ...distributed.state_registry import get_state_registry
            self.state_registry = get_state_registry()
            if self.state_registry:
                logger.info("worker_router_state_registry_enabled")
            else:
                logger.info("worker_router_state_registry_unavailable")
        except Exception as e:
            logger.warning(
                "worker_router_state_registry_init_failed",
                error=str(e),
                exc_info=True,
            )
        
        # Strategy instances (created on demand)
        self._strategy_instances = {}
        
        # Performance caching
        self._worker_cache = None
        self._cache_timestamp = 0
        
        # Routing statistics
        self.routing_stats = {
            'total_requests': 0,
            'successful_routes': 0,
            'failed_routes': 0,
            'cache_hits': 0,
            'cache_misses': 0,
            'avg_decision_time_ms': 0.0,
            'strategy_usage': {},
            'tenant_usage': {},
        }
    
    def route_command(self, 
                          command: Any, 
                          target_worker_id: Optional[str] = None,
                          strategy_override: Optional[str] = None) -> RoutingDecision:
        """
        Main routing method - routes commands to optimal workers.
        
        This is the unified entry point that handles:
        - Normal strategy-based routing
        - Specific worker targeting
        - Tenant-based routing
        - Readiness and capability validation
        
        Args:
            command: Command to route
            target_worker_id: Optional specific worker to target
            strategy_override: Optional strategy override
            
        Returns:
            RoutingDecision with selected worker and metadata
        """
        start_time = time.time()
        self.routing_stats['total_requests'] += 1
        
        try:
            # Create routing context from command
            context = RoutingContext.from_command(command)
            # Defensive backfill: if a tool_execution command reaches routing with
            # empty required capabilities, derive them from tool registry metadata.
            # This keeps EDGE_* routing deterministic even if upstream capability
            # inference is bypassed during command transport/deserialization.
            self._backfill_tool_execution_capabilities(command, context)
            if target_worker_id:
                context.target_worker_id = target_worker_id
                context.require_specific_worker = True
            elif context.target_worker_id:
                # Use target worker from command context
                context.require_specific_worker = True
            
            # Get all available workers
            all_workers = self._get_all_workers()
            if not all_workers:
                return self._create_error_decision(
                    start_time, "No workers available", 0, 0
                )
            
            # Apply filters
            filtered_workers, filter_trace = self._apply_filters(all_workers, context)
            if not filtered_workers:
                # Include trace summary in error message
                killer = filter_trace.get_killer_filter()
                error_msg = f"No workers passed filtering (killer: {killer})" if killer else "No workers passed filtering"
                return self._create_error_decision(
                    start_time, error_msg, len(all_workers), 0
                )
            
            # Select routing strategy
            original_strategy_name = strategy_override or self._determine_strategy(context)
            strategy, strategy_name = self._get_strategy_instance(original_strategy_name, context)
            fallback_used = strategy_name != original_strategy_name
            
            # Route using strategy
            selected_worker = strategy.select_worker(filtered_workers, context)
            
            # If strategy failed and we're not using default, try fallback
            if not selected_worker and strategy_name != self.default_strategy:
                logger.warning(
                    "worker_router_strategy_failed_fallback_default",
                    strategy=strategy_name,
                    default_strategy=self.default_strategy,
                )
                fallback_strategy, _ = self._get_strategy_instance(self.default_strategy, context)
                selected_worker = fallback_strategy.select_worker(filtered_workers, context)
                if selected_worker:
                    strategy_name = self.default_strategy
                    fallback_used = True
            
            decision_time = (time.time() - start_time) * 1000
            
            if selected_worker:
                # Success
                self.routing_stats['successful_routes'] += 1
                self._update_strategy_stats(strategy_name)
                self._update_tenant_stats(context.tenant_id)
                
                return RoutingDecision(
                    selected_worker=selected_worker,
                    strategy_used=strategy_name,
                    decision_time_ms=decision_time,
                    available_workers=len(all_workers),
                    filtered_workers=len(filtered_workers),
                    selection_reason=selected_worker.get('selection_reason', f'Selected by {strategy_name}'),
                    fallback_used=fallback_used,
                    metadata={
                        'context': context.__dict__,
                        'strategy_metadata': strategy.get_strategy_metadata()
                    }
                )
            else:
                # All strategies failed to select worker
                return self._create_error_decision(
                    start_time, f"All strategies failed to select worker (tried {strategy_name})", 
                    len(all_workers), len(filtered_workers)
                )
                
        except Exception as e:
            return self._create_error_decision(
                start_time, f"Routing error: {str(e)}", 0, 0
            )
    
    def route_to_specific_worker(self, 
                                     command: Any, 
                                     target_worker_id: str,
                                     wait_if_not_ready: bool = True,
                                     timeout_seconds: int = 30) -> RoutingDecision:
        """
        Route command to a specific worker with comprehensive validation.
        
        This method provides enhanced specific worker routing with:
        - Proper readiness and capability validation
        - Optional waiting for worker to become ready
        - Rich error reporting and fallback options
        """
        return self.route_command(
            command, 
            target_worker_id=target_worker_id,
            strategy_override="specific_worker"
        )
    
    def get_available_workers(self, 
                                  required_capabilities: Optional[Set[str]] = None,
                                  tenant_id: Optional[str] = None,
                                  include_readiness_check: bool = True) -> List[Dict[str, Any]]:
        """
        Get list of available workers with optional filtering.
        
        Args:
            required_capabilities: Optional capability requirements
            tenant_id: Optional tenant filtering
            include_readiness_check: Whether to check worker readiness
            
        Returns:
            List of available workers
        """
        all_workers = self._get_all_workers()
        
        if not include_readiness_check:
            return all_workers
        
        # Create mock context for filtering
        context = RoutingContext(
            command_type="query",
            required_capabilities=required_capabilities or set(),
            priority=RoutingPriority.NORMAL,
            timeout_seconds=60,
            tenant_id=tenant_id
        )
        
        # Return only the filtered workers (discard trace for this public API)
        filtered_workers, _ = self._apply_filters(all_workers, context)
        return filtered_workers

    def select_worker_for_command(self, command: Any) -> Optional[Dict[str, Any]]:
        """
        Select a worker for a command using standard routing.

        This is a convenience wrapper used by Dispatch/Gather routing.
        """
        decision = self.route_command(command)
        return decision.selected_worker
    
    def get_routing_stats(self) -> Dict[str, Any]:
        """Get comprehensive routing statistics"""
        readiness_stats = self.readiness_service.get_readiness_stats()
        circuit_breaker_stats = self.circuit_breaker_filter.get_circuit_breaker_stats()
        
        return {
            **self.routing_stats,
            'readiness_stats': readiness_stats,
            'circuit_breaker_stats': circuit_breaker_stats,
            'available_strategies': list_strategies(),
            'cache_enabled': self.enable_caching,
            'cache_ttl_seconds': self.cache_ttl_seconds
        }
    
    def get_tenant_routing_info(self, tenant_id: str) -> Dict[str, Any]:
        """Get routing information for a specific tenant"""
        # Get tenant-specific workers
        available_workers = self.get_available_workers(tenant_id=tenant_id)
        
        # Get tenant usage stats
        tenant_stats = self.routing_stats['tenant_usage'].get(tenant_id, {
            'total_routes': 0,
            'successful_routes': 0,
            'failed_routes': 0
        })
        
        return {
            'tenant_id': tenant_id,
            'available_workers': len(available_workers),
            'worker_details': available_workers,
            'usage_stats': tenant_stats
        }
    
    def add_custom_strategy(self, name: str, strategy: RoutingStrategy):
        """Add a custom routing strategy"""
        self._strategy_instances[name] = strategy
        logger.info("worker_router_custom_strategy_added", strategy=name)
    
    def remove_strategy(self, name: str):
        """Remove a routing strategy"""
        if name in self._strategy_instances:
            del self._strategy_instances[name]
            logger.info("worker_router_custom_strategy_removed", strategy=name)
    
    # Private methods
    
    def _get_all_workers(self) -> List[Dict[str, Any]]:
        """Get all available workers with caching"""
        current_time = time.time()
        
        # Check cache
        if (self.enable_caching and 
            self._worker_cache is not None and 
            current_time - self._cache_timestamp < self.cache_ttl_seconds):
            self.routing_stats['cache_hits'] += 1
            return self._worker_cache
        
        # Cache miss - fetch fresh data
        self.routing_stats['cache_misses'] += 1
        
        try:
            # Get workers from readiness service (now synchronous)
            if DEBUG_MODE:
                logger.debug("worker_router_get_all_workers_start")
            all_workers_info = self.readiness_service.get_all_workers()
            if DEBUG_MODE:
                logger.debug(
                    "worker_router_get_all_workers_result",
                    workers_count=len(all_workers_info),
                    worker_ids=list(all_workers_info.keys())[:50],
                )
            
            # Convert to router format
            workers = []
            for worker_id, worker_info in all_workers_info.items():
                worker_dict = {
                    'worker_id': worker_id,
                    'state': worker_info.state.value,
                    'capabilities': worker_info.capabilities,
                    'pool_type': worker_info.pool_type,  # ADR-0033: Include pool type
                    'current_load': worker_info.active_commands / worker_info.max_concurrency if worker_info.max_concurrency > 0 else 0,
                    'active_commands': worker_info.active_commands,
                    'max_concurrency': worker_info.max_concurrency,
                    'tool_count': worker_info.tool_count,
                    'mcp_tool_count': worker_info.mcp_tool_count,
                    'warmup_completed': worker_info.warmup_completed,
                    'last_heartbeat': worker_info.last_heartbeat,
                    'uptime_seconds': current_time - worker_info.startup_time if worker_info.startup_time > 0 else 0,
                    # Edge worker identity scope (ADR-0095) — consumed by
                    # EdgeWorkerAffinityFilter; without these the filter cannot
                    # exclude edge workers owned by a different principal/tenant.
                    'owner_principal_id': worker_info.owner_principal_id,
                    'owner_tenant_id': worker_info.owner_tenant_id,
                    'command_scope': worker_info.command_scope,
                }
                workers.append(worker_dict)
            
            # Update cache
            if self.enable_caching:
                self._worker_cache = workers
                self._cache_timestamp = current_time
            
            return workers
            
        except Exception as e:
            logger.warning("worker_router_get_all_workers_failed", error=str(e), exc_info=True)
            return []
    
    def _apply_filters(self, 
                           workers: List[Dict[str, Any]], 
                           context: RoutingContext) -> Tuple[List[Dict[str, Any]], FilterTrace]:
        """
        Apply all filters to worker list with detailed tracing.
        
        Returns:
            Tuple of (filtered_workers, filter_trace)
        """
        debug_mode = DEBUG_MODE
        # Create trace for debugging
        trace = FilterTrace(len(workers))
        if debug_mode:
            logger.debug("worker_router_filter_start", workers_count=len(workers))
        
        # Apply readiness filter (now sync)
        ready_workers = self.readiness_filter.filter_workers(workers, context)
        filtered_ids = [w['worker_id'] for w in workers if w not in ready_workers]
        trace.add_step(
            "ReadinessFilter",
            len(workers),
            len(ready_workers),
            reason="Required: state='ready' or 'accepting', warmup_completed=True",
            filtered_workers=filtered_ids[:5]  # Limit to 5 for readability
        )
        if debug_mode:
            logger.debug("worker_router_filter_after_readiness", workers_count=len(ready_workers))
        if not ready_workers:
            if debug_mode:
                logger.debug("worker_router_filter_trace", trace=trace.to_string())
            return [], trace
        
        # Apply capability filter (synchronous)
        capable_workers = self.capability_filter.filter_workers(ready_workers, context)
        required_caps = context.required_capabilities if hasattr(context, 'required_capabilities') else set()
        filtered_ids = [w['worker_id'] for w in ready_workers if w not in capable_workers]
        trace.add_step(
            "CapabilityFilter",
            len(ready_workers),
            len(capable_workers),
            reason=f"Required capabilities: {required_caps}" if required_caps else "No capability requirements",
            filtered_workers=filtered_ids[:5]
        )
        if debug_mode:
            logger.debug("worker_router_filter_after_capability", workers_count=len(capable_workers))
        if not capable_workers:
            if debug_mode:
                logger.debug("worker_router_filter_trace", trace=trace.to_string())
            return [], trace

        # Commands requiring EDGE_* capabilities must execute on edge workers only.
        # This prevents accidental routing to cloud/agent workers that may advertise
        # overlapping capabilities but lack host bridges/device context.
        if self._requires_edge_worker(required_caps):
            edge_capability_workers = [
                worker
                for worker in capable_workers
                if str(worker.get("worker_id", "")).startswith("edge_")
            ]
            filtered_ids = [
                str(worker.get("worker_id"))
                for worker in capable_workers
                if worker not in edge_capability_workers and worker.get("worker_id") is not None
            ]
            trace.add_step(
                "EdgeCapabilityGuard",
                len(capable_workers),
                len(edge_capability_workers),
                reason="Required capabilities include EDGE_*; only edge_* workers are eligible",
                filtered_workers=filtered_ids[:5],
            )
            if debug_mode:
                logger.debug(
                    "worker_router_filter_after_edge_capability_guard",
                    workers_count=len(edge_capability_workers),
                )
            if not edge_capability_workers:
                if debug_mode:
                    logger.debug("worker_router_filter_trace", trace=trace.to_string())
                return [], trace
            capable_workers = edge_capability_workers

        # ADR-0095: exclude edge workers that don't match the command's identity
        affinity_workers = self.edge_worker_affinity_filter.filter_workers(capable_workers, context)
        filtered_ids = [w['worker_id'] for w in capable_workers if w not in affinity_workers]
        trace.add_step(
            "EdgeWorkerAffinityFilter",
            len(capable_workers),
            len(affinity_workers),
            reason="Edge workers filtered by principal/tenant scope (ADR-0095)",
            filtered_workers=filtered_ids[:5]
        )
        if debug_mode:
            logger.debug("worker_router_filter_after_edge_affinity", workers_count=len(affinity_workers))
        if not affinity_workers:
            if debug_mode:
                logger.debug("worker_router_filter_trace", trace=trace.to_string())
            return [], trace

        # Exclude lifecycle worker unless explicitly required or explicitly targeted
        required_values = {str(cap) for cap in required_caps}
        lifecycle_worker_id = get_lifecycle_worker_id()
        target_worker_id = getattr(context, "target_worker_id", None)
        # Allow the lifecycle worker through when: command requires deployment/lifecycle,
        # or caller explicitly targeted this worker (e.g. publish_bundle reload per worker).
        if (
            "worker_lifecycle_management" in required_values
            or "deployment" in required_values
            or target_worker_id == lifecycle_worker_id
        ):
            lifecycle_filtered_workers = affinity_workers
            lifecycle_filtered_ids = []
        else:
            lifecycle_filtered_workers = [
                worker for worker in affinity_workers
                if worker.get('worker_id') != lifecycle_worker_id
                and "worker_lifecycle_management" not in set(worker.get('capabilities', []))
            ]
            lifecycle_filtered_ids = [
                str(worker.get('worker_id')) for worker in affinity_workers
                if worker not in lifecycle_filtered_workers and worker.get('worker_id') is not None
            ]
        trace.add_step(
            "LifecycleWorkerFilter",
            len(affinity_workers),
            len(lifecycle_filtered_workers),
            reason="Lifecycle worker excluded unless explicitly required",
            filtered_workers=lifecycle_filtered_ids[:5]
        )
        if debug_mode:
            logger.debug("worker_router_filter_after_lifecycle", workers_count=len(lifecycle_filtered_workers))
        if not lifecycle_filtered_workers:
            if debug_mode:
                logger.debug("worker_router_filter_trace", trace=trace.to_string())
            return [], trace
        
        # Apply circuit breaker filter (ADR-0008 Phase 4) (now sync)
        circuit_breaker_workers = self.circuit_breaker_filter.filter_workers(
            lifecycle_filtered_workers, context
        )
        filtered_ids = [w['worker_id'] for w in lifecycle_filtered_workers if w not in circuit_breaker_workers]
        trace.add_step(
            "CircuitBreakerFilter",
            len(lifecycle_filtered_workers),
            len(circuit_breaker_workers),
            reason="Workers with open circuit breakers filtered",
            filtered_workers=filtered_ids[:5]
        )
        if debug_mode:
            logger.debug("worker_router_filter_after_circuit_breaker", workers_count=len(circuit_breaker_workers))
        
        # Apply pool type preference (ADR-0033) - BEFORE targeting filters
        # This is a soft preference, not a hard filter (workers are reordered, not removed)
        pool_type_preferred_workers = self._apply_pool_type_preference(circuit_breaker_workers, context)
        trace.add_step(
            "PoolTypePreference",
            len(circuit_breaker_workers),
            len(pool_type_preferred_workers),
            reason="Pool type preference applied (soft filter)",
            filtered_workers=[]  # No workers are actually filtered out
        )
        if debug_mode:
            logger.debug("worker_router_filter_after_pool_preference", workers_count=len(pool_type_preferred_workers))
        
        # Apply worker targeting filters (ADR-0025)
        targeted_workers = self._apply_worker_targeting_filters(pool_type_preferred_workers, context)
        filtered_ids = [w['worker_id'] for w in pool_type_preferred_workers if w not in targeted_workers]
        
        # Build targeting reason
        targeting_reason = None
        if context.target_worker_id:
            targeting_reason = f"Target worker: {context.target_worker_id}"
        elif context.require_specific_worker:
            targeting_reason = "Specific worker required"
        
        trace.add_step(
            "WorkerTargetingFilter",
            len(pool_type_preferred_workers),
            len(targeted_workers),
            reason=targeting_reason or "No specific targeting",
            filtered_workers=filtered_ids[:5]
        )
        if debug_mode:
            logger.debug("worker_router_filter_after_targeting", workers_count=len(targeted_workers))
        
        # Show trace if no workers survived all filters
        if not targeted_workers:
            if debug_mode:
                logger.debug("worker_router_filter_trace", trace=trace.to_string())
        
        return targeted_workers, trace

    @staticmethod
    def _requires_edge_worker(required_caps: Set[Any]) -> bool:
        """True when command requires any EDGE_* capability."""
        if not required_caps:
            return False
        for cap in required_caps:
            cap_value = cap.value if hasattr(cap, "value") else str(cap)
            if str(cap_value).lower().startswith("edge_"):
                return True
        return False

    @staticmethod
    def _backfill_tool_execution_capabilities(command: Any, context: RoutingContext) -> None:
        """Populate missing tool_execution capabilities from tool registry metadata."""
        if context.required_capabilities:
            return
        if context.command_type not in {"core.tool_execution", "tool_execution"}:
            return

        command_data = getattr(command, "data", None)
        tool_name = None
        if isinstance(command_data, dict):
            tool_name = command_data.get("tool_name")
        else:
            tool_name = getattr(command_data, "tool_name", None)
        if not tool_name:
            return

        try:
            from ...tools.registry import registry as tool_registry
            from motet.core.commands.capabilities import WorkerCapability
        except Exception:
            return

        tool = tool_registry.get(tool_name)
        if tool is None:
            return

        caps_raw = list(getattr(tool, "required_capabilities", []) or [])
        if not caps_raw:
            return

        inferred_caps: Set[Any] = set()
        for cap_name in caps_raw:
            cap_key = str(cap_name).strip()
            if not cap_key:
                continue
            if cap_key in WorkerCapability.__members__:
                inferred_caps.add(WorkerCapability[cap_key])
                continue
            cap_value = cap_key.lower()
            for enum_cap in WorkerCapability:
                if enum_cap.value == cap_value:
                    inferred_caps.add(enum_cap)
                    break

        if inferred_caps:
            context.required_capabilities = inferred_caps
            logger.info(
                "worker_router_backfilled_tool_capabilities",
                command_type=context.command_type,
                tool_name=tool_name,
                required_capabilities=[
                    cap.value if hasattr(cap, "value") else str(cap)
                    for cap in sorted(inferred_caps, key=lambda item: str(item))
                ],
            )
    
    def _determine_strategy(self, context: RoutingContext) -> str:
        """Determine which strategy to use based on context"""
        # Specific worker request
        if context.require_specific_worker or context.target_worker_id:
            return "specific_worker"
        
        # Tenant-based routing
        if context.tenant_id:
            return "tenant_affinity"  # or "multi_tenant" based on configuration
        
        # High priority commands
        if context.priority == RoutingPriority.CRITICAL:
            return "fastest_response"
        
        # Geographic preference
        if context.preferred_region:
            return "geographic_proximity"
        
        # Cost optimization
        if context.max_cost is not None:
            return "cost_optimized"
        
        # Default strategy
        return self.default_strategy
    
    def _get_strategy_instance(self, 
                                   strategy_name: str, 
                                   context: RoutingContext) -> tuple[RoutingStrategy, str]:
        """Get or create strategy instance, returns (strategy, actual_strategy_name)"""
        # specific_worker must be keyed by target_worker_id so each target gets correct routing
        cache_key = strategy_name
        if strategy_name == "specific_worker" and context.target_worker_id:
            cache_key = f"{strategy_name}:{context.target_worker_id}"
        if cache_key in self._strategy_instances:
            return self._strategy_instances[cache_key], strategy_name

        # Create new strategy instance
        try:
            if strategy_name == "specific_worker" and context.target_worker_id:
                from .strategies.specific import SpecificWorkerStrategy
                strategy = SpecificWorkerStrategy(context.target_worker_id)
            else:
                strategy = get_strategy(strategy_name)

            # Cache for reuse (use cache_key so specific_worker per target is cached)
            self._strategy_instances[cache_key] = strategy
            return strategy, strategy_name
            
        except Exception as e:
            logger.warning(
                "worker_router_strategy_create_failed",
                strategy=strategy_name,
                error=str(e),
                exc_info=True,
            )
            # Fallback to default
            fallback_strategy = get_strategy(self.default_strategy)
            self._strategy_instances[strategy_name] = fallback_strategy
            return fallback_strategy, self.default_strategy
    
    def _create_error_decision(self, 
                             start_time: float, 
                             error: str, 
                             available_workers: int, 
                             filtered_workers: int) -> RoutingDecision:
        """Create error routing decision"""
        self.routing_stats['failed_routes'] += 1
        
        return RoutingDecision(
            selected_worker=None,
            strategy_used="none",
            decision_time_ms=(time.time() - start_time) * 1000,
            available_workers=available_workers,
            filtered_workers=filtered_workers,
            selection_reason="",
            fallback_used=False,
            error=error
        )
    
    def _update_strategy_stats(self, strategy_name: str):
        """Update strategy usage statistics"""
        if strategy_name not in self.routing_stats['strategy_usage']:
            self.routing_stats['strategy_usage'][strategy_name] = 0
        self.routing_stats['strategy_usage'][strategy_name] += 1
    
    def _update_tenant_stats(self, tenant_id: Optional[str]):
        """Update tenant usage statistics"""
        if not tenant_id:
            return
        
        if tenant_id not in self.routing_stats['tenant_usage']:
            self.routing_stats['tenant_usage'][tenant_id] = {
                'total_routes': 0,
                'successful_routes': 0,
                'failed_routes': 0
            }
        
        stats = self.routing_stats['tenant_usage'][tenant_id]
        stats['total_routes'] += 1
        stats['successful_routes'] += 1
    
    def _apply_pool_type_preference(self,
                                    workers: List[Dict[str, Any]],
                                    context: RoutingContext) -> List[Dict[str, Any]]:
        """
        Apply pool type preference without filtering workers (ADR-0033).
        
        This is a SOFT preference mechanism that reorders workers to prefer matching
        pool types but NEVER removes workers. This ensures commands never fail due
        to pool type mismatch - they just perform better on preferred pools.
        
        Pool type mapping:
        - "high_concurrency" matches: eventlet, gevent, threads
        - "process" matches: fork
        - None (no preference): no reordering
        
        Args:
            workers: List of available workers
            context: Routing context with preferred_pool_type field
            
        Returns:
            Same workers list, potentially reordered with preferred pool types first
        """
        if not workers:
            return workers
        
        # Get preferred pool type from context (already extracted from command.distributed_context)
        preferred_pool_type = context.preferred_pool_type
        
        # If no preference, return workers as-is
        if not preferred_pool_type:
            return workers
        
        # Separate workers by pool type match
        preferred_workers = []
        other_workers = []
        
        for worker in workers:
            worker_pool_type = worker.get('pool_type', 'unknown')
            matches = False
            
            if preferred_pool_type == "high_concurrency":
                # High concurrency pools: eventlet, gevent, threads
                matches = worker_pool_type in ['eventlet', 'gevent', 'threads']
            elif preferred_pool_type == "process":
                # Process isolation pool: fork
                matches = worker_pool_type == 'fork'
            
            if matches:
                preferred_workers.append(worker)
            else:
                other_workers.append(worker)
        
        # Log preference application
        if preferred_workers:
            if DEBUG_MODE:
                logger.debug(
                    "worker_router_pool_type_preference_applied",
                    preferred_pool_type=preferred_pool_type,
                    preferred_workers_count=len(preferred_workers),
                    fallback_workers_count=len(other_workers),
                )
        
        # Return preferred workers first, then others (all workers included!)
        return preferred_workers + other_workers
    
    def _apply_worker_targeting_filters(self, 
                                       workers: List[Dict[str, Any]], 
                                       context: RoutingContext) -> List[Dict[str, Any]]:
        """
        Apply worker targeting filters based on ADR-0025 worker targeting fields.
        
        This method handles:
        - preferred_worker_ids: Prefer workers from this list
        - avoid_worker_ids: Exclude workers from this list
        - worker_affinity: Use consistent worker selection based on affinity key
        
        Args:
            workers: List of available workers
            context: Routing context with worker targeting preferences
            
        Returns:
            Filtered list of workers respecting targeting preferences
        """
        if not workers:
            return workers
        
        filtered_workers = workers.copy()
        
        # Apply avoid_worker_ids filter (exclude these workers)
        if context.avoid_worker_ids:
            original_count = len(filtered_workers)
            filtered_workers = [
                worker for worker in filtered_workers 
                if worker.get('worker_id') not in context.avoid_worker_ids
            ]
            excluded_count = original_count - len(filtered_workers)
            if excluded_count > 0:
                if DEBUG_MODE:
                    logger.debug(
                        "worker_router_avoid_worker_ids_excluded",
                        excluded_count=excluded_count,
                    )
        
        # Apply preferred_worker_ids filter (prefer these workers)
        if context.preferred_worker_ids and filtered_workers:
            # Separate preferred and non-preferred workers
            preferred_workers = []
            other_workers = []
            
            for worker in filtered_workers:
                worker_id = worker.get('worker_id')
                if worker_id in context.preferred_worker_ids:
                    preferred_workers.append(worker)
                else:
                    other_workers.append(worker)
            
            # Return preferred workers first, then others
            if preferred_workers:
                if DEBUG_MODE:
                    logger.debug(
                        "worker_router_preferred_workers_found",
                        preferred_workers_count=len(preferred_workers),
                    )
                filtered_workers = preferred_workers + other_workers
        
        # Apply worker_affinity (consistent worker selection)
        if context.worker_affinity and filtered_workers:
            # Use hash of affinity key to consistently select the same worker
            import hashlib
            affinity_hash = int(hashlib.md5(context.worker_affinity.encode()).hexdigest(), 16)
            selected_index = affinity_hash % len(filtered_workers)
            selected_worker = filtered_workers[selected_index]
            
            logger.debug(
                "worker_router_worker_affinity_selected",
                worker_id=selected_worker.get("worker_id"),
            )
            filtered_workers = [selected_worker]
        
        return filtered_workers
