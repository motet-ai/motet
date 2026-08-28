"""
Motet - Base Worker Filter Interface

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Base worker filter interface for the Motet distributed framework.
    Defines abstract base class for all worker filters.

Dependencies:
    - abc: Abstract base classes
    - typing: Type hints and annotations
    - Worker filtering logic

Usage:
    from motet.core.workers.routing.filters.base import WorkerFilter
    
    # Create custom filter
    class MyFilter(WorkerFilter):
        def filter_workers(self, workers, context):
            # Implementation
            pass

Notes:
    - Provides abstract base class for worker filters
    - Includes filter interface and methods
    - Supports pluggable filtering logic
    - Integrates with distributed architecture
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Any


class WorkerFilter(ABC):
    """Base interface for worker filtering"""
    
    @abstractmethod
    def filter_workers(self, 
                      workers: List[Dict[str, Any]], 
                      context: Any) -> List[Dict[str, Any]]:
        """Filter workers based on specific criteria"""
        pass
    
    def get_filter_name(self) -> str:
        """Get human-readable filter name"""
        return self.__class__.__name__
