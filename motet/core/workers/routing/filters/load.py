"""
Motet - Load

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Routing load for the Motet distributed framework.

Dependencies:
    - typing: Type hints and annotations
    - Base interfaces and implementations

Usage:
    from motet.core.workers.routing.filters.load import Load

Notes:
    - Provides core functionality
    - Integrates with distributed architecture
"""


from typing import Dict, List, Any

from .base import WorkerFilter


class LoadFilter(WorkerFilter):
    """
    Filter workers by load thresholds.
    
    This filter removes overloaded workers from consideration.
    """
    
    def __init__(self, max_load_threshold: float = 0.9):
        self.max_load_threshold = max_load_threshold
    
    def filter_workers(self, 
                      workers: List[Dict[str, Any]], 
                      context: Any) -> List[Dict[str, Any]]:
        """Filter out overloaded workers"""
        if not workers:
            return []
        
        available_workers = []
        
        for worker in workers:
            current_load = worker.get('current_load', 1.0)
            
            if current_load <= self.max_load_threshold:
                updated_worker = worker.copy()
                updated_worker.update({
                    'load_check_passed': True,
                    'load_headroom': self.max_load_threshold - current_load
                })
                available_workers.append(updated_worker)
        
        if len(available_workers) < len(workers):
            filtered_count = len(workers) - len(available_workers)
            print(f"🔍 LoadFilter: {len(available_workers)} available workers (filtered out {filtered_count} overloaded)")
        
        return available_workers
