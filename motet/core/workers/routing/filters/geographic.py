"""
Motet - Geographic

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Routing geographic for the Motet distributed framework.

Dependencies:
    - typing: Type hints and annotations
    - Base interfaces and implementations

Usage:
    from motet.core.workers.routing.filters.geographic import Geographic

Notes:
    - Provides core functionality
    - Integrates with distributed architecture
"""


from typing import Dict, List, Any, Optional

from .base import WorkerFilter


class GeographicFilter(WorkerFilter):
    """
    Filter workers by geographic requirements.
    
    This filter handles region preferences and data locality requirements.
    """
    
    def __init__(self, region_preferences: Optional[Dict[str, List[str]]] = None):
        self.region_preferences = region_preferences or {}
    
    def filter_workers(self, 
                      workers: List[Dict[str, Any]], 
                      context: Any) -> List[Dict[str, Any]]:
        """Filter workers based on geographic requirements"""
        if not workers:
            return []
        
        preferred_region = getattr(context, 'preferred_region', None)
        if not preferred_region:
            return workers  # No geographic filtering needed
        
        regional_workers = []
        other_workers = []
        
        for worker in workers:
            worker_region = worker.get('region', 'unknown')
            updated_worker = worker.copy()
            
            if worker_region == preferred_region:
                updated_worker.update({
                    'geographic_match': True,
                    'geographic_score': 1.0,
                    'region_preference_met': True
                })
                regional_workers.append(updated_worker)
            else:
                # Calculate distance penalty (simplified)
                distance_penalty = 0.5  # Would be calculated based on actual regions
                updated_worker.update({
                    'geographic_match': False,
                    'geographic_score': 1.0 - distance_penalty,
                    'region_preference_met': False
                })
                other_workers.append(updated_worker)
        
        # Return regional workers first, then others
        return regional_workers + other_workers
