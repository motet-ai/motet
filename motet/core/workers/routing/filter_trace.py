"""
Motet - Filter Trace

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Routing filter trace for the Motet distributed framework.

Dependencies:
    - typing: Type hints and annotations
    - Base interfaces and implementations

Usage:
    from motet.core.workers.routing.filter_trace import FilterTrace

Notes:
    - Provides core functionality
    - Integrates with distributed architecture
"""


from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field


@dataclass
class FilterStep:
    """Single step in the filter trace"""
    filter_name: str
    workers_before: int
    workers_after: int
    reason: Optional[str] = None
    filtered_workers: Optional[List[str]] = None  # IDs of filtered workers
    
    @property
    def filtered_count(self) -> int:
        """Number of workers filtered out in this step"""
        return self.workers_before - self.workers_after
    
    @property
    def is_killer(self) -> bool:
        """True if this filter reduced workers to zero"""
        return self.workers_after == 0


class FilterTrace:
    """
    Captures the filtering pipeline for debugging.
    
    This trace tracks each filter's impact on the worker pool,
    making it easy to identify which filter caused routing failures.
    
    Example:
        trace = FilterTrace(initial_count=5)
        trace.add_step("ReadinessFilter", 5, 4, "1 worker not ready")
        trace.add_step("CapabilityFilter", 4, 0, "Required: http_operations")
        
        print(trace.to_string())
        # Output:
        # 🔍 Filter Trace (started with 5 workers):
        #   ├─ ReadinessFilter: 5 → 4 (filtered 1)
        #      └─ 1 worker not ready
        #   ├─ CapabilityFilter: 4 → 0 (filtered 4)
        #      └─ Required: http_operations
        #   ❌ Killer Filter: CapabilityFilter
    """
    
    def __init__(self, initial_count: int):
        """
        Initialize filter trace.
        
        Args:
            initial_count: Number of workers at start of filtering
        """
        self.initial_count = initial_count
        self.steps: List[FilterStep] = []
    
    def add_step(self, 
                 filter_name: str, 
                 workers_before: int, 
                 workers_after: int,
                 reason: Optional[str] = None,
                 filtered_workers: Optional[List[str]] = None) -> None:
        """
        Add a filter step to the trace.
        
        Args:
            filter_name: Name of the filter
            workers_before: Worker count before filter
            workers_after: Worker count after filter
            reason: Optional reason for filtering (e.g., "Required capabilities: {set}")
            filtered_workers: Optional list of worker IDs that were filtered out
        """
        step = FilterStep(
            filter_name=filter_name,
            workers_before=workers_before,
            workers_after=workers_after,
            reason=reason,
            filtered_workers=filtered_workers
        )
        self.steps.append(step)
    
    def get_killer_filter(self) -> Optional[str]:
        """
        Returns the filter that reduced workers to zero.
        
        Returns:
            Name of the killer filter, or None if no filter killed all workers
        """
        for step in self.steps:
            if step.is_killer:
                return step.filter_name
        return None
    
    def get_total_filtered(self) -> int:
        """
        Returns total number of workers filtered across all steps.
        
        Note: This is NOT the same as (initial_count - final_count) due to
        workers potentially being added back by some filters.
        """
        return sum(step.filtered_count for step in self.steps)
    
    def get_final_count(self) -> int:
        """
        Returns the final worker count after all filters.
        
        Returns:
            Final worker count, or initial_count if no steps recorded
        """
        if not self.steps:
            return self.initial_count
        return self.steps[-1].workers_after
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert trace to dictionary for JSON serialization.
        
        Returns:
            Dictionary representation of the trace
        """
        return {
            "initial_count": self.initial_count,
            "final_count": self.get_final_count(),
            "total_filtered": self.get_total_filtered(),
            "killer_filter": self.get_killer_filter(),
            "steps": [
                {
                    "filter": step.filter_name,
                    "before": step.workers_before,
                    "after": step.workers_after,
                    "filtered": step.filtered_count,
                    "reason": step.reason,
                    "filtered_workers": step.filtered_workers
                }
                for step in self.steps
            ]
        }
    
    def to_string(self) -> str:
        """
        Format trace as a readable string for logging.
        
        Returns:
            Multi-line string representation of the filter trace
        """
        if not self.steps:
            return f"🔍 Filter Trace (started with {self.initial_count} workers, no filters applied)"
        
        lines = [f"🔍 Filter Trace (started with {self.initial_count} workers):"]
        
        for i, step in enumerate(self.steps):
            # Use different tree characters for last step
            is_last = (i == len(self.steps) - 1)
            prefix = "  └─" if is_last else "  ├─"
            
            # Build step line
            step_line = (
                f"{prefix} {step.filter_name}: {step.workers_before} → {step.workers_after} "
                f"(filtered {step.filtered_count})"
            )
            lines.append(step_line)
            
            # Add reason if provided
            if step.reason:
                reason_prefix = "     " if is_last else "  │  "
                lines.append(f"{reason_prefix}└─ {step.reason}")
            
            # Add filtered worker IDs if provided and count is reasonable
            if step.filtered_workers and len(step.filtered_workers) <= 5:
                worker_ids = ", ".join(step.filtered_workers)
                worker_prefix = "     " if is_last else "  │  "
                lines.append(f"{worker_prefix}└─ Filtered: {worker_ids}")
        
        # Add killer filter indicator
        killer = self.get_killer_filter()
        if killer:
            lines.append(f"  ❌ Killer Filter: {killer}")
        
        return "\n".join(lines)
    
    def __str__(self) -> str:
        """String representation for print()"""
        return self.to_string()
    
    def __repr__(self) -> str:
        """Developer representation"""
        return (
            f"FilterTrace(initial={self.initial_count}, "
            f"final={self.get_final_count()}, "
            f"steps={len(self.steps)})"
        )


def create_filter_trace(initial_count: int) -> FilterTrace:
    """
    Factory function to create a filter trace.
    
    Args:
        initial_count: Number of workers at start of filtering
        
    Returns:
        New FilterTrace instance
    """
    return FilterTrace(initial_count)

