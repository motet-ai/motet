"""
Motet - Specific

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Routing specific for the Motet distributed framework.

Dependencies:
    - typing: Type hints and annotations
    - Base interfaces and implementations

Usage:
    from motet.core.workers.routing.strategies.specific import Specific

Notes:
    - Provides core functionality
    - Integrates with distributed architecture
"""


from typing import Dict, List, Optional, Any, Set
from .base import RoutingStrategy, RoutingContext, WorkerScore
import time

class SpecificWorkerStrategy(RoutingStrategy):
    """
    Route command to a specific worker by ID.
    
    This strategy provides enhanced specific worker routing with
    proper validation, fallback options, and rich error reporting.
    """
    
    def __init__(self, 
                 target_worker_id: str,
                 allow_fallback: bool = False,
                 fallback_strategy: Optional[RoutingStrategy] = None):
        """
        Initialize specific worker strategy.
        
        Args:
            target_worker_id: ID of the target worker
            allow_fallback: Allow fallback if target worker unavailable
            fallback_strategy: Strategy to use for fallback
        """
        self.target_worker_id = target_worker_id
        self.allow_fallback = allow_fallback
        self.fallback_strategy = fallback_strategy
    
    def select_worker(self, 
                     workers: List[Dict[str, Any]], 
                     context: RoutingContext) -> Optional[Dict[str, Any]]:
        """Select the specific target worker"""
        if not workers:
            return None
        
        # Look for the target worker
        for worker in workers:
            if worker.get('worker_id') == self.target_worker_id:
                # Validate worker is suitable
                if self._validate_worker(worker, context):
                    selected = worker.copy()
                    selected['selection_reason'] = f"Specific worker requested: {self.target_worker_id}"
                    selected['specific_worker_match'] = True
                    return selected
                else:
                    # Worker found but not suitable
                    if self.allow_fallback and self.fallback_strategy:
                        fallback_worker = self.fallback_strategy.select_worker(workers, context)
                        if fallback_worker:
                            fallback_worker = fallback_worker.copy()
                            fallback_worker['selection_reason'] = f"Fallback from {self.target_worker_id}: {fallback_worker.get('selection_reason', 'fallback')}"
                            fallback_worker['specific_worker_fallback'] = True
                            fallback_worker['original_target'] = self.target_worker_id
                            return fallback_worker
                    return None
        
        # Target worker not found
        if self.allow_fallback and self.fallback_strategy:
            fallback_worker = self.fallback_strategy.select_worker(workers, context)
            if fallback_worker:
                fallback_worker = fallback_worker.copy()
                fallback_worker['selection_reason'] = f"Fallback (target {self.target_worker_id} not found): {fallback_worker.get('selection_reason', 'fallback')}"
                fallback_worker['specific_worker_fallback'] = True
                fallback_worker['original_target'] = self.target_worker_id
                return fallback_worker
        
        return None
    
    def _validate_worker(self, worker: Dict[str, Any], context: RoutingContext) -> bool:
        """Validate that the worker is suitable for the command"""
        # Check readiness (state is WorkerState.value: "ready", "accepting", "busy", etc.)
        state = worker.get('state')
        if isinstance(state, str):
            state = state.upper()
        if state not in ('READY', 'ACCEPTING'):
            return False
        
        # Check capabilities
        worker_capabilities = set(worker.get('capabilities', []))
        required_capabilities = context.required_capabilities
        if required_capabilities and not required_capabilities.issubset(worker_capabilities):
            return False
        
        # Check load (allow some overload for specific requests)
        current_load = worker.get('current_load', 0.0)
        if current_load > 0.95:  # 95% threshold for specific workers
            return False
        
        return True
    
    def get_strategy_name(self) -> str:
        return f"Specific Worker ({self.target_worker_id})"
    
    def supports_context(self, context: RoutingContext) -> bool:
        """This strategy only supports contexts with specific worker requirements"""
        return context.require_specific_worker or context.target_worker_id is not None


class SessionAffinityStrategy(RoutingStrategy):
    """
    Route commands to workers based on session affinity.
    
    This strategy ensures commands from the same session go to the same
    worker when possible, providing session stickiness.
    """
    
    def __init__(self, 
                 session_worker_map: Optional[Dict[str, str]] = None,
                 max_load_threshold: float = 0.8):
        """
        Initialize session affinity strategy.
        
        Args:
            session_worker_map: Optional pre-existing session -> worker mapping
            max_load_threshold: Maximum load before considering other workers
        """
        self.session_worker_map = session_worker_map or {}
        self.max_load_threshold = max_load_threshold
    
    def select_worker(self, 
                     workers: List[Dict[str, Any]], 
                     context: RoutingContext) -> Optional[Dict[str, Any]]:
        """Select worker with session affinity"""
        if not workers:
            return None
        
        session_id = context.session_id
        if not session_id:
            # No session ID, fall back to least loaded
            return self._select_least_loaded(workers, "No session ID provided")
        
        # Check if we have an existing mapping
        if session_id in self.session_worker_map:
            target_worker_id = self.session_worker_map[session_id]
            
            # Look for the mapped worker
            for worker in workers:
                if worker.get('worker_id') == target_worker_id:
                    # Check if worker is still suitable
                    current_load = worker.get('current_load', 1.0)
                    if current_load <= self.max_load_threshold:
                        selected = worker.copy()
                        selected['selection_reason'] = f"Session affinity for {session_id}"
                        selected['session_affinity'] = True
                        selected['session_id'] = session_id
                        return selected
                    else:
                        # Worker overloaded, remove mapping and find new worker
                        del self.session_worker_map[session_id]
                        break
        
        # No existing mapping or mapped worker unavailable
        # Select least loaded worker and create new mapping
        selected = self._select_least_loaded(workers, f"New session affinity for {session_id}")
        if selected:
            _swid = selected.get("worker_id")
            if _swid is not None:
                self.session_worker_map[session_id] = str(_swid)
            selected['session_affinity'] = True
            selected['session_id'] = session_id
            selected['new_session_mapping'] = True
        
        return selected
    
    def _select_least_loaded(self, workers: List[Dict[str, Any]], reason: str) -> Optional[Dict[str, Any]]:
        """Select least loaded worker"""
        if not workers:
            return None
        
        selected = min(workers, key=lambda w: w.get('current_load', 1.0))
        selected = selected.copy()
        selected['selection_reason'] = reason
        
        return selected
    
    def get_session_mapping(self, session_id: str) -> Optional[str]:
        """Get worker ID mapped to a session"""
        return self.session_worker_map.get(session_id)
    
    def remove_session_mapping(self, session_id: str):
        """Remove session mapping"""
        if session_id in self.session_worker_map:
            del self.session_worker_map[session_id]
    
    def get_all_session_mappings(self) -> Dict[str, str]:
        """Get all session mappings"""
        return self.session_worker_map.copy()
    
    def get_strategy_name(self) -> str:
        return "Session Affinity"


class AffinityBasedStrategy(RoutingStrategy):
    """
    Route commands based on various affinity rules.
    
    This strategy supports multiple types of affinity including user affinity,
    data affinity, and custom affinity rules.
    """
    
    def __init__(self, 
                 affinity_rules: Optional[Dict[str, Dict[str, str]]] = None,
                 affinity_strength: float = 0.8):
        """
        Initialize affinity-based strategy.
        
        Args:
            affinity_rules: Dict of affinity_type -> {key: worker_id} mappings
            affinity_strength: How strongly to prefer affinity matches (0.0-1.0)
        """
        self.affinity_rules = affinity_rules or {
            'user_id': {},
            'tenant_id': {},
            'data_location': {},
            'command_type': {}
        }
        self.affinity_strength = affinity_strength
    
    def select_worker(self, 
                     workers: List[Dict[str, Any]], 
                     context: RoutingContext) -> Optional[Dict[str, Any]]:
        """Select worker based on affinity rules"""
        if not workers:
            return None
        
        # Score workers based on affinity matches
        scored_workers = []
        
        for worker in workers:
            score = self._calculate_affinity_score(worker, context)
            scored_workers.append((score, worker))
        
        # Sort by score (descending)
        scored_workers.sort(key=lambda x: x[0], reverse=True)
        
        # Select highest scoring worker
        if scored_workers:
            best_score, selected_worker = scored_workers[0]
            selected = selected_worker.copy()
            selected['selection_reason'] = f"Affinity-based selection (score: {best_score:.2f})"
            selected['affinity_score'] = best_score
            
            # Update affinity rules based on selection
            self._update_affinity_rules(selected, context)
            
            return selected
        
        return None
    
    def _calculate_affinity_score(self, 
                                worker: Dict[str, Any], 
                                context: RoutingContext) -> float:
        """Calculate affinity score for a worker"""
        base_score = 1.0 - worker.get('current_load', 1.0)  # Load-based base score
        affinity_bonus = 0.0
        
        worker_id = worker.get('worker_id')
        
        # Check each affinity type
        for affinity_type, mappings in self.affinity_rules.items():
            context_value = getattr(context, affinity_type, None)
            if context_value and context_value in mappings:
                if mappings[context_value] == worker_id:
                    affinity_bonus += self.affinity_strength
        
        # Check tenant affinity
        if context.tenant_id:
            tenant_mappings = self.affinity_rules.get('tenant_id', {})
            if context.tenant_id in tenant_mappings and tenant_mappings[context.tenant_id] == worker_id:
                affinity_bonus += self.affinity_strength * 1.2  # Higher weight for tenant affinity
        
        # Check command type affinity
        command_type_mappings = self.affinity_rules.get('command_type', {})
        if context.command_type in command_type_mappings and command_type_mappings[context.command_type] == worker_id:
            affinity_bonus += self.affinity_strength * 0.5  # Lower weight for command type
        
        return base_score + affinity_bonus
    
    def _update_affinity_rules(self, worker: Dict[str, Any], context: RoutingContext):
        """Update affinity rules based on successful selection"""
        worker_id = worker.get('worker_id')
        
        # Update tenant affinity
        if context.tenant_id and worker_id is not None:
            self.affinity_rules['tenant_id'][context.tenant_id] = str(worker_id)
        
        # Update session affinity
        if context.session_id and worker_id is not None:
            if 'session_id' not in self.affinity_rules:
                self.affinity_rules['session_id'] = {}
            self.affinity_rules['session_id'][context.session_id] = str(worker_id)
    
    def add_affinity_rule(self, affinity_type: str, key: str, worker_id: str):
        """Add a new affinity rule"""
        if affinity_type not in self.affinity_rules:
            self.affinity_rules[affinity_type] = {}
        self.affinity_rules[affinity_type][key] = worker_id
    
    def remove_affinity_rule(self, affinity_type: str, key: str):
        """Remove an affinity rule"""
        if affinity_type in self.affinity_rules and key in self.affinity_rules[affinity_type]:
            del self.affinity_rules[affinity_type][key]
    
    def get_affinity_rules(self) -> Dict[str, Dict[str, str]]:
        """Get all affinity rules"""
        return self.affinity_rules.copy()
    
    def get_strategy_name(self) -> str:
        return "Affinity-Based"


class StickyWorkerStrategy(RoutingStrategy):
    """
    Route commands to workers with "sticky" behavior.
    
    Once a worker is selected for a particular context, it continues
    to be used until it becomes unavailable or overloaded.
    """
    
    def __init__(self, 
                 sticky_key: str = 'tenant_id',
                 max_load_threshold: float = 0.85,
                 sticky_duration_seconds: int = 3600):
        """
        Initialize sticky worker strategy.
        
        Args:
            sticky_key: Context attribute to use for stickiness
            max_load_threshold: Maximum load before releasing stickiness
            sticky_duration_seconds: How long to maintain stickiness
        """
        self.sticky_key = sticky_key
        self.max_load_threshold = max_load_threshold
        self.sticky_duration_seconds = sticky_duration_seconds
        
        # Track sticky assignments
        self.sticky_assignments = {}  # key -> (worker_id, timestamp)
    
    def select_worker(self, 
                     workers: List[Dict[str, Any]], 
                     context: RoutingContext) -> Optional[Dict[str, Any]]:
        """Select worker with sticky behavior"""
        if not workers:
            return None
        
        sticky_value = getattr(context, self.sticky_key, None)
        if not sticky_value:
            # No sticky key, fall back to least loaded
            return self._select_least_loaded(workers, f"No {self.sticky_key} for stickiness")
        
        # Check for existing sticky assignment
        if sticky_value in self.sticky_assignments:
            assigned_worker_id, assignment_time = self.sticky_assignments[sticky_value]
            
            # Check if assignment is still valid
            if time.time() - assignment_time < self.sticky_duration_seconds:
                # Look for the assigned worker
                for worker in workers:
                    if worker.get('worker_id') == assigned_worker_id:
                        # Check if worker is still suitable
                        current_load = worker.get('current_load', 1.0)
                        if current_load <= self.max_load_threshold:
                            selected = worker.copy()
                            selected['selection_reason'] = f"Sticky assignment for {self.sticky_key}={sticky_value}"
                            selected['sticky_assignment'] = True
                            selected['sticky_key'] = self.sticky_key
                            selected['sticky_value'] = sticky_value
                            return selected
                        else:
                            # Worker overloaded, remove sticky assignment
                            del self.sticky_assignments[sticky_value]
                            break
            else:
                # Assignment expired
                del self.sticky_assignments[sticky_value]
        
        # No valid sticky assignment, select new worker
        selected = self._select_least_loaded(workers, f"New sticky assignment for {self.sticky_key}={sticky_value}")
        if selected:
            # Create new sticky assignment
            self.sticky_assignments[sticky_value] = (selected.get('worker_id'), time.time())
            selected['sticky_assignment'] = True
            selected['sticky_key'] = self.sticky_key
            selected['sticky_value'] = sticky_value
            selected['new_sticky_assignment'] = True
        
        return selected
    
    def _select_least_loaded(self, workers: List[Dict[str, Any]], reason: str) -> Optional[Dict[str, Any]]:
        """Select least loaded worker"""
        if not workers:
            return None
        
        selected = min(workers, key=lambda w: w.get('current_load', 1.0))
        selected = selected.copy()
        selected['selection_reason'] = reason
        
        return selected
    
    def get_sticky_assignments(self) -> Dict[str, tuple]:
        """Get all sticky assignments"""
        return self.sticky_assignments.copy()
    
    def clear_sticky_assignment(self, key: str):
        """Clear a specific sticky assignment"""
        if key in self.sticky_assignments:
            del self.sticky_assignments[key]
    
    def clear_all_sticky_assignments(self):
        """Clear all sticky assignments"""
        self.sticky_assignments.clear()
    
    def get_strategy_name(self) -> str:
        return f"Sticky Worker ({self.sticky_key})"
