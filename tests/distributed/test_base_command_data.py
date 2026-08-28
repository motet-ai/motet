"""
Tests for BaseCommandData class.

This module tests the BaseCommandData class as described in ADR-0014,
including common functionality, validation, and serialization methods.
"""

import pytest
import json
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

from motet.core.commands.base_command_data import BaseCommandData


class TestBaseCommandData:
    """Test cases for BaseCommandData."""
    
    def test_basic_initialization(self):
        """Test basic initialization with default values."""
        data = BaseCommandData()
        
        assert data.conversation_history is None
        assert data.reasoning_task is None
        assert data.reasoning_context is None
        assert data.metadata is None
        assert data.execution_hints is None
    
    def test_initialization_with_values(self):
        """Test initialization with provided values."""
        conversation_history = [{"role": "user", "content": "Hello"}]
        reasoning_task = {"task": "analyze"}
        reasoning_context = {"strategy": "chain_of_thought"}
        metadata = {"source": "test"}
        execution_hints = {"priority": "high"}
        
        data = BaseCommandData(
            conversation_history=conversation_history,
            reasoning_task=reasoning_task,
            reasoning_context=reasoning_context,
            metadata=metadata,
            execution_hints=execution_hints
        )
        
        # conversation_history is auto-converted to Message objects
        assert len(data.conversation_history) == 1
        assert data.conversation_history[0].role == "user"
        assert data.conversation_history[0].content == "Hello"
        assert data.reasoning_task == reasoning_task
        assert data.reasoning_context == reasoning_context
        assert data.metadata == metadata
        assert data.execution_hints == execution_hints
    
    def test_get_conversation_context_empty(self):
        """Test conversation context extraction with empty history."""
        data = BaseCommandData()
        context = data.get_conversation_context()
        assert context == ""
    
    def test_get_conversation_context_with_messages(self):
        """Test conversation context extraction with messages."""
        messages = [
            {"role": "user", "content": "Hello, how are you?"},
            {"role": "assistant", "content": "I'm doing well, thank you!"},
            {"role": "user", "content": "Can you help me with something?"},
            {"role": "assistant", "content": "Of course, I'd be happy to help."},
            {"role": "user", "content": "What's the weather like?"},
            {"role": "assistant", "content": "I don't have access to real-time weather data."},
            {"role": "user", "content": "That's okay, thanks anyway."}
        ]
        
        data = BaseCommandData(conversation_history=messages)
        context = data.get_conversation_context()
        
        # Should only include last 5 messages
        lines = context.split('\n')
        assert len(lines) == 5
        
        # Check that the last 5 messages are included
        assert "That's okay, thanks anyway." in context
        assert "I don't have access to real-time weather data." in context
        assert "What's the weather like?" in context
        assert "Of course, I'd be happy to help." in context
        assert "Can you help me with something?" in context
        
        # First message should not be included
        assert "Hello, how are you?" not in context
    
    def test_get_conversation_context_with_message_objects(self):
        """Test conversation context extraction with message objects."""
        class MockMessage:
            def __init__(self, role, content):
                self.role = role
                self.content = content
        
        messages = [
            MockMessage("user", "Hello"),
            MockMessage("assistant", "Hi there!"),
            MockMessage("user", "How are you?")
        ]
        
        data = BaseCommandData(conversation_history=messages)
        context = data.get_conversation_context()
        
        lines = context.split('\n')
        assert len(lines) == 3
        assert "user: Hello" in context
        assert "assistant: Hi there!" in context
        assert "user: How are you?" in context
    
    def test_get_conversation_context_truncates_long_messages(self):
        """Test that long messages are truncated."""
        long_message = "x" * 300  # 300 characters
        messages = [{"role": "user", "content": long_message}]
        
        data = BaseCommandData(conversation_history=messages)
        context = data.get_conversation_context()
        
        # Should be truncated to 200 characters
        assert len(context.split(': ')[1]) == 200
        assert context.endswith('x' * 200)
    
    def test_get_reasoning_context_summary_empty(self):
        """Test reasoning context summary with empty context."""
        data = BaseCommandData()
        summary = data.get_reasoning_context_summary()
        assert summary == {}
    
    def test_get_reasoning_context_summary_with_data(self):
        """Test reasoning context summary with data."""
        reasoning_context = {
            "intent": "analyze_complexity",
            "confidence": 0.85,
            "strategy": "chain_of_thought",
            "complexity": "high",
            "extra_field": "ignored"
        }
        
        data = BaseCommandData(reasoning_context=reasoning_context)
        summary = data.get_reasoning_context_summary()
        
        expected = {
            "intent": "analyze_complexity",
            "confidence": 0.85,
            "strategy": "chain_of_thought",
            "complexity": "high"
        }
        assert summary == expected
    
    def test_has_conversation_context(self):
        """Test conversation context detection."""
        # No context
        data = BaseCommandData()
        assert not data.has_conversation_context()
        
        # Empty list
        data = BaseCommandData(conversation_history=[])
        assert not data.has_conversation_context()
        
        # With messages
        data = BaseCommandData(conversation_history=[{"role": "user", "content": "Hello"}])
        assert data.has_conversation_context()
    
    def test_has_reasoning_context(self):
        """Test reasoning context detection."""
        # No context
        data = BaseCommandData()
        assert not data.has_reasoning_context()
        
        # With reasoning_context
        data = BaseCommandData(reasoning_context={"strategy": "auto"})
        assert data.has_reasoning_context()
        
        # With reasoning_task
        data = BaseCommandData(reasoning_task={"task": "analyze"})
        assert data.has_reasoning_context()
        
        # With both
        data = BaseCommandData(
            reasoning_context={"strategy": "auto"},
            reasoning_task={"task": "analyze"}
        )
        assert data.has_reasoning_context()
    
    def test_to_serializable_dict(self):
        """Test conversion to serializable dictionary."""
        data = BaseCommandData(
            conversation_history=[{"role": "user", "content": "Hello"}],
            reasoning_context={"strategy": "auto"},
            metadata={"source": "test"},
            execution_hints={"priority": "high"}
        )
        
        result = data.to_serializable_dict()
        
        # conversation_history is serialized as Message model_dump() (may include extra keys)
        assert result["reasoning_task"] is None
        assert result["reasoning_context"] == {"strategy": "auto"}
        assert result["metadata"] == {"source": "test"}
        assert result["execution_hints"] == {"priority": "high"}
        assert len(result["conversation_history"]) == 1
        assert result["conversation_history"][0]["role"] == "user"
        assert result["conversation_history"][0]["content"] == "Hello"
    
    def test_from_serializable_dict(self):
        """Test creation from serializable dictionary."""
        data_dict = {
            "conversation_history": [{"role": "user", "content": "Hello"}],
            "reasoning_task": None,
            "reasoning_context": {"strategy": "auto"},
            "metadata": {"source": "test"},
            "execution_hints": {"priority": "high"}
        }
        
        data = BaseCommandData.from_serializable_dict(data_dict)
        
        # conversation_history is auto-converted to Message objects
        assert len(data.conversation_history) == 1
        assert data.conversation_history[0].role == "user"
        assert data.conversation_history[0].content == "Hello"
        assert data.reasoning_task is None
        assert data.reasoning_context == {"strategy": "auto"}
        assert data.metadata == {"source": "test"}
        assert data.execution_hints == {"priority": "high"}
    
    def test_get_data_size_estimate(self):
        """Test data size estimation."""
        # Small data
        data = BaseCommandData(metadata={"key": "value"})
        size = data.get_data_size_estimate()
        assert size > 0
        assert size < 1000  # Should be small
        
        # Large data (conversation_history dicts require "role" for Message validation)
        large_data = BaseCommandData(
            conversation_history=[{"role": "user", "content": "x" * 1000}] * 10,
            metadata={"large_field": "y" * 2000}
        )
        large_size = large_data.get_data_size_estimate()
        assert large_size > size
        assert large_size > 10000  # Should be large
    
    def test_is_large_data(self):
        """Test large data detection."""
        # Small data
        data = BaseCommandData(metadata={"key": "value"})
        assert not data.is_large_data()
        
        # Large data (conversation_history dicts require "role" for Message validation)
        large_data = BaseCommandData(
            conversation_history=[{"role": "user", "content": "x" * 1000}] * 10,
            metadata={"large_field": "y" * 2000}
        )
        assert large_data.is_large_data()
        
        # Test with custom threshold
        assert not large_data.is_large_data(threshold_bytes=50000)
        assert large_data.is_large_data(threshold_bytes=1000)
    
    def test_get_context_summary(self):
        """Test context summary generation."""
        data = BaseCommandData(
            conversation_history=[{"role": "user", "content": "Hello"}] * 3,
            reasoning_context={"strategy": "auto", "confidence": 0.8},
            metadata={"source": "test"},
            execution_hints={"priority": "high"}
        )
        
        summary = data.get_context_summary()
        
        assert summary["has_conversation_context"] is True
        assert summary["has_reasoning_context"] is True
        assert summary["conversation_message_count"] == 3
        assert summary["reasoning_context_summary"]["strategy"] == "auto"
        assert summary["reasoning_context_summary"]["confidence"] == 0.8
        assert summary["data_size_bytes"] > 0
        assert summary["metadata_keys"] == ["source"]
        assert summary["execution_hints_keys"] == ["priority"]
    
    def test_validate_data_valid(self):
        """Test data validation with valid data."""
        data = BaseCommandData(
            conversation_history=[{"role": "user", "content": "Hello"}],
            reasoning_context={"strategy": "auto"},
            metadata={"source": "test"},
            execution_hints={"priority": "high"}
        )
        
        # Pydantic validates automatically on creation; conversation_history is stored as Message objects
        assert len(data.conversation_history) == 1
        assert data.conversation_history[0].role == "user"
        assert data.conversation_history[0].content == "Hello"
        assert data.reasoning_context == {"strategy": "auto"}
        assert data.metadata == {"source": "test"}
        assert data.execution_hints == {"priority": "high"}
    
    def test_validate_data_invalid_conversation_history(self):
        """Test data validation with invalid conversation history."""
        from pydantic import ValidationError
        
        # Non-iterable type (e.g. int) raises: validator iterates and raises TypeError, or Pydantic raises ValidationError
        with pytest.raises((ValidationError, TypeError)) as exc_info:
            BaseCommandData(conversation_history=123)
        
        # Check that the error is related to the field or type
        error_str = str(exc_info.value)
        assert "conversation_history" in error_str or "list" in error_str.lower() or "iterable" in error_str.lower() or "int" in error_str
    
    def test_validate_data_invalid_reasoning_context(self):
        """Test data validation with invalid reasoning context."""
        from pydantic import ValidationError
        
        with pytest.raises(ValidationError) as exc_info:
            BaseCommandData(reasoning_context="not a dict")
        
        error = exc_info.value
        assert "reasoning_context" in str(error)
    
    def test_validate_data_invalid_metadata(self):
        """Test data validation with invalid metadata."""
        from pydantic import ValidationError
        
        with pytest.raises(ValidationError) as exc_info:
            BaseCommandData(metadata="not a dict")
        
        error = exc_info.value
        assert "metadata" in str(error)
    
    def test_validate_data_invalid_execution_hints(self):
        """Test data validation with invalid execution hints."""
        from pydantic import ValidationError
        
        with pytest.raises(ValidationError) as exc_info:
            BaseCommandData(execution_hints="not a dict")
        
        error = exc_info.value
        assert "execution_hints" in str(error)
    
    def test_validate_data_multiple_errors(self):
        """Test data validation with multiple errors."""
        from pydantic import ValidationError
        
        with pytest.raises(ValidationError) as exc_info:
            BaseCommandData(
                conversation_history="not a list",
                reasoning_context="not a dict",
                metadata="not a dict",
                execution_hints="not a dict"
            )
        
        error = exc_info.value
        # Pydantic may report multiple validation errors (order can vary)
        error_str = str(error)
        assert sum(1 for k in ("conversation_history", "reasoning_context", "metadata", "execution_hints") if k in error_str) >= 2
    
    def test_string_representation(self):
        """Test string representation."""
        data = BaseCommandData(
            conversation_history=[{"role": "user", "content": "Hello"}],
            reasoning_context={"strategy": "auto"}
        )
        
        str_repr = str(data)
        assert "BaseCommandData" in str_repr
        assert "conversation=True" in str_repr
        assert "reasoning=True" in str_repr
        assert "bytes" in str_repr
    
    def test_repr_representation(self):
        """Test detailed representation."""
        data = BaseCommandData(
            conversation_history=[{"role": "user", "content": "Hello"}],
            reasoning_context={"strategy": "auto"}
        )
        
        repr_str = repr(data)
        assert "BaseCommandData" in repr_str
        assert "conversation_history" in repr_str
        assert "reasoning_context" in repr_str


class TestBaseCommandDataInheritance:
    """Test cases for classes inheriting from BaseCommandData."""
    
    class _SampleCommandDataClass(BaseCommandData):
        """Sample command data for inheritance tests (name avoids pytest collecting as test class)."""
        test_field: str = "default"
        numeric_field: int = 0
    
    def test_inheritance_basic(self):
        """Test basic inheritance functionality."""
        data = self._SampleCommandDataClass(
            test_field="test_value",
            numeric_field=42,
            conversation_history=[{"role": "user", "content": "Hello"}]
        )
        
        # Test inherited fields (conversation_history stored as Message objects)
        assert len(data.conversation_history) == 1
        assert data.conversation_history[0].role == "user"
        assert data.conversation_history[0].content == "Hello"
        assert data.has_conversation_context()
        
        # Test own fields
        assert data.test_field == "test_value"
        assert data.numeric_field == 42
    
    def test_inheritance_serialization(self):
        """Test serialization with inherited class."""
        data = self._SampleCommandDataClass(
            test_field="test_value",
            numeric_field=42,
            metadata={"source": "test"}
        )
        
        # Test model_dump (Pydantic serialization)
        result = data.model_dump()
        expected = {
            "conversation_history": None,
            "reasoning_task": None,
            "reasoning_context": None,
            "metadata": {"source": "test"},
            "execution_hints": None,
            "test_field": "test_value",
            "numeric_field": 42
        }
        assert result == expected
        
        # Test model_validate (Pydantic deserialization)
        restored = self._SampleCommandDataClass.model_validate(result)
        assert restored.test_field == "test_value"
        assert restored.numeric_field == 42
        assert restored.metadata == {"source": "test"}
    
    def test_inheritance_validation(self):
        """Test validation with inherited class."""
        from pydantic import ValidationError
        
        # Valid data - should create successfully
        data = self._SampleCommandDataClass(
            test_field="valid",
            numeric_field=42,
            conversation_history=[{"role": "user", "content": "Hello"}]
        )
        assert data.test_field == "valid"
        assert data.numeric_field == 42
        
        # Invalid data (inherited validation) - non-iterable raises TypeError or ValidationError
        with pytest.raises((ValidationError, TypeError)):
            self._SampleCommandDataClass(
                test_field="valid",
                numeric_field=42,
                conversation_history=123  # int is not a valid list
            )
    
    def test_inheritance_context_summary(self):
        """Test context summary with inherited class."""
        data = self._SampleCommandDataClass(
            test_field="test_value",
            numeric_field=42,
            conversation_history=[{"role": "user", "content": "Hello"}],
            reasoning_context={"strategy": "auto"}
        )
        
        summary = data.get_context_summary()
        assert summary["has_conversation_context"] is True
        assert summary["has_reasoning_context"] is True
        assert summary["conversation_message_count"] == 1
        assert summary["data_size_bytes"] > 0