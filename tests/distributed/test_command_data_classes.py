"""
Tests for command data classes.

This module tests the specific command data classes that inherit from BaseCommandData,
as described in ADR-0014, including type safety and command-specific functionality.
"""

import pytest
from typing import List, Dict, Any

# Helper: compare message-like list (Message objects or dicts) to expected list of dicts with role/content
def _messages_eq(actual, expected_dict_list: List[Dict[str, str]]) -> bool:
    if actual is None and expected_dict_list is None:
        return True
    if actual is None or expected_dict_list is None:
        return False
    if len(actual) != len(expected_dict_list):
        return False
    for i, exp in enumerate(expected_dict_list):
        m = actual[i]
        role = m.role if hasattr(m, "role") else m.get("role")
        content = m.content if hasattr(m, "content") else m.get("content")
        if role != exp.get("role") or content != exp.get("content"):
            return False
    return True


def _serialized_messages_eq(serialized_list, expected_dict_list: List[Dict[str, str]]) -> bool:
    """Compare serialized message list (list of dicts from model_dump) to expected role/content dicts."""
    if serialized_list is None and expected_dict_list is None:
        return True
    if serialized_list is None or expected_dict_list is None or len(serialized_list) != len(expected_dict_list):
        return False
    for i, exp in enumerate(expected_dict_list):
        d = serialized_list[i]
        if d.get("role") != exp.get("role") or d.get("content") != exp.get("content"):
            return False
    return True


from motet.core.commands.command_data_classes import (
    ModelInferenceData,
    ToolExecutionData,
    AgentTurnData,
    WorkflowExecutionData,
    ToolDiscoveryData,
    ConversationAnalysisData,
    EmbeddingData,
    get_command_data_class,
    create_command_data,
    COMMAND_DATA_CLASSES
)


class TestModelInferenceData:
    """Test cases for ModelInferenceData."""
    
    def test_basic_initialization(self):
        """Test basic initialization."""
        messages = [{"role": "user", "content": "Hello"}]
        data = ModelInferenceData(messages=messages)
        
        assert _messages_eq(data.messages, messages)
        assert data.model_settings is None
        assert data.stream is False
        assert data.conversation_history is None  # Inherited from BaseCommandData
    
    def test_full_initialization(self):
        """Test full initialization with all parameters (temperature/max_tokens in model_settings)."""
        messages = [{"role": "user", "content": "Hello"}]
        model_config = {"model": "gpt-4o-mini", "provider": "openai", "temperature": 0.7, "max_tokens": 1000}
        conversation_history = [{"role": "assistant", "content": "Previous response"}]
        
        data = ModelInferenceData(
            messages=messages,
            model_settings=model_config,
            stream=True,
            conversation_history=conversation_history,
            metadata={"source": "test"}
        )
        
        assert _messages_eq(data.messages, messages)
        assert data.model_settings == model_config
        assert data.model_settings.get("temperature") == 0.7
        assert data.model_settings.get("max_tokens") == 1000
        assert data.stream is True
        assert _messages_eq(data.conversation_history, conversation_history)
        assert data.metadata == {"source": "test"}
    
    def test_serialization(self):
        """Test serialization and deserialization."""
        messages = [{"role": "user", "content": "Hello"}]
        data = ModelInferenceData(
            messages=messages,
            model_settings={"temperature": 0.7},
            stream=True,
            conversation_history=[{"role": "assistant", "content": "Hi"}]
        )
        
        # Test to_serializable_dict (now uses model_dump; messages/conversation_history are serialized)
        result = data.to_serializable_dict()
        assert _serialized_messages_eq(result["messages"], messages)
        assert result.get("model_settings", {}).get("temperature") == 0.7
        assert result["stream"] is True
        assert _serialized_messages_eq(result["conversation_history"], [{"role": "assistant", "content": "Hi"}])
        
        # Test from_serializable_dict (now uses model_validate)
        restored = ModelInferenceData.from_serializable_dict(result)
        assert _messages_eq(restored.messages, messages)
        assert restored.model_settings.get("temperature") == 0.7
        assert restored.stream is True
        assert _messages_eq(restored.conversation_history, [{"role": "assistant", "content": "Hi"}])


class TestToolExecutionData:
    """Test cases for ToolExecutionData."""
    
    def test_basic_initialization(self):
        """Test basic initialization."""
        data = ToolExecutionData(
            tool_name="test_tool",
            parameters={"param1": "value1", "param2": 42}
        )
        
        assert data.tool_name == "test_tool"
        assert data.parameters == {"param1": "value1", "param2": 42}
        assert data.conversation_history is None
        assert data.reasoning_task is None
    
    def test_with_context(self):
        """Test initialization with conversation and reasoning context."""
        conversation_history = [{"role": "user", "content": "Use the tool"}]
        reasoning_task = {"task": "analyze_parameters"}
        
        data = ToolExecutionData(
            tool_name="analysis_tool",
            parameters={"input": "data"},
            conversation_history=conversation_history,
            reasoning_task=reasoning_task,
            metadata={"priority": "high"}
        )
        
        assert data.tool_name == "analysis_tool"
        assert data.parameters == {"input": "data"}
        assert _messages_eq(data.conversation_history, conversation_history)
        assert data.reasoning_task == reasoning_task
        assert data.metadata == {"priority": "high"}
    
    def test_serialization(self):
        """Test serialization and deserialization."""
        data = ToolExecutionData(
            tool_name="test_tool",
            parameters={"param1": "value1"},
            conversation_history=[{"role": "user", "content": "Hello"}],
            reasoning_context={"strategy": "auto"}
        )
        
        # Test to_serializable_dict
        result = data.to_serializable_dict()
        assert result["tool_name"] == "test_tool"
        assert result["parameters"] == {"param1": "value1"}
        assert _serialized_messages_eq(result["conversation_history"], [{"role": "user", "content": "Hello"}])
        assert result["reasoning_context"] == {"strategy": "auto"}
        
        # Test from_serializable_dict
        restored = ToolExecutionData.from_serializable_dict(result)
        assert restored.tool_name == "test_tool"
        assert restored.parameters == {"param1": "value1"}
        assert _messages_eq(restored.conversation_history, [{"role": "user", "content": "Hello"}])
        assert restored.reasoning_context == {"strategy": "auto"}


class TestWorkflowExecutionData:
    """Test cases for WorkflowExecutionData."""
    
    def test_basic_initialization(self):
        """Test basic initialization."""
        workflow_steps = [
            {"step": 1, "action": "analyze"},
            {"step": 2, "action": "process"},
            {"step": 3, "action": "output"}
        ]
        
        data = WorkflowExecutionData(
            workflow_id="workflow_123",
            workflow_name="test_workflow",
            workflow_steps=workflow_steps
        )
        
        assert data.workflow_id == "workflow_123"
        assert data.workflow_name == "test_workflow"
        assert data.workflow_steps == workflow_steps
        assert data.max_parallel_steps == 3
        assert data.enable_parallel_execution is True
        assert data.retry_failed_steps is True
        assert data.max_step_retries == 2
    
    def test_custom_parameters(self):
        """Test initialization with custom parameters."""
        workflow_steps = [{"step": 1, "action": "test"}]
        
        data = WorkflowExecutionData(
            workflow_id="workflow_456",
            workflow_name="custom_workflow",
            workflow_steps=workflow_steps,
            max_parallel_steps=5,
            enable_parallel_execution=False,
            retry_failed_steps=False,
            max_step_retries=1,
            conversation_history=[{"role": "user", "content": "Run workflow"}],
            reasoning_task={"task": "workflow_analysis"}
        )
        
        assert data.workflow_id == "workflow_456"
        assert data.workflow_name == "custom_workflow"
        assert data.max_parallel_steps == 5
        assert data.enable_parallel_execution is False
        assert data.retry_failed_steps is False
        assert data.max_step_retries == 1
        assert _messages_eq(data.conversation_history, [{"role": "user", "content": "Run workflow"}])
        assert data.reasoning_task == {"task": "workflow_analysis"}
    
    def test_serialization(self):
        """Test serialization and deserialization."""
        workflow_steps = [{"step": 1, "action": "test"}]
        
        data = WorkflowExecutionData(
            workflow_id="workflow_789",
            workflow_name="serialization_test",
            workflow_steps=workflow_steps,
            max_parallel_steps=4,
            enable_parallel_execution=False,
            metadata={"test": "data"}
        )
        
        # Test to_serializable_dict
        result = data.to_serializable_dict()
        assert result["workflow_id"] == "workflow_789"
        assert result["workflow_name"] == "serialization_test"
        assert result["workflow_steps"] == workflow_steps
        assert result["max_parallel_steps"] == 4
        assert result["enable_parallel_execution"] is False
        assert result["metadata"] == {"test": "data"}
        
        # Test from_serializable_dict
        restored = WorkflowExecutionData.from_serializable_dict(result)
        assert restored.workflow_id == "workflow_789"
        assert restored.workflow_name == "serialization_test"
        assert restored.workflow_steps == workflow_steps
        assert restored.max_parallel_steps == 4
        assert restored.enable_parallel_execution is False
        assert restored.metadata == {"test": "data"}


class TestToolDiscoveryData:
    """Test cases for ToolDiscoveryData."""
    
    def test_basic_initialization(self):
        """Test basic initialization."""
        data = ToolDiscoveryData()
        
        assert data.discovery_type == "all"
        assert data.filter_criteria is None
        assert data.conversation_history is None
    
    def test_with_parameters(self):
        """Test initialization with parameters."""
        filter_criteria = {"category": "analysis", "available": True}
        
        data = ToolDiscoveryData(
            discovery_type="available",
            filter_criteria=filter_criteria,
            conversation_history=[{"role": "user", "content": "Find tools"}],
            metadata={"source": "user_request"}
        )
        
        assert data.discovery_type == "available"
        assert data.filter_criteria == filter_criteria
        assert _messages_eq(data.conversation_history, [{"role": "user", "content": "Find tools"}])
        assert data.metadata == {"source": "user_request"}
    
    def test_serialization(self):
        """Test serialization and deserialization."""
        data = ToolDiscoveryData(
            discovery_type="capabilities",
            filter_criteria={"type": "mcp"},
            conversation_history=[{"role": "user", "content": "Discover"}]
        )
        
        # Test to_serializable_dict
        result = data.to_serializable_dict()
        assert result["discovery_type"] == "capabilities"
        assert result["filter_criteria"] == {"type": "mcp"}
        assert _serialized_messages_eq(result["conversation_history"], [{"role": "user", "content": "Discover"}])
        
        # Test from_serializable_dict
        restored = ToolDiscoveryData.from_serializable_dict(result)
        assert restored.discovery_type == "capabilities"
        assert restored.filter_criteria == {"type": "mcp"}
        assert _messages_eq(restored.conversation_history, [{"role": "user", "content": "Discover"}])


class TestConversationAnalysisData:
    """Test cases for ConversationAnalysisData."""
    
    def test_basic_initialization(self):
        """Test basic initialization."""
        messages = [{"role": "user", "content": "Analyze this conversation"}]
        data = ConversationAnalysisData(messages=messages)
        
        assert _messages_eq(data.messages, messages)
        assert data.analysis_type == "complexity"
        assert data.conversation_history is None
    
    def test_with_analysis_type(self):
        """Test initialization with specific analysis type."""
        messages = [{"role": "user", "content": "What's the intent?"}]
        
        data = ConversationAnalysisData(
            analysis_type="intent",
            messages=messages,
            conversation_history=[{"role": "assistant", "content": "Previous message"}],
            metadata={"analysis_depth": "deep"}
        )
        
        assert data.analysis_type == "intent"
        assert _messages_eq(data.messages, messages)
        assert _messages_eq(data.conversation_history, [{"role": "assistant", "content": "Previous message"}])
        assert data.metadata == {"analysis_depth": "deep"}
    
    def test_serialization(self):
        """Test serialization and deserialization."""
        data = ConversationAnalysisData(
            analysis_type="sentiment",
            messages=[{"role": "user", "content": "How do I feel?"}],
            conversation_history=[{"role": "assistant", "content": "You seem happy"}]
        )
        
        # Test to_serializable_dict
        result = data.to_serializable_dict()
        assert result["analysis_type"] == "sentiment"
        assert _serialized_messages_eq(result["messages"], [{"role": "user", "content": "How do I feel?"}])
        assert _serialized_messages_eq(result["conversation_history"], [{"role": "assistant", "content": "You seem happy"}])
        
        # Test from_serializable_dict
        restored = ConversationAnalysisData.from_serializable_dict(result)
        assert restored.analysis_type == "sentiment"
        assert _messages_eq(restored.messages, [{"role": "user", "content": "How do I feel?"}])
        assert _messages_eq(restored.conversation_history, [{"role": "assistant", "content": "You seem happy"}])


class TestEmbeddingData:
    """Test cases for EmbeddingData."""
    
    def test_basic_initialization(self):
        """Test basic initialization."""
        data = EmbeddingData(texts=["Hello world"])
        
        assert data.texts == ["Hello world"]
        assert data.model is None
        assert data.conversation_history is None
    
    def test_with_model(self):
        """Test initialization with model."""
        data = EmbeddingData(
            texts=["Generate embedding for this text"],
            model="text-embedding-ada-002",
            conversation_history=[{"role": "user", "content": "Create embedding"}],
            metadata={"purpose": "similarity_search"}
        )
        
        assert data.texts == ["Generate embedding for this text"]
        assert data.model == "text-embedding-ada-002"
        assert _messages_eq(data.conversation_history, [{"role": "user", "content": "Create embedding"}])
        assert data.metadata == {"purpose": "similarity_search"}
    
    def test_serialization(self):
        """Test serialization and deserialization."""
        data = EmbeddingData(
            texts=["Test text for embedding"],
            model="custom-model",
            conversation_history=[{"role": "user", "content": "Embed this"}]
        )
        
        # Test to_serializable_dict
        result = data.to_serializable_dict()
        assert result["texts"] == ["Test text for embedding"]
        assert result["model"] == "custom-model"
        assert _serialized_messages_eq(result["conversation_history"], [{"role": "user", "content": "Embed this"}])
        
        # Test from_serializable_dict
        restored = EmbeddingData.from_serializable_dict(result)
        assert restored.texts == ["Test text for embedding"]
        assert restored.model == "custom-model"
        assert _messages_eq(restored.conversation_history, [{"role": "user", "content": "Embed this"}])


class TestCommandDataClassRegistry:
    """Test cases for command data class registry and factory functions."""
    
    def test_command_data_classes_registry(self):
        """Test that all command data classes are registered (under core.* keys)."""
        expected_classes = {
            "core.model_inference": ModelInferenceData,
            "core.tool_execution": ToolExecutionData,
            "core.agent_turn": AgentTurnData,
            "core.workflow_execution": WorkflowExecutionData,
            "core.tool_discovery": ToolDiscoveryData,
            "core.embedding_generation": EmbeddingData,
        }
        
        # Check that all expected classes are present (registry uses core.* keys)
        for command_type, expected_class in expected_classes.items():
            assert command_type in COMMAND_DATA_CLASSES
            assert COMMAND_DATA_CLASSES[command_type] == expected_class
        
        # core.conversation_analysis may be registered by decorator (conversation_analysis.data_classes)
        assert "core.conversation_analysis" in COMMAND_DATA_CLASSES
        assert COMMAND_DATA_CLASSES["core.conversation_analysis"].__name__ == "ConversationAnalysisData"
        
        # Check that we have additional classes beyond the original 7
        assert len(COMMAND_DATA_CLASSES) >= 7
    
    def test_get_command_data_class_valid(self):
        """Test getting valid command data classes (bare name resolves to core.*)."""
        assert get_command_data_class("model_inference") == ModelInferenceData
        assert get_command_data_class("tool_execution") == ToolExecutionData
        assert get_command_data_class("agent_turn") == AgentTurnData
        assert get_command_data_class("workflow_execution") == WorkflowExecutionData
        assert get_command_data_class("tool_discovery") == ToolDiscoveryData
        # conversation_analysis may be the decorator-registered class (different module)
        assert get_command_data_class("conversation_analysis") is not None
        assert get_command_data_class("conversation_analysis").__name__ == "ConversationAnalysisData"
        # embedding is registered as core.embedding_generation
        assert get_command_data_class("embedding_generation") == EmbeddingData
    
    def test_get_command_data_class_invalid(self):
        """Test getting invalid command data class."""
        # The function now returns None for invalid types instead of raising ValueError
        assert get_command_data_class("invalid_type") is None
    
    def test_create_command_data_valid(self):
        """Test creating valid command data instances."""
        # Test model inference data
        data = create_command_data(
            "model_inference",
            messages=[{"role": "user", "content": "Hello"}],
            model_settings={"temperature": 0.7}
        )
        assert isinstance(data, ModelInferenceData)
        assert _messages_eq(data.messages, [{"role": "user", "content": "Hello"}])
        assert data.model_settings.get("temperature") == 0.7
        
        # Test tool execution data
        data = create_command_data(
            "tool_execution",
            tool_name="test_tool",
            parameters={"param": "value"}
        )
        assert isinstance(data, ToolExecutionData)
        assert data.tool_name == "test_tool"
        assert data.parameters == {"param": "value"}
        
        # Test agent turn data
        data = create_command_data(
            "agent_turn",
            messages=[{"role": "user", "content": "Think"}],
        )
        assert isinstance(data, AgentTurnData)
        assert _messages_eq(data.messages, [{"role": "user", "content": "Think"}])
    
    def test_create_command_data_invalid(self):
        """Test creating invalid command data instance."""
        with pytest.raises(ValueError, match="Unsupported command type: invalid_type"):
            create_command_data("invalid_type", param="value")
    
    def test_create_command_data_with_inherited_fields(self):
        """Test creating command data with inherited fields."""
        data = create_command_data(
            "model_inference",
            messages=[{"role": "user", "content": "Hello"}],
            conversation_history=[{"role": "assistant", "content": "Hi"}],
            reasoning_context={"strategy": "auto"},
            metadata={"source": "test"}
        )
        
        assert isinstance(data, ModelInferenceData)
        assert _messages_eq(data.messages, [{"role": "user", "content": "Hello"}])
        assert _messages_eq(data.conversation_history, [{"role": "assistant", "content": "Hi"}])
        assert data.reasoning_context == {"strategy": "auto"}
        assert data.metadata == {"source": "test"}
