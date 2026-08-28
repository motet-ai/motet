"""
Motet - Base Command Data

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-27

Description:
    Base class for all command data payloads in the Motet distributed framework.
    Provides common fields and automatic Message deserialization for all command types.
    Includes conversation history management, reasoning context, and comprehensive
    metadata handling with automatic validation and serialization.

Dependencies:
    - pydantic: Data validation and model definitions
    - structlog: Structured logging
    - typing: Type hints and annotations
    - Message types and serialization

Usage:
    from motet.core.commands.base_command_data import (
        BaseCommandData, MessageFieldMixin
    )
    
    # Create base command data
    class MyCommandData(BaseCommandData):
        field: str = "value"
    
    # Create with conversation history
    data = MyCommandData(
        conversation_history=[Message(role="user", content="Hello")],
        metadata={"source": "api"}
    )

Notes:
    - Provides common fields for all command data types
    - Includes automatic Message deserialization and validation
    - Supports conversation history management and context extraction
    - Includes reasoning context and task management
    - Provides metadata and execution hints support
    - Integrates with command data registry and validation
    - Includes comprehensive context analysis and summarization
    - Message list fields are strict: a bare string is rejected with an actionable
      error rather than coerced into a user message. There is one canonical shape —
      [{"role": ..., "content": ...}] — and validate_command_data() teaches callers
      (including LLMs filling schedule payloads) to use it.
    - unknown_command_data_keys() is the single source of truth for keys a data class
      would silently drop (extra="allow" respected). Used by validate_command_data()
      to hard-reject at schedule creation and by
      DistributedCommand._deserialize_command_data() for warning-mode logging.
"""


from typing import Dict, Any, Optional, List, Union
from pydantic import BaseModel, Field, field_validator
import structlog

logger = structlog.get_logger(__name__)


def _deserialize_messages(messages_data: Optional[List[Union[Dict, Any]]]) -> Optional[List]:
    """
    Internal helper to convert message dictionaries to Message objects.
    
    Centralized in base_command_data to avoid circular imports with command_data_classes.

    Rejects bare strings outright: iterating a string would silently produce a list
    of single-character "messages". There is one canonical shape — a list of
    {role, content} items — and misuse fails with an actionable error.
    """
    if isinstance(messages_data, str):
        raise ValueError(
            'must be a list of {"role": ..., "content": ...} messages, not a string. '
            'For a single user message use [{"role": "user", "content": "..."}].'
        )
    if not messages_data:
        return messages_data  # Return None or empty list as-is
    
    # Import here to avoid circular imports
    from ...core.types import Message
    
    result = []
    for msg in messages_data:
        if isinstance(msg, dict):
            result.append(Message.model_validate(msg))
        else:
            result.append(msg)
    return result


def unknown_command_data_keys(data_class: type, payload: Any) -> List[str]:
    """
    Keys in ``payload`` that ``data_class`` would silently drop.

    Single source of truth for "does this payload fit the data class": pydantic
    ignores unknown keys by default, so a misnamed field validates as an empty
    one.

    Used by validate_command_data() (hard reject at schedule creation) and by
    DistributedCommand._deserialize_command_data() (warning-mode visibility for
    every other entry point).
    """
    if not isinstance(payload, dict):
        return []
    config = getattr(data_class, "model_config", None) or {}
    extra = config.get("extra") if isinstance(config, dict) else getattr(config, "extra", None)
    if extra == "allow":
        return []  # Extras are kept, not dropped — nothing is lost.
    model_fields = getattr(data_class, "model_fields", None) or {}
    return sorted(set(payload) - set(model_fields))


class BaseCommandData(BaseModel):
    """
    Base class for all command data payloads.
    
    Provides common fields and automatic Message deserialization for all command types.
    Subclasses only need to define their specific fields - Message conversion is automatic.
    """
    
    # Common context fields (auto-converted to Message objects via validators)
    conversation_history: Optional[List] = Field(
        default=None,
        description=(
            "Optional list of prior conversation messages for context. Items may be Message-like objects "
            "or dicts with {role, content}; they are automatically converted via validators."
        ),
    )
    reasoning_task: Optional[Any] = Field(
        default=None,
        description="Optional reasoning task payload (command-dependent structure).",
    )
    reasoning_context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional structured context for reasoning/execution (e.g., intent, constraints, trace metadata).",
    )
    
    # Common metadata
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional arbitrary metadata for the command execution (free-form key/value).",
    )
    
    # Common execution hints
    execution_hints: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional execution hints (free-form) used by orchestrators/strategies to influence behavior.",
    )
    
    @field_validator('conversation_history', mode='before')
    @classmethod
    def convert_conversation_history(cls, v):
        """Automatically convert message dicts to Message objects in conversation_history"""
        return _deserialize_messages(v)
    
    def get_conversation_context(self) -> str:
        """Extract conversation context as string for analysis."""
        if not self.conversation_history:
            return ""
        
        # Convert conversation history to readable string
        context_parts = []
        for msg in self.conversation_history[-5:]:  # Last 5 messages
            if hasattr(msg, 'content'):
                role = getattr(msg, 'role', 'unknown')
                content = msg.content[:200]  # Truncate long messages
                context_parts.append(f"{role}: {content}")
            elif isinstance(msg, dict):
                role = msg.get('role', 'unknown')
                content = str(msg.get('content', ''))[:200]
                context_parts.append(f"{role}: {content}")
        
        return "\n".join(context_parts)
    
    def get_reasoning_context_summary(self) -> Dict[str, Any]:
        """Extract key reasoning context information."""
        if not self.reasoning_context:
            return {}
        
        return {
            'intent': self.reasoning_context.get('intent'),
            'confidence': self.reasoning_context.get('confidence'),
            'strategy': self.reasoning_context.get('strategy'),
            'complexity': self.reasoning_context.get('complexity')
        }
    
    def has_conversation_context(self) -> bool:
        """Check if this command has conversation context."""
        return bool(self.conversation_history)
    
    def has_reasoning_context(self) -> bool:
        """Check if this command has reasoning context."""
        return bool(self.reasoning_context or self.reasoning_task)
    
    def to_serializable_dict(self) -> Dict[str, Any]:
        """Convert to dictionary suitable for Redis storage."""
        return self.model_dump()
    
    @classmethod
    def from_serializable_dict(cls, data: Dict[str, Any]) -> 'BaseCommandData':
        """Create instance from Redis-stored dictionary."""
        return cls.model_validate(data)
    
    def get_data_size_estimate(self) -> int:
        """
        Estimate the size of this data in bytes for serialization decisions.
        
        Returns:
            Estimated size in bytes
        """
        try:
            # Convert to dict and estimate size
            data_dict = self.to_serializable_dict()
            import json
            serialized = json.dumps(data_dict)
            return len(serialized.encode('utf-8'))
        except Exception as e:
            logger.warning("Failed to estimate data size", error=str(e))
            return 0
    
    def is_large_data(self, threshold_bytes: int = 5000) -> bool:
        """
        Check if this data is considered large for Redis storage.
        
        Args:
            threshold_bytes: Size threshold in bytes
            
        Returns:
            True if data is larger than threshold
        """
        return self.get_data_size_estimate() > threshold_bytes
    
    def get_context_summary(self) -> Dict[str, Any]:
        """
        Get a summary of the command context for debugging and monitoring.
        
        Returns:
            Dictionary with context summary
        """
        return {
            'has_conversation_context': self.has_conversation_context(),
            'has_reasoning_context': self.has_reasoning_context(),
            'conversation_message_count': len(self.conversation_history) if self.conversation_history else 0,
            'reasoning_context_summary': self.get_reasoning_context_summary(),
            'data_size_bytes': self.get_data_size_estimate(),
            'is_large_data': self.is_large_data(),
            'metadata_keys': list(self.metadata.keys()) if self.metadata else [],
            'execution_hints_keys': list(self.execution_hints.keys()) if self.execution_hints else []
        }
    
    
    def __str__(self) -> str:
        """String representation of the command data."""
        context_summary = self.get_context_summary()
        return f"{self.__class__.__name__}(size={context_summary['data_size_bytes']} bytes, " \
               f"conversation={context_summary['has_conversation_context']}, " \
               f"reasoning={context_summary['has_reasoning_context']})"
    
    def __repr__(self) -> str:
        """Detailed representation of the command data."""
        return f"{self.__class__.__name__}({self.to_serializable_dict()})"
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert this CommandData instance to a dictionary.
        
        Returns:
            Dict representation of the command data
        """
        return self.model_dump()


class MessageFieldMixin(BaseModel):
    """
    Mixin for command data classes that have a 'messages' field.
    
    Provides automatic Message deserialization without code duplication.
    Inherit from this + BaseCommandData for commands with messages.
    
    Usage:
        class MyCommandData(MessageFieldMixin, BaseCommandData):
            messages: List[Any] = Field(default_factory=list)
            # Validator automatically applied - no code needed!
    """
    
    @field_validator('messages', mode='before', check_fields=False)
    @classmethod
    def convert_messages(cls, v):
        """Automatically convert message dicts to Message objects in messages field"""
        return _deserialize_messages(v) or []  # Return empty list instead of None


