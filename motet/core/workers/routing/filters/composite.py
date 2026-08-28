"""
Motet - Composite

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Routing composite for the Motet distributed framework.

Dependencies:
    - typing: Type hints and annotations
    - Base interfaces and implementations

Usage:
    from motet.core.workers.routing.filters.composite import Composite

Notes:
    - Provides core functionality
    - Integrates with distributed architecture
"""


from typing import Dict, List, Any, Optional

from .base import WorkerFilter
from .readiness import ReadinessFilter
from .capability import CapabilityFilter
from .tenant import TenantFilter
from .load import LoadFilter


class CompositeFilter(WorkerFilter):
    """
    Composite filter that applies multiple filters in sequence.
    
    This allows combining multiple filtering criteria cleanly.
    """
    
    def __init__(self, filters: List[WorkerFilter]):
        self.filters = filters
    
    def filter_workers(self, 
                      workers: List[Dict[str, Any]], 
                      context: Any) -> List[Dict[str, Any]]:
        """Apply all filters in sequence"""
        current_workers = workers
        
        for filter_instance in self.filters:
            if not current_workers:
                break  # No workers left to filter
            
            current_workers = filter_instance.filter_workers(current_workers, context)
        
        return current_workers
    
    def get_filter_name(self) -> str:
        filter_names = [f.get_filter_name() for f in self.filters]
        return f"Composite({', '.join(filter_names)})"


# Utility functions for common filtering operations

def apply_standard_filters(workers: List[Dict[str, Any]], 
                          context: Any,
                          readiness_service) -> List[Dict[str, Any]]:
    """Apply the standard set of filters (readiness + capability)"""
    composite_filter = CompositeFilter([
        ReadinessFilter(readiness_service),
        CapabilityFilter()
    ])
    
    return composite_filter.filter_workers(workers, context)


def apply_tenant_filters(workers: List[Dict[str, Any]], 
                        context: Any,
                        readiness_service,
                        tenant_assignments: Optional[Dict[str, List[str]]] = None,
                        enforce_isolation: bool = False) -> List[Dict[str, Any]]:
    """Apply filters for tenant-aware routing"""
    composite_filter = CompositeFilter([
        ReadinessFilter(readiness_service),
        CapabilityFilter(),
        TenantFilter(tenant_assignments, enforce_isolation),
        LoadFilter(max_load_threshold=0.8)  # More conservative for tenant workloads
    ])
    
    return composite_filter.filter_workers(workers, context)
