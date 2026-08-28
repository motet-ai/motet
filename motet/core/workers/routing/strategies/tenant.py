"""
Motet - Tenant

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Routing tenant for the Motet distributed framework.

Dependencies:
    - typing: Type hints and annotations
    - Base interfaces and implementations

Usage:
    from motet.core.workers.routing.strategies.tenant import Tenant

Notes:
    - Provides core functionality
    - Integrates with distributed architecture
"""


from typing import Dict, List, Optional, Any, Set
from .base import RoutingStrategy, RoutingContext, WorkerScore, calculate_load_score


class TenantAffinityStrategy(RoutingStrategy):
    """
    Route commands to workers with affinity for specific tenants.
    
    This strategy prefers workers that have been designated for specific tenants
    or have historically handled commands for those tenants well.
    """
    
    def __init__(self, tenant_worker_map: Optional[Dict[str, List[str]]] = None):
        """
        Initialize tenant affinity strategy.
        
        Args:
            tenant_worker_map: Optional mapping of tenant_id -> list of preferred worker_ids
        """
        self.tenant_worker_map = tenant_worker_map or {}
        self.tenant_history = {}  # Track which workers have handled which tenants
    
    def select_worker(self, 
                     workers: List[Dict[str, Any]], 
                     context: RoutingContext) -> Optional[Dict[str, Any]]:
        """Select worker with best tenant affinity"""
        if not workers:
            return None
        
        tenant_id = context.tenant_id
        if not tenant_id:
            # No tenant specified, fall back to least loaded
            return min(workers, key=lambda w: w.get('current_load', 1.0))
        
        # Check for explicitly mapped workers first
        preferred_workers = self.tenant_worker_map.get(tenant_id, [])
        for worker_id in preferred_workers:
            for worker in workers:
                if worker.get('worker_id') == worker_id:
                    worker = worker.copy()
                    worker['selection_reason'] = f"Tenant affinity mapping for {tenant_id}"
                    return worker
        
        # Check for workers with historical affinity
        tenant_workers = []
        for worker in workers:
            worker_id = worker.get('worker_id')
            if worker_id in self.tenant_history.get(tenant_id, set()):
                tenant_workers.append(worker)
        
        if tenant_workers:
            # Select least loaded among tenant-experienced workers
            selected = min(tenant_workers, key=lambda w: w.get('current_load', 1.0))
            selected = selected.copy()
            selected['selection_reason'] = f"Historical tenant affinity for {tenant_id}"
            
            # Update history
            _wid = selected.get("worker_id")
            if _wid is not None:
                self._update_tenant_history(tenant_id, str(_wid))
            return selected
        
        # No affinity found, select least loaded and establish new affinity
        selected = min(workers, key=lambda w: w.get('current_load', 1.0))
        selected = selected.copy()
        selected['selection_reason'] = f"New tenant affinity established for {tenant_id}"
        
        # Update history
        _wid = selected.get("worker_id")
        if _wid is not None:
            self._update_tenant_history(tenant_id, str(_wid))
        return selected
    
    def _update_tenant_history(self, tenant_id: str, worker_id: str):
        """Update tenant-worker history"""
        if tenant_id not in self.tenant_history:
            self.tenant_history[tenant_id] = set()
        self.tenant_history[tenant_id].add(worker_id)
    
    def get_strategy_name(self) -> str:
        return "Tenant Affinity"
    
    def add_tenant_mapping(self, tenant_id: str, worker_ids: List[str]):
        """Add or update tenant-to-worker mapping"""
        self.tenant_worker_map[tenant_id] = worker_ids
    
    def get_tenant_workers(self, tenant_id: str) -> List[str]:
        """Get workers associated with a tenant"""
        explicit = self.tenant_worker_map.get(tenant_id, [])
        historical = list(self.tenant_history.get(tenant_id, set()))
        return list(set(explicit + historical))


class TenantIsolationStrategy(RoutingStrategy):
    """
    Ensure strict tenant isolation by routing to dedicated tenant workers.
    
    This strategy enforces that tenants only use workers specifically
    allocated to them, providing strong isolation guarantees.
    """
    
    def __init__(self, tenant_worker_assignments: Dict[str, List[str]]):
        """
        Initialize tenant isolation strategy.
        
        Args:
            tenant_worker_assignments: Mapping of tenant_id -> list of dedicated worker_ids
        """
        self.tenant_worker_assignments = tenant_worker_assignments
        
        # Create reverse mapping for validation
        self.worker_tenant_map = {}
        for tenant_id, worker_ids in tenant_worker_assignments.items():
            for worker_id in worker_ids:
                if worker_id in self.worker_tenant_map:
                    raise ValueError(f"Worker {worker_id} assigned to multiple tenants")
                self.worker_tenant_map[worker_id] = tenant_id
    
    def select_worker(self, 
                     workers: List[Dict[str, Any]], 
                     context: RoutingContext) -> Optional[Dict[str, Any]]:
        """Select worker from tenant's dedicated pool"""
        if not workers:
            return None
        
        tenant_id = context.tenant_id
        if not tenant_id:
            return None  # Strict isolation requires tenant ID
        
        if tenant_id not in self.tenant_worker_assignments:
            return None  # No workers assigned to this tenant
        
        # Filter to only tenant's dedicated workers
        tenant_workers = []
        assigned_worker_ids = set(self.tenant_worker_assignments[tenant_id])
        
        for worker in workers:
            worker_id = worker.get('worker_id')
            if worker_id in assigned_worker_ids:
                tenant_workers.append(worker)
        
        if not tenant_workers:
            return None  # No tenant workers available
        
        # Select least loaded among tenant's workers
        selected = min(tenant_workers, key=lambda w: w.get('current_load', 1.0))
        selected = selected.copy()
        selected['selection_reason'] = f"Tenant isolation for {tenant_id}"
        selected['tenant_isolation'] = True
        
        return selected
    
    def get_strategy_name(self) -> str:
        return "Tenant Isolation"
    
    def add_tenant_workers(self, tenant_id: str, worker_ids: List[str]):
        """Add workers to a tenant's dedicated pool"""
        # Validate no worker conflicts
        for worker_id in worker_ids:
            if worker_id in self.worker_tenant_map and self.worker_tenant_map[worker_id] != tenant_id:
                raise ValueError(f"Worker {worker_id} already assigned to tenant {self.worker_tenant_map[worker_id]}")
        
        # Add to assignments
        if tenant_id not in self.tenant_worker_assignments:
            self.tenant_worker_assignments[tenant_id] = []
        
        self.tenant_worker_assignments[tenant_id].extend(worker_ids)
        
        # Update reverse mapping
        for worker_id in worker_ids:
            self.worker_tenant_map[worker_id] = tenant_id
    
    def remove_tenant_worker(self, tenant_id: str, worker_id: str):
        """Remove a worker from tenant's pool"""
        if tenant_id in self.tenant_worker_assignments:
            if worker_id in self.tenant_worker_assignments[tenant_id]:
                self.tenant_worker_assignments[tenant_id].remove(worker_id)
            
        if worker_id in self.worker_tenant_map:
            del self.worker_tenant_map[worker_id]
    
    def get_tenant_utilization(self, tenant_id: str) -> Dict[str, Any]:
        """Get utilization stats for a tenant's workers"""
        if tenant_id not in self.tenant_worker_assignments:
            return {"error": "Tenant not found"}
        
        worker_ids = self.tenant_worker_assignments[tenant_id]
        return {
            "tenant_id": tenant_id,
            "assigned_workers": len(worker_ids),
            "worker_ids": worker_ids
        }


class MultiTenantStrategy(RoutingStrategy):
    """
    Optimized routing for multi-tenant environments with load balancing.
    
    This strategy balances between tenant affinity and overall system efficiency,
    allowing controlled sharing of workers while maintaining tenant preferences.
    """
    
    def __init__(self, 
                 tenant_priorities: Optional[Dict[str, int]] = None,
                 max_tenant_load_per_worker: float = 0.8,
                 enable_cross_tenant_sharing: bool = True):
        """
        Initialize multi-tenant strategy.
        
        Args:
            tenant_priorities: Optional mapping of tenant_id -> priority (higher = more important)
            max_tenant_load_per_worker: Maximum load before considering other workers
            enable_cross_tenant_sharing: Allow workers to handle multiple tenants
        """
        self.tenant_priorities = tenant_priorities or {}
        self.max_tenant_load_per_worker = max_tenant_load_per_worker
        self.enable_cross_tenant_sharing = enable_cross_tenant_sharing
        
        # Track tenant-worker relationships
        self.tenant_worker_usage = {}  # tenant_id -> {worker_id: usage_count}
        self.worker_tenant_load = {}   # worker_id -> {tenant_id: current_load}
    
    def select_worker(self, 
                     workers: List[Dict[str, Any]], 
                     context: RoutingContext) -> Optional[Dict[str, Any]]:
        """Select worker optimized for multi-tenant environment"""
        if not workers:
            return None
        
        tenant_id = context.tenant_id
        if not tenant_id:
            # No tenant, use simple load balancing
            return min(workers, key=lambda w: w.get('current_load', 1.0))
        
        # Score workers for this tenant
        scored_workers = []
        for worker in workers:
            score = self._calculate_multi_tenant_score(worker, tenant_id, context)
            scored_workers.append((score, worker))
        
        # Sort by score (descending)
        scored_workers.sort(key=lambda x: x[0], reverse=True)
        
        if scored_workers:
            selected = scored_workers[0][1].copy()
            selected['selection_reason'] = f"Multi-tenant optimization for {tenant_id} (score: {scored_workers[0][0]:.2f})"
            selected['tenant_score'] = scored_workers[0][0]
            
            # Update usage tracking
            self._update_usage_tracking(tenant_id, selected.get('worker_id'))
            
            return selected
        
        return None
    
    def _calculate_multi_tenant_score(self, 
                                    worker: Dict[str, Any], 
                                    tenant_id: str, 
                                    context: RoutingContext) -> float:
        """Calculate multi-tenant score for a worker"""
        worker_id = worker.get('worker_id')
        score = 0.0
        
        # Base load score (lower load = higher score)
        current_load = worker.get('current_load', 1.0)
        load_score = max(0.0, 1.0 - current_load)
        score += load_score * 0.4
        
        # Tenant affinity score
        if tenant_id in self.tenant_worker_usage:
            usage_count = self.tenant_worker_usage[tenant_id].get(worker_id, 0)
            affinity_score = min(1.0, usage_count / 10.0)  # Normalize usage
            score += affinity_score * 0.3
        
        # Tenant priority score
        tenant_priority = self.tenant_priorities.get(tenant_id, 5)
        priority_score = tenant_priority / 10.0  # Normalize to 0-1
        score += priority_score * 0.2
        
        # Cross-tenant penalty (if sharing disabled or worker overloaded)
        if not self.enable_cross_tenant_sharing:
            if worker_id in self.worker_tenant_load:
                other_tenants = [t for t in self.worker_tenant_load[worker_id].keys() if t != tenant_id]
                if other_tenants:
                    score *= 0.5  # Penalty for cross-tenant usage
        else:
            # Check if worker is overloaded with other tenants
            if worker_id in self.worker_tenant_load:
                total_tenant_load = sum(self.worker_tenant_load[worker_id].values())
                if total_tenant_load > self.max_tenant_load_per_worker:
                    score *= 0.7  # Penalty for overloaded worker
        
        # Capability bonus
        required_caps = context.required_capabilities
        if required_caps:
            worker_caps = set(worker.get('capabilities', []))
            if required_caps.issubset(worker_caps):
                extra_caps = len(worker_caps) - len(required_caps)
                capability_bonus = min(0.1, extra_caps * 0.02)
                score += capability_bonus
        
        return score
    
    def _update_usage_tracking(self, tenant_id: str, worker_id: str):
        """Update tenant-worker usage tracking"""
        # Update usage count
        if tenant_id not in self.tenant_worker_usage:
            self.tenant_worker_usage[tenant_id] = {}
        
        self.tenant_worker_usage[tenant_id][worker_id] = \
            self.tenant_worker_usage[tenant_id].get(worker_id, 0) + 1
        
        # Update load tracking (simplified)
        if worker_id not in self.worker_tenant_load:
            self.worker_tenant_load[worker_id] = {}
        
        self.worker_tenant_load[worker_id][tenant_id] = \
            self.worker_tenant_load[worker_id].get(tenant_id, 0.0) + 0.1
    
    def get_strategy_name(self) -> str:
        return "Multi-Tenant"
    
    def get_tenant_stats(self, tenant_id: str) -> Dict[str, Any]:
        """Get statistics for a specific tenant"""
        usage = self.tenant_worker_usage.get(tenant_id, {})
        total_usage = sum(usage.values())
        
        return {
            "tenant_id": tenant_id,
            "total_commands": total_usage,
            "workers_used": len(usage),
            "worker_usage": usage,
            "priority": self.tenant_priorities.get(tenant_id, 5)
        }
    
    def get_system_tenant_stats(self) -> Dict[str, Any]:
        """Get system-wide tenant statistics"""
        total_tenants = len(self.tenant_worker_usage)
        total_commands = sum(
            sum(usage.values()) 
            for usage in self.tenant_worker_usage.values()
        )
        
        return {
            "total_tenants": total_tenants,
            "total_commands": total_commands,
            "cross_tenant_sharing_enabled": self.enable_cross_tenant_sharing,
            "max_tenant_load_per_worker": self.max_tenant_load_per_worker,
            "tenant_priorities": self.tenant_priorities
        }
