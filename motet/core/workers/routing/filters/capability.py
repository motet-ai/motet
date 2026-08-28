"""
Motet - Capability

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Routing capability for the Motet distributed framework.

Dependencies:
    - typing: Type hints and annotations
    - Base interfaces and implementations

Usage:
    from motet.core.workers.routing.filters.capability import Capability

Notes:
    - Provides core functionality
    - Integrates with distributed architecture
"""


from typing import Dict, List, Any, Set

from .base import WorkerFilter


class CapabilityFilter(WorkerFilter):
    """
    Filter workers by required capabilities.
    
    This filter ensures workers have all capabilities required by the command.
    """
    
    def filter_workers(self, 
                      workers: List[Dict[str, Any]], 
                      context: Any) -> List[Dict[str, Any]]:
        """Only return workers with required capabilities"""
        if not workers:
            return []
        
        # Extract required capabilities from context
        required_capabilities = getattr(context, 'required_capabilities', set())
        if not required_capabilities:
            return workers  # No capability requirements
        
        # Convert enum capabilities to strings for comparison
        required_capability_strings = set()
        for cap in required_capabilities:
            if hasattr(cap, 'value'):
                required_capability_strings.add(cap.value)
            else:
                required_capability_strings.add(str(cap))
        
        capable_workers = []
        
        for worker in workers:
            worker_capabilities = set(worker.get('capabilities', []))
            
            # Check if worker has all required capabilities
            if required_capability_strings.issubset(worker_capabilities):
                # Calculate capability match score
                extra_capabilities = len(worker_capabilities) - len(required_capability_strings)
                capability_score = 1.0 + (extra_capabilities * 0.1)  # Bonus for extra capabilities
                
                updated_worker = worker.copy()
                updated_worker.update({
                    'capability_match_score': capability_score,
                    'extra_capabilities': extra_capabilities,
                    'capability_check_passed': True
                })
                capable_workers.append(updated_worker)
        
        if len(capable_workers) < len(workers):
            filtered_count = len(workers) - len(capable_workers)
            print(f"🔍 CapabilityFilter: {len(capable_workers)} capable workers (filtered out {filtered_count})")
            print(f"   Required capabilities: {required_capability_strings}")
        
        return capable_workers
    
    def get_capability_match_score(self, 
                                 worker_capabilities: Set[str], 
                                 required_capabilities: Set[str]) -> float:
        """Calculate how well worker capabilities match requirements"""
        if not required_capabilities:
            return 1.0
        
        if not required_capabilities.issubset(worker_capabilities):
            return 0.0  # Missing required capabilities
        
        # Score based on capability overlap and extras
        overlap = len(required_capabilities.intersection(worker_capabilities))
        extras = len(worker_capabilities) - len(required_capabilities)
        
        base_score = overlap / len(required_capabilities)  # Should be 1.0 for exact match
        bonus = min(0.5, extras * 0.1)  # Bonus for extra capabilities (capped at 0.5)
        
        return base_score + bonus
