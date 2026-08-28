"""
Motet - Memory Scoping Strategies

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Memory scoping strategy pattern for Phase 2 implementation.
    Provides intelligent memory scoping based on access patterns and retention policies.

    Scoping strategies determine:
    - Which scope type to assign to a memory (GLOBAL, PRINCIPAL, CONVERSATION, etc.)
    - How to generate scope IDs for memory isolation
    - Retention policies (TTL, cleanup rules, promotion criteria)

    Motet/tenant/principal isolation is handled automatically via MemoryItem fields,
    not by scope types. Scope types define access patterns and retention, not boundaries.

Dependencies:
    - abc: Abstract base class pattern
    - typing: Type hints and annotations
    - MotetContext: Command execution context
    - MemoryScopeType: Scope type enumeration

Usage:
    from motet.core.memory.scoping import (
        ConversationScopedStrategy, PrincipalScopedStrategy
    )
    
    # Determine scope for a memory
    strategy = ConversationScopedStrategy()
    scope_type = strategy.determine_scope(motet, content, metadata)
    scope_id = strategy.generate_scope_id(motet)
    retention = strategy.get_retention_policy()
    
    # Apply to memory storage
    memory_item = MemoryItem(
        ...
        scope_type=scope_type,
        scope_id=scope_id,
        ...
    )

Notes:
    - All strategies are stateless and thread-safe
    - Strategies use MotetContext for scope determination
    - Retention policies are advisory; enforcement happens in MemoryManager
    - Scope types are orthogonal to motet/tenant isolation
    - See Phase 2 for detailed scoping architecture
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from ..types import MemoryScopeType


class MemoryScopingStrategy(ABC):
    """
    Abstract base class for memory scoping strategies (ADR-0027 Phase 2).
    
    Scoping strategies determine how memories are scoped based on their context,
    content, and intended use. Each strategy defines:
    - Scope type determination logic
    - Scope ID generation
    - Retention and cleanup policies
    
    All strategies are stateless and can be safely reused across multiple invocations.
    """
    
    @abstractmethod
    def determine_scope(
        self,
        motet: Any,  # MotetContext - using Any to avoid circular import
        content: str,
        metadata: Dict[str, Any]
    ) -> MemoryScopeType:
        """
        Determine the appropriate scope type for this memory.
        
        Args:
            motet: MotetContext providing command execution context
            content: Memory content string
            metadata: Additional metadata that may influence scoping
            
        Returns:
            MemoryScopeType: The determined scope type
        """
        pass
    
    @abstractmethod
    def generate_scope_id(self, motet: Any) -> str:
        """
        Generate a unique scope ID for memory isolation.
        
        Scope IDs are used to group related memories within the same scope type.
        For example, all CONVERSATION-scoped memories in the same conversation
        share the same scope_id (the conversation_id).
        
        Args:
            motet: MotetContext providing command execution context
            
        Returns:
            str: Unique scope ID
        """
        pass
    
    @abstractmethod
    def get_retention_policy(self) -> Dict[str, Any]:
        """
        Define retention and cleanup policies for this scope.
        
        Returns:
            Dict containing:
            - ttl_seconds: Optional[int] - Time-to-live in seconds (None = permanent)
            - cleanup_trigger: Optional[str] - When to clean up ("conversation_end", "task_complete", etc.)
            - auto_promote: bool - Whether to auto-promote to higher scope
            - replication_factor: Optional[int] - How many workers should replicate
            - requires_validation: bool - Whether validation is needed for promotion
        """
        pass


class GlobalScopedStrategy(MemoryScopingStrategy):
    """
    Strategy for tenant-wide shared knowledge (within current motet).
    
    Use for:
    - Company policies and procedures
    - Shared team knowledge
    - System configuration relevant to all users
    - Common reference data
    
    Retention: Permanent unless explicitly deleted
    Access: All users/conversations in tenant+motet
    """
    
    def determine_scope(
        self,
        motet: Any,
        content: str,
        metadata: Dict[str, Any]
    ) -> MemoryScopeType:
        return MemoryScopeType.GLOBAL
    
    def generate_scope_id(self, motet: Any) -> str:
        """Global memories use tenant_id as scope_id"""
        return f"global-{motet.tenant_id}"
    
    def get_retention_policy(self) -> Dict[str, Any]:
        return {
            "ttl_seconds": None,  # Permanent
            "cleanup_trigger": None,
            "auto_promote": False,
            "replication_factor": None,  # Use default
            "requires_validation": False
        }


class ConversationScopedStrategy(MemoryScopingStrategy):
    """
    Strategy for conversation-specific ephemeral context.
    
    Use for:
    - Current conversation topic
    - Temporary context ("user is asking about project Alpha")
    - In-conversation preferences
    - Ephemeral working state
    
    Retention: Cleaned up after conversation ends (TTL: 24 hours)
    Access: Only within specific conversation
    """
    
    def determine_scope(
        self,
        motet: Any,
        content: str,
        metadata: Dict[str, Any]
    ) -> MemoryScopeType:
        return MemoryScopeType.CONVERSATION
    
    def generate_scope_id(self, motet: Any) -> str:
        """Conversation memories use conversation_id as scope_id"""
        if not motet.conversation_id:
            raise ValueError("ConversationScopedStrategy requires conversation_id in MotetContext")
        return motet.conversation_id
    
    def get_retention_policy(self) -> Dict[str, Any]:
        return {
            "ttl_seconds": 86400,  # 24 hours
            "cleanup_trigger": "conversation_end",
            "auto_promote": True,  # Can be promoted to PRINCIPAL if important
            "replication_factor": 1,  # Single worker (use affinity routing)
            "requires_validation": False
        }


class TaskScopedStrategy(MemoryScopingStrategy):
    """
    Strategy for task-specific ephemeral memories.
    
    Use for:
    - Workflow step state
    - Batch processing progress
    - Temporary computation results
    - Task-specific context
    
    Retention: Cleaned up after task completion (TTL: 1 hour)
    Access: Only within specific task
    """
    
    def determine_scope(
        self,
        motet: Any,
        content: str,
        metadata: Dict[str, Any]
    ) -> MemoryScopeType:
        return MemoryScopeType.TASK
    
    def generate_scope_id(self, motet: Any) -> str:
        """Task memories use task_id as scope_id"""
        if not motet.task_id:
            raise ValueError("TaskScopedStrategy requires task_id in MotetContext")
        return motet.task_id
    
    def get_retention_policy(self) -> Dict[str, Any]:
        return {
            "ttl_seconds": 3600,  # 1 hour
            "cleanup_trigger": "task_complete",
            "auto_promote": False,  # Tasks are ephemeral
            "replication_factor": 1,
            "requires_validation": False
        }


class PrincipalScopedStrategy(MemoryScopingStrategy):
    """
    Strategy for user-specific memories that follow the user.
    
    Use for:
    - User preferences ("prefers concise responses")
    - User profile information
    - Personal context
    - User-specific learnings
    
    Retention: Permanent unless user deletes (TTL: 30 days inactive)
    Access: Follows user across conversations
    """
    
    def determine_scope(
        self,
        motet: Any,
        content: str,
        metadata: Dict[str, Any]
    ) -> MemoryScopeType:
        return MemoryScopeType.PRINCIPAL
    
    def generate_scope_id(self, motet: Any) -> str:
        """Principal memories use principal_id as scope_id"""
        if not motet.principal_id:
            raise ValueError("PrincipalScopedStrategy requires principal_id in MotetContext")
        return motet.principal_id
    
    def get_retention_policy(self) -> Dict[str, Any]:
        return {
            "ttl_seconds": 2592000,  # 30 days (if inactive)
            "cleanup_trigger": None,
            "auto_promote": False,  # Principal memories stay principal
            "replication_factor": 2,  # Replicate for availability
            "requires_validation": False
        }


class CollectiveScopedStrategy(MemoryScopingStrategy):
    """
    Strategy for cross-worker validated insights.
    
    Use for:
    - Best practices discovered through consensus
    - Common patterns validated by multiple workers
    - Shared learnings promoted from individual memories
    - System-wide optimizations
    
    Retention: Permanent unless consensus changes (no TTL)
    Access: All workers, requires validation
    """
    
    def determine_scope(
        self,
        motet: Any,
        content: str,
        metadata: Dict[str, Any]
    ) -> MemoryScopeType:
        return MemoryScopeType.COLLECTIVE
    
    def generate_scope_id(self, motet: Any) -> str:
        """Collective memories use tenant_id + "collective" as scope_id"""
        return f"collective-{motet.tenant_id}"
    
    def get_retention_policy(self) -> Dict[str, Any]:
        return {
            "ttl_seconds": None,  # Permanent unless consensus changes
            "cleanup_trigger": None,
            "auto_promote": False,  # Already at highest level
            "replication_factor": 3,  # Replicate across multiple workers
            "requires_validation": True,  # Requires cross-worker consensus
            "min_validation_count": 3,  # At least 3 workers must agree
            "consensus_threshold": 0.67  # 67% agreement required
        }


class BackgroundThinkingScopedStrategy(MemoryScopingStrategy):
    """
    Strategy for autonomous thinking from background processes.
    
    Use for:
    - Post-conversation reflections
    - Pattern detection insights
    - Autonomous analysis results
    - Scheduled consolidation outputs
    
    Retention: 7 days, can be promoted to COLLECTIVE if validated
    Access: Available for review and promotion
    """
    
    def determine_scope(
        self,
        motet: Any,
        content: str,
        metadata: Dict[str, Any]
    ) -> MemoryScopeType:
        return MemoryScopeType.BACKGROUND
    
    def generate_scope_id(self, motet: Any) -> str:
        """Background memories use task_id or schedule_id as scope_id"""
        # Try task_id first (for scheduled tasks)
        if motet.task_id:
            return f"background-{motet.task_id}"
        # Fallback to timestamp-based ID
        from datetime import datetime
        return f"background-{motet.tenant_id}-{datetime.utcnow().strftime('%Y%m%d%H')}"
    
    def get_retention_policy(self) -> Dict[str, Any]:
        return {
            "ttl_seconds": 604800,  # 7 days
            "cleanup_trigger": None,
            "auto_promote": True,  # Can be promoted to COLLECTIVE
            "replication_factor": 2,
            "requires_validation": True,  # Validate before promotion
            "min_validation_count": 2  # 2 workers should validate
        }


# Strategy registry for easy lookup
SCOPE_STRATEGIES = {
    MemoryScopeType.GLOBAL: GlobalScopedStrategy,
    MemoryScopeType.CONVERSATION: ConversationScopedStrategy,
    MemoryScopeType.TASK: TaskScopedStrategy,
    MemoryScopeType.PRINCIPAL: PrincipalScopedStrategy,
    MemoryScopeType.COLLECTIVE: CollectiveScopedStrategy,
    MemoryScopeType.BACKGROUND: BackgroundThinkingScopedStrategy,
}


def get_strategy_for_scope(scope_type: MemoryScopeType) -> MemoryScopingStrategy:
    """
    Get the appropriate strategy instance for a given scope type.
    
    Args:
        scope_type: The memory scope type
        
    Returns:
        MemoryScopingStrategy: Strategy instance for the scope
        
    Raises:
        KeyError: If scope_type is not recognized
    """
    strategy_class = SCOPE_STRATEGIES.get(scope_type)
    if not strategy_class:
        raise KeyError(f"No strategy registered for scope type: {scope_type}")
    return strategy_class()


__all__ = [
    "MemoryScopingStrategy",
    "GlobalScopedStrategy",
    "ConversationScopedStrategy",
    "TaskScopedStrategy",
    "PrincipalScopedStrategy",
    "CollectiveScopedStrategy",
    "BackgroundThinkingScopedStrategy",
    "SCOPE_STRATEGIES",
    "get_strategy_for_scope",
]

