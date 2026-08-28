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
    from motet.core.workers.routing.strategies.geographic import Geographic

Notes:
    - Provides core functionality
    - Integrates with distributed architecture
"""


from typing import Dict, List, Optional, Any
from .base import RoutingStrategy, RoutingContext


class GeographicProximityStrategy(RoutingStrategy):
    """Route to geographically closest workers"""
    
    def select_worker(self, workers: List[Dict[str, Any]], context: RoutingContext) -> Optional[Dict[str, Any]]:
        if not workers:
            return None
        
        preferred_region = context.preferred_region
        if not preferred_region:
            # No preference, use least loaded
            return min(workers, key=lambda w: w.get('current_load', 1.0))
        
        # Find workers in preferred region
        regional_workers = [w for w in workers if w.get('region') == preferred_region]
        
        if regional_workers:
            selected = min(regional_workers, key=lambda w: w.get('current_load', 1.0))
            selected = selected.copy()
            selected['selection_reason'] = f"Geographic proximity to {preferred_region}"
            return selected
        
        # No workers in preferred region, fall back to closest
        return min(workers, key=lambda w: w.get('current_load', 1.0))
    
    def get_strategy_name(self) -> str:
        return "Geographic Proximity"


class DataLocalityStrategy(RoutingStrategy):
    """Route based on data locality requirements"""
    
    def select_worker(self, workers: List[Dict[str, Any]], context: RoutingContext) -> Optional[Dict[str, Any]]:
        if not workers:
            return None
        
        # Simple implementation - prefer workers with data locality indicators
        local_workers = [w for w in workers if w.get('has_local_data', False)]
        
        if local_workers:
            selected = min(local_workers, key=lambda w: w.get('current_load', 1.0))
            selected = selected.copy()
            selected['selection_reason'] = "Data locality optimization"
            return selected
        
        return min(workers, key=lambda w: w.get('current_load', 1.0))
    
    def get_strategy_name(self) -> str:
        return "Data Locality"


class RegionalStrategy(RoutingStrategy):
    """Route within specific regions with failover"""
    
    def __init__(self, region_priority: Optional[List[str]] = None):
        self.region_priority = region_priority or []
    
    def select_worker(self, workers: List[Dict[str, Any]], context: RoutingContext) -> Optional[Dict[str, Any]]:
        if not workers:
            return None
        
        # Try regions in priority order
        for region in self.region_priority:
            regional_workers = [w for w in workers if w.get('region') == region]
            if regional_workers:
                selected = min(regional_workers, key=lambda w: w.get('current_load', 1.0))
                selected = selected.copy()
                selected['selection_reason'] = f"Regional priority: {region}"
                return selected
        
        # No priority regions available, use any worker
        return min(workers, key=lambda w: w.get('current_load', 1.0))
    
    def get_strategy_name(self) -> str:
        return "Regional"
