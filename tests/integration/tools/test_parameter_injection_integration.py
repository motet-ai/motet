"""
Integration tests for parameter injection (ADR-0046).

Tests end-to-end parameter injection flow:
1. Tool registration with ContextParam
2. Schema filtering via ToolSchemaExporter
3. Parameter injection via ParameterInjectionService

NativeFunctionCallingService was removed after ADR-0074 (#112); injection
remains on ParameterInjectionService / tool_execution only.
"""

import pytest
from unittest.mock import Mock
from pydantic import BaseModel, Field

from motet.core.tools.parameter_sources import ParameterSource, ContextParam
from motet.core.tools.parameter_injection import ParameterInjectionService
from motet.core.tools.registry import ToolRegistry
from motet.core.tools.schema_exporter import ToolSchemaExporter


# Example Tool Schemas

class GmailSendParams(BaseModel):
    """Gmail send with context injection."""
    to: str = Field(..., description="Recipient email")
    subject: str = Field(..., description="Email subject")
    body: str = Field(..., description="Email body")
    user_google_email: str = ContextParam(
        description="Sender email",
        source=ParameterSource.USER_CONTEXT,
        context_key="authenticated_user_email"
    )
    access_token: str = ContextParam(
        description="OAuth token",
        source=ParameterSource.CREDENTIAL,
        context_key="google_access_token"
    )


class DatabaseQueryParams(BaseModel):
    """Database query with multi-tenant isolation."""
    query: str = Field(..., description="SQL query")
    limit: int = Field(default=100, description="Row limit")
    tenant_id: str = ContextParam(
        description="Tenant ID",
        source=ParameterSource.SYSTEM,
        context_key="tenant_id"
    )


@pytest.fixture
def tool_registry():
    """Create tool registry with example tools."""
    registry = ToolRegistry()
    
    # Register Gmail tool
    def gmail_send(**kwargs):
        return f"Email sent from {kwargs['user_google_email']}"
    
    registry.register(
        name="gmail_send",
        func=gmail_send,
        description="Send email via Gmail",
        schema=GmailSendParams
    )
    
    # Register database tool
    def db_query(**kwargs):
        return f"Query results for tenant {kwargs['tenant_id']}"
    
    registry.register(
        name="db_query",
        func=db_query,
        description="Query database",
        schema=DatabaseQueryParams
    )
    
    return registry


@pytest.fixture
def mock_motet():
    """Create mock MotetContext."""
    motet = Mock()
    motet.principal_id = "alice@example.com"
    motet.task_id = "task-123"
    motet.conversation_id = "conv-456"
    motet.tenant_id = "tenant-789"
    motet.command_id = "cmd-abc"
    motet.trace_id = "trace-xyz"
    return motet


class TestEndToEndParameterInjection:
    """Test complete parameter injection flow."""
    
    def test_schema_filtering_hides_context_params(self, tool_registry):
        """Context params are shown as tokens in schema (ADR-0046); required excludes injected params."""
        exporter = ToolSchemaExporter(tool_registry)
        
        tools = exporter.export_canonical()
        gmail_tool = next(t for t in tools if t.name == "gmail_send")
        
        params = gmail_tool.json_schema["properties"]
        assert "to" in params
        assert "subject" in params
        assert "body" in params
        # Context params shown as tokens (default __CTX_...), not hidden
        assert "user_google_email" in params
        assert "access_token" in params
        assert params["user_google_email"].get("default", "").startswith("__CTX_")
        
        required = gmail_tool.json_schema.get("required", [])
        assert "to" in required
        assert "subject" in required
        assert "body" in required
        assert "user_google_email" not in required
        assert "access_token" not in required
    
    def test_parameter_injection_enriches_llm_params(self, tool_registry, mock_motet):
        """Test that ParameterInjectionService injects missing parameters."""
        injector = ParameterInjectionService(tool_registry)
        
        # LLM provides partial parameters
        llm_params = {
            "to": "recipient@example.com",
            "subject": "Test Email",
            "body": "Hello World"
        }
        
        # Inject context parameters (API: principal_id, tenant_id, task_id, conversation_id)
        complete_params = injector.inject_parameters(
            tool_name="gmail_send",
            llm_parameters=llm_params,
            principal_id=mock_motet.principal_id,
            tenant_id=mock_motet.tenant_id,
            task_id=mock_motet.task_id,
            conversation_id=mock_motet.conversation_id,
        )
        
        # Verify LLM parameters preserved
        assert complete_params["to"] == "recipient@example.com"
        assert complete_params["subject"] == "Test Email"
        assert complete_params["body"] == "Hello World"
        
        # Verify injected parameters
        assert complete_params["user_google_email"] == "alice@example.com"
        # Note: access_token injection not implemented yet (Phase 4)
    
    def test_multi_source_parameter_injection(self, tool_registry, mock_motet):
        """Test injection from multiple sources (user, system)."""
        injector = ParameterInjectionService(tool_registry)
        
        llm_params = {"query": "SELECT * FROM users"}
        
        complete_params = injector.inject_parameters(
            tool_name="db_query",
            llm_parameters=llm_params,
            principal_id=mock_motet.principal_id,
            tenant_id=mock_motet.tenant_id,
            task_id=mock_motet.task_id,
            conversation_id=mock_motet.conversation_id,
        )
        
        assert complete_params["query"] == "SELECT * FROM users"
        assert complete_params["tenant_id"] == "tenant-789"  # System context


class TestSecurityAndPrivacy:
    """Test security and privacy features."""
    
    def test_credentials_never_in_llm_schema(self, tool_registry):
        """Credential params are shown as tokens (__CTX_...), not raw values (ADR-0046)."""
        exporter = ToolSchemaExporter(tool_registry)
        tools = exporter.export_canonical()
        
        for tool in tools:
            params = tool.json_schema["properties"]
            for param_name, prop in params.items():
                # Credential-like params must have token default, not raw value
                if "token" in param_name.lower() or "password" in param_name.lower() or "secret" in param_name.lower():
                    default = prop.get("default", "")
                    assert isinstance(default, str) and default.startswith("__CTX_"), \
                        f"Credential param '{param_name}' must use token default, got {default!r}"
    
    def test_user_pii_minimization(self, tool_registry):
        """User PII params are shown as tokens (__CTX_...), not raw values (ADR-0046)."""
        exporter = ToolSchemaExporter(tool_registry)
        tools = exporter.export_canonical()
        
        gmail_tool = next(t for t in tools if t.name == "gmail_send")
        params = gmail_tool.json_schema["properties"]
        
        # user_google_email present as token placeholder, not raw email
        assert "user_google_email" in params
        assert params["user_google_email"].get("default", "").startswith("__CTX_")
    
    def test_injected_params_cannot_be_overridden_by_llm(self, tool_registry, mock_motet):
        """Test that LLM cannot override system-injected parameters."""
        injector = ParameterInjectionService(tool_registry)
        
        # LLM tries to provide tenant_id (should be system-injected)
        llm_params = {
            "query": "SELECT * FROM users",
            "tenant_id": "evil-tenant-999"  # Attack: trying to access other tenant's data
        }
        
        complete_params = injector.inject_parameters(
            tool_name="db_query",
            llm_parameters=llm_params,
            principal_id=mock_motet.principal_id,
            tenant_id=mock_motet.tenant_id,
            task_id=mock_motet.task_id,
            conversation_id=mock_motet.conversation_id,
        )
        
        # Injected value wins (security: LLM cannot override context params)
        assert complete_params["tenant_id"] == "tenant-789"


class TestMCPToolIntegration:
    """Test MCP tool integration with convention-based recognition."""
    
    def test_mcp_tool_parameter_conventions(self):
        """Test that MCP tool parameters are recognized by convention."""
        from motet.core.tools.parameter_sources import PARAMETER_CONVENTIONS
        
        # Common MCP tool parameters should be in conventions
        assert "user_google_email" in PARAMETER_CONVENTIONS
        assert "access_token" in PARAMETER_CONVENTIONS
        assert "task_id" in PARAMETER_CONVENTIONS
        assert "conversation_id" in PARAMETER_CONVENTIONS
        
        # Verify mappings
        source, context_key = PARAMETER_CONVENTIONS["user_google_email"]
        assert source == ParameterSource.USER_CONTEXT
        assert context_key == "authenticated_user_email"


class TestErrorHandling:
    """Test error handling in parameter injection."""
    
    def test_unknown_tool_returns_params_as_is(self, mock_motet):
        """Test that unknown tools don't break parameter injection."""
        registry = ToolRegistry()
        injector = ParameterInjectionService(registry)
        
        llm_params = {"param1": "value1"}
        complete_params = injector.inject_parameters(
            tool_name="unknown_tool",
            llm_parameters=llm_params,
            principal_id=mock_motet.principal_id,
            tenant_id=mock_motet.tenant_id,
            task_id=mock_motet.task_id,
            conversation_id=mock_motet.conversation_id,
        )
        
        assert complete_params == llm_params
    
    def test_missing_motet_attribute_doesnt_crash(self, tool_registry):
        """Missing context values (e.g. principal_id None) are handled gracefully."""
        injector = ParameterInjectionService(tool_registry)
        
        llm_params = {"query": "SELECT * FROM users"}
        
        complete_params = injector.inject_parameters(
            tool_name="db_query",
            llm_parameters=llm_params,
            principal_id=None,
            tenant_id="tenant-123",
            task_id="task-456",
            conversation_id="conv-789",
        )
        
        assert complete_params["tenant_id"] == "tenant-123"
        assert complete_params["query"] == "SELECT * FROM users"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

