"""
Motet - Readiness Filter

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Readiness filter for the Motet distributed framework.
    Filters workers by readiness state to ensure commands only route to workers
    that are ready to accept commands.

Dependencies:
    - typing: Type hints and annotations
    - Base worker filter interface
    - Worker readiness monitoring

Usage:
    from motet.core.workers.routing.filters.readiness import ReadinessFilter
    
    # Create readiness filter
    filter = ReadinessFilter()
    
    # Filter workers by readiness
    ready_workers = filter.filter_workers(workers, context)

Notes:
    - Provides worker readiness filtering
    - Includes readiness state validation
    - Supports readiness monitoring and health checks
    - Integrates with distributed architecture
"""

from typing import Dict, List, Any, Optional, Set

from .base import WorkerFilter


class ReadinessFilter(WorkerFilter):
    """
    Filter workers by readiness state.
    
    This filter ensures commands only route to workers that are ready
    to accept commands, replacing the monkey-patched readiness logic.
    """
    
    def __init__(self, readiness_service):
        self.readiness_service = readiness_service
    
    def filter_workers(self, 
                      workers: List[Dict[str, Any]], 
                      context: Any) -> List[Dict[str, Any]]:
        """Only return workers that are ready to accept commands"""
        if not workers:
            return []
        
        ready_workers = []
        
        for worker in workers:
            worker_id = worker.get('worker_id')
            if not worker_id:
                continue
            
            # Check if worker is in ready state (workers already have fresh readiness info)
            worker_state = worker.get('state', '')
            warmup_completed = worker.get('warmup_completed', False)
            
            if worker_state in ['ready', 'accepting'] and warmup_completed:
                # Worker is ready to accept commands
                updated_worker = worker.copy()
                updated_worker['readiness_check_passed'] = True
                ready_workers.append(updated_worker)
            
        if len(ready_workers) < len(workers):
            filtered_count = len(workers) - len(ready_workers)
            print(f"🔍 ReadinessFilter: {len(ready_workers)} ready workers (filtered out {filtered_count})")
            for worker in workers:
                if worker not in ready_workers:
                    print(f"   Filtered out {worker.get('worker_id')}: state={worker.get('state')}, warmup={worker.get('warmup_completed')}")
        else:
            print(f"🔍 ReadinessFilter: All {len(workers)} workers passed readiness check")
        
        return ready_workers
    
    def wait_for_ready_workers(self, 
                              required_capabilities: Optional[Set[str]] = None,
                              min_workers: int = 1,
                              timeout_seconds: int = 30) -> bool:
        """Wait for minimum number of ready workers"""
        try:
            return self.readiness_service.wait_for_ready_workers(
                required_capabilities=list(required_capabilities) if required_capabilities else None,
                min_workers=min_workers,
                timeout_seconds=timeout_seconds
            )
        except Exception as e:
            print(f"⚠️ Error waiting for ready workers: {e}")
            return False
