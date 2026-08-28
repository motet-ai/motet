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
    from motet.core.workers.routing.filters.tenant import Tenant

Notes:
    - Provides core functionality
    - Integrates with distributed architecture
"""


from typing import Dict, List, Any, Optional

from .base import WorkerFilter


class TenantFilter(WorkerFilter):
    """
    Filter workers by tenant requirements.
    
    This filter handles tenant isolation and affinity requirements.
    """
    
    def __init__(self, 
                 tenant_worker_assignments: Optional[Dict[str, List[str]]] = None,
                 enforce_isolation: bool = False):
        self.tenant_worker_assignments = tenant_worker_assignments or {}
        self.enforce_isolation = enforce_isolation
    
    def filter_workers(self, 
                      workers: List[Dict[str, Any]], 
                      context: Any) -> List[Dict[str, Any]]:
        """Filter workers based on tenant requirements"""
        if not workers:
            return []
        
        tenant_id = getattr(context, 'tenant_id', None)
        if not tenant_id:
            return workers  # No tenant filtering needed
        
        if self.enforce_isolation:
            # Strict isolation - only workers assigned to this tenant
            assigned_workers = self.tenant_worker_assignments.get(tenant_id, [])
            if not assigned_workers:
                return []  # No workers assigned to tenant
            
            tenant_workers = []
            for worker in workers:
                if worker.get('worker_id') in assigned_workers:
                    updated_worker = worker.copy()
                    updated_worker.update({
                        'tenant_isolation': True,
                        'tenant_assigned': True
                    })
                    tenant_workers.append(updated_worker)
            
            return tenant_workers
        else:
            # Soft affinity - prefer assigned workers but allow others
            assigned_workers = self.tenant_worker_assignments.get(tenant_id, [])
            
            preferred_workers = []
            other_workers = []
            
            for worker in workers:
                updated_worker = worker.copy()
                if worker.get('worker_id') in assigned_workers:
                    updated_worker.update({
                        'tenant_affinity': True,
                        'tenant_preference_score': 1.0
                    })
                    preferred_workers.append(updated_worker)
                else:
                    updated_worker.update({
                        'tenant_affinity': False,
                        'tenant_preference_score': 0.5
                    })
                    other_workers.append(updated_worker)
            
            # Return preferred workers first, then others
            return preferred_workers + other_workers
