"""
Unit tests for parameter injection service (ADR-0046).

Tests cover:
- Parameter source classification
- ContextParam helper functionality
- Parameter injection for Pydantic tools
- Parameter injection for MCP tools
- Convention-based parameter recognition
- Schema filtering
"""

import pytest
from unittest.mock import Mock, MagicMock
from pydantic import BaseModel, Field

from motet.core.tools.parameter_sources import (
    ParameterSource,
    ContextParam,
    PARAMETER_CONVENTIONS
)
from motet.core.tools.parameter_injection import ParameterInjectionService
from motet.core.tools.registry import ToolRegistry, RegisteredTool
from motet.core.tools.schema_exporter import ToolSchemaExporter


class TestParameterSource:
    """Test ParameterSource enum."""
    
    def test_parameter_source_values(self):
        """Test that ParameterSource has all expected values."""
        assert ParameterSource.LLM_PROVIDED.value == "llm"
        assert ParameterSource.USER_CONTEXT.value == "user_context"
        assert ParameterSource.CREDENTIAL.value == "credential"
        assert ParameterSource.SYSTEM.value == "system"
        assert ParameterSource.CONVERSATION.value == "conversation"


class TestContextParam:
    """Test ContextParam helper function."""
    
    def test_context_param_basic(self):
        """Test basic ContextParam usage."""
        field = ContextParam(
            description="Test parameter",
            source=ParameterSource.USER_CONTEXT,
            context_key="test_key"
        )
        
        assert field.description == "Test parameter"
        assert field.json_schema_extra["x-imf-source"] == "user_context"
        assert field.json_schema_extra["x-imf-context-key"] == "test_key"
        # Context params are shown as tokens; implementation uses False (token replacement)
        assert field.json_schema_extra.get("x-imf-hide-from-llm") is False
    
    def test_context_param_llm_provided(self):
        """Test that LLM_PROVIDED parameters are not hidden."""
        field = ContextParam(
            description="LLM parameter",
            source=ParameterSource.LLM_PROVIDED
        )
        
        assert field.json_schema_extra["x-imf-hide-from-llm"] is False
    
    def test_context_param_with_default(self):
        """Test ContextParam with default value."""
        field = ContextParam(
            description="Optional parameter",
            source=ParameterSource.SYSTEM,
            context_key="tenant_id",
            default="default"
        )
        
        assert field.default == "default"


class TestSchemaFiltering:
    """Test schema filtering in ToolSchemaExporter."""
    
    def test_schema_filters_hidden_parameters(self):
        """Context params are shown as tokens (not hidden); only x-imf-hide-from-llm=true are filtered."""
        
        # Define tool with context parameter (shown as token per ADR-0046)
        class TestToolParams(BaseModel):
            query: str = Field(..., description="Search query")
            user_email: str = ContextParam(
                description="User email",
                source=ParameterSource.USER_CONTEXT,
                context_key="authenticated_user_email"
            )
        
        registry = ToolRegistry()
        exporter = ToolSchemaExporter(registry)
        schema = exporter._extract_json_schema(TestToolParams)
        
        # Both params present; context param shown with token default
        assert "query" in schema["properties"]
        assert "user_email" in schema["properties"]
        assert schema["properties"]["user_email"].get("default", "").startswith("__CTX_")
        assert "query" in schema["required"]
        # Tokenized params not required (injected at execution)
        assert "user_email" not in schema["required"]
    
    def test_schema_keeps_llm_parameters(self):
        """Test that LLM parameters are not filtered."""
        
        class TestToolParams(BaseModel):
            query: str = Field(..., description="Search query")
            limit: int = Field(default=10, description="Result limit")
        
        registry = ToolRegistry()
        exporter = ToolSchemaExporter(registry)
        schema = exporter._extract_json_schema(TestToolParams)
        
        assert "query" in schema["properties"]
        assert "limit" in schema["properties"]


class TestParameterInjection:
    """Test ParameterInjectionService."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.registry = ToolRegistry()
        self.injector = ParameterInjectionService(self.registry)
        
        # Create mock motet context
        self.motet = Mock()
        self.motet.principal_id = "user@example.com"
        self.motet.task_id = "task-123"
        self.motet.conversation_id = "conv-456"
        self.motet.tenant_id = "tenant-789"
        self.motet.command_id = "cmd-abc"
    
    def test_inject_user_context_parameters(self):
        """Test injection of user context parameters."""
        
        # Define tool with user context parameters
        class GmailParams(BaseModel):
            to: str = Field(..., description="Recipient")
            subject: str = Field(..., description="Subject")
            user_google_email: str = ContextParam(
                description="Sender email",
                source=ParameterSource.USER_CONTEXT,
                context_key="authenticated_user_email"
            )
        
        # Register tool
        tool_func = Mock()
        self.registry.register(
            name="gmail_send",
            func=tool_func,
            description="Send email",
            schema=GmailParams
        )
        
        # LLM provides partial parameters
        llm_params = {
            "to": "recipient@example.com",
            "subject": "Test"
        }
        
        # Inject parameters (API: principal_id, tenant_id, task_id, conversation_id)
        complete_params = self.injector.inject_parameters(
            tool_name="gmail_send",
            llm_parameters=llm_params,
            principal_id=self.motet.principal_id,
            tenant_id=self.motet.tenant_id,
            task_id=self.motet.task_id,
            conversation_id=self.motet.conversation_id,
        )
        
        # Verify injection
        assert complete_params["to"] == "recipient@example.com"
        assert complete_params["subject"] == "Test"
        assert complete_params["user_google_email"] == "user@example.com"
    
    def test_inject_system_context_parameters(self):
        """Test injection of system context parameters."""
        
        class SystemParams(BaseModel):
            query: str = Field(..., description="Query")
            task_id: str = ContextParam(
                description="Task ID",
                source=ParameterSource.SYSTEM,
                context_key="task_id"
            )
            conversation_id: str = ContextParam(
                description="Conversation ID",
                source=ParameterSource.SYSTEM,
                context_key="conversation_id"
            )
        
        tool_func = Mock()
        self.registry.register(
            name="test_tool",
            func=tool_func,
            description="Test tool",
            schema=SystemParams
        )
        
        llm_params = {"query": "test"}
        complete_params = self.injector.inject_parameters(
            tool_name="test_tool",
            llm_parameters=llm_params,
            principal_id=self.motet.principal_id,
            tenant_id=self.motet.tenant_id,
            task_id=self.motet.task_id,
            conversation_id=self.motet.conversation_id,
        )
        
        assert complete_params["query"] == "test"
        assert complete_params["task_id"] == "task-123"
        assert complete_params["conversation_id"] == "conv-456"
    
    def test_llm_cannot_override_injected_params(self):
        """Test that LLM-provided values don't override injected ones."""
        
        class SecureParams(BaseModel):
            query: str = Field(..., description="Query")
            user_email: str = ContextParam(
                description="User email",
                source=ParameterSource.USER_CONTEXT,
                context_key="authenticated_user_email"
            )
        
        tool_func = Mock()
        self.registry.register(
            name="secure_tool",
            func=tool_func,
            description="Secure tool",
            schema=SecureParams
        )
        
        # LLM tries to provide user_email (should be ignored)
        llm_params = {
            "query": "test",
            "user_email": "attacker@evil.com"  # Should be ignored
        }
        
        complete_params = self.injector.inject_parameters(
            tool_name="secure_tool",
            llm_parameters=llm_params,
            principal_id=self.motet.principal_id,
            tenant_id=self.motet.tenant_id,
            task_id=self.motet.task_id,
            conversation_id=self.motet.conversation_id,
        )
        
        # Injected value wins (principal_id from motet)
        assert complete_params["user_email"] == "user@example.com"
    
    def test_convention_based_parameter_recognition(self):
        """Test MCP tool convention-based parameter recognition."""
        
        # Verify conventions dict
        assert "user_google_email" in PARAMETER_CONVENTIONS
        assert "access_token" in PARAMETER_CONVENTIONS
        assert "task_id" in PARAMETER_CONVENTIONS
        
        source, context_key = PARAMETER_CONVENTIONS["user_google_email"]
        assert source == ParameterSource.USER_CONTEXT
        assert context_key == "authenticated_user_email"
    
    def test_inject_no_schema_returns_as_is(self):
        """Test that unknown tools return parameters as-is."""
        
        llm_params = {"param1": "value1"}
        
        complete_params = self.injector.inject_parameters(
            tool_name="unknown_tool",
            llm_parameters=llm_params,
            principal_id="user",
            tenant_id="tenant",
            task_id="task",
            conversation_id="conv",
        )
        
        assert complete_params == llm_params


class TestNativeFunctionCallingRemoved:
    """NFC removed after ADR-0074; injection remains on tool_execution (ADR-0046)."""

    def test_native_function_calling_service_not_importable(self):
        """Stranded NFC must not remain as an injection integration surface."""
        import importlib
        import sys

        module_name = "motet.core.tools.native_function_calling"
        sys.modules.pop(module_name, None)
        with pytest.raises((ImportError, ModuleNotFoundError)):
            importlib.import_module(module_name)

    def test_parameter_injection_service_still_available(self):
        """Parameter injection lives on ParameterInjectionService / tool_execution, not NFC."""
        registry = ToolRegistry()
        injector = ParameterInjectionService(registry)
        assert injector is not None
        assert hasattr(injector, "inject_parameters")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

