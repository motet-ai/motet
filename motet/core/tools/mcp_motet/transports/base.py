"""
Motet - MCP Transport Base

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Abstract base class for MCP transports in the Motet distributed framework.
    Defines the interface that all MCP transport implementations must follow.
    Transports act as bridges between Redis Motet Streams and MCP servers,
    translating protocols while maintaining a consistent interface.

Dependencies:
    - abc: Abstract base class and method definitions
    - pydantic: Data validation and model definitions
    - typing: Type hints and annotations
    - structlog: Structured logging and observability

Usage:
    from motet.core.tools.mcp_motet.transports.base import MCPTransport, MCPToolDefinition

    # Create custom transport
    class MyTransport(MCPTransport):
        async def start(self) -> None:
            # Implementation
            pass

        async def stop(self) -> None:
            # Implementation
            pass

        async def list_tools(self) -> List[MCPToolDefinition]:
            # Implementation
            pass

Notes:
    - Provides abstract base class for MCP transport implementations
    - Defines consistent interface for all transport types
    - Includes tool definition and execution interfaces
    - Supports protocol translation between Motet Streams and MCP servers
    - Includes comprehensive error handling and logging
    - Integrates with MCP transport system
    - Includes comprehensive observability and monitoring
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field, ConfigDict
import structlog

logger = structlog.get_logger(__name__)


# --- Optional MCP resources/prompts (ADR-0076 Scope 3 Phase A) ---


class MCPResourceDefinition(BaseModel):
    """
    Resource definition from MCP server (resources/list).
    Per MCP spec: uri, name, title, description, mimeType, optional icons.
    """
    model_config = ConfigDict(populate_by_name=True)
    uri: str = Field(..., description="Resource URI")
    name: str = Field(..., description="Resource name")
    title: Optional[str] = Field(None, description="Display title")
    description: Optional[str] = Field(None, description="Resource description")
    mime_type: Optional[str] = Field(None, description="MIME type", alias="mimeType")
    icons: Optional[List[Dict[str, Any]]] = Field(None, description="Optional icons")


class MCPResourceContent(BaseModel):
    """Single content item from resources/read."""
    model_config = ConfigDict(populate_by_name=True)
    uri: str = Field(..., description="Resource URI")
    mime_type: Optional[str] = Field(None, alias="mimeType")
    text: Optional[str] = Field(None, description="Text content")
    blob: Optional[str] = Field(None, description="Base64-encoded binary content")


class MCPPromptDefinition(BaseModel):
    """
    Prompt definition from MCP server (prompts/list).
    Per MCP spec: name, title, description, arguments.
    """
    model_config = ConfigDict(populate_by_name=True)
    name: str = Field(..., description="Prompt name")
    title: Optional[str] = Field(None, description="Display title")
    description: Optional[str] = Field(None, description="Prompt description")
    arguments: Optional[List[Dict[str, Any]]] = Field(None, description="Argument schema")


class MCPPromptMessage(BaseModel):
    """Message in prompts/get result."""
    role: str = Field(..., description="user or assistant")
    content: Dict[str, Any] = Field(..., description="Content (e.g. type + text)")


class MCPPromptResult(BaseModel):
    """Result of prompts/get."""
    description: Optional[str] = Field(None, description="Prompt description")
    messages: List[MCPPromptMessage] = Field(default_factory=list, description="Prompt messages")


class MCPToolDefinition(BaseModel):
    """
    Tool definition from MCP server.
    
    This is a standardized representation of an MCP tool schema,
    agnostic to the transport type.
    """
    model_config = ConfigDict(populate_by_name=True)
    
    name: str = Field(..., description="Name of the tool")
    description: str = Field(..., description="Description of what the tool does")
    input_schema: Dict[str, Any] = Field(..., description="JSON schema for tool input parameters", alias="inputSchema")
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert to dictionary for serialization with camelCase keys.
        
        Uses Pydantic's model_dump with by_alias=True to ensure
        input_schema is serialized as inputSchema.
        """
        return self.model_dump(by_alias=True)


class MCPTransport(ABC):
    """
    Abstract base class for MCP transports.
    
    A transport is responsible for:
    1. Starting and managing an MCP server instance
    2. Translating Redis stream messages to/from the server's native protocol
    3. Health monitoring and status reporting
    4. Clean shutdown and resource cleanup
    
    The transport sits between MotetMCPClient (Redis streams) and the MCP server,
    acting as a protocol bridge while maintaining transport independence.
    
    Design Philosophy:
    - MotetMCPClient is transport-agnostic (always uses Redis streams)
    - Each transport implements this interface to handle its specific protocol
    - Transports are created via MCPTransportFactory based on configuration
    """
    
    def __init__(
        self,
        service_id: str,
        config: Dict[str, Any],
        worker_id: Optional[str] = None,
        startup_command_context: Optional[Any] = None
    ):
        """
        Initialize transport.
        
        Args:
            service_id: Unique identifier for this MCP service (e.g., "weather", "google_workspace")
            config: Transport-specific configuration from mcp_instance_manager.yaml
            worker_id: Optional worker ID for distributed environments
            startup_command_context: Optional context for vault credential fetching
        """
        self.service_id = service_id
        self.config = config
        self.worker_id = worker_id
        self.startup_command_context = startup_command_context
        self.is_running = False
        self.logger = logger.bind(
            service_id=service_id,
            transport=self.__class__.__name__,
            worker_id=worker_id
        )
    
    @abstractmethod
    async def start(self) -> bool:
        """
        Start the transport and MCP server.
        
        This should:
        1. Fetch any required credentials (from vault, environment, etc.)
        2. Start the MCP server (subprocess, HTTP connection, etc.)
        3. Establish communication channel
        4. Verify the server is responsive
        5. Set self.is_running = True on success
        
        Returns:
            True if started successfully, False otherwise
        """
        pass
    
    @abstractmethod
    async def stop(self) -> bool:
        """
        Stop the transport and MCP server.
        
        This should:
        1. Gracefully shut down the MCP server
        2. Close communication channels
        3. Clean up resources
        4. Set self.is_running = False
        
        Returns:
            True if stopped successfully, False otherwise
        """
        pass
    
    @abstractmethod
    async def health_check(self) -> bool:
        """
        Check if transport and MCP server are healthy.
        
        This should verify:
        1. MCP server is running
        2. Communication channel is open
        3. Server responds to health check requests
        
        Returns:
            True if healthy, False otherwise
        """
        pass
    
    @abstractmethod
    async def list_tools(self, timeout_seconds: int = 30) -> List[MCPToolDefinition]:
        """
        List available tools from the MCP server.
        
        This should:
        1. Send tools/list request to MCP server
        2. Wait for response (with timeout)
        3. Parse response into MCPToolDefinition objects
        
        Args:
            timeout_seconds: Maximum time to wait for response
            
        Returns:
            List of tool definitions
            
        Raises:
            TimeoutError: If server doesn't respond in time
            RuntimeError: If server returns an error
        """
        pass
    
    @abstractmethod
    async def call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        timeout_seconds: int = 30
    ) -> Dict[str, Any]:
        """
        Execute a tool on the MCP server.
        
        This should:
        1. Send tools/call request with tool name and arguments
        2. Wait for response (with timeout)
        3. Return the tool's result
        
        Args:
            tool_name: Name of the tool to execute
            arguments: Tool arguments as a dictionary
            timeout_seconds: Maximum time to wait for response
            
        Returns:
            Tool execution result as a dictionary
            
        Raises:
            TimeoutError: If server doesn't respond in time
            RuntimeError: If tool execution fails
        """
        pass
    
    # --- Optional resources/prompts (ADR-0076 Scope 3 Phase A) ---

    async def list_resources(
        self,
        timeout_seconds: int = 30,
    ) -> List[MCPResourceDefinition]:
        """
        List available resources from the MCP server (optional).
        Default: return empty list. Override in transport if server supports resources.
        """
        return []

    async def read_resource(
        self,
        uri: str,
        timeout_seconds: int = 30,
    ) -> List[MCPResourceContent]:
        """
        Read resource content by URI (optional).
        Default: raise NotImplementedError. Override in transport if server supports resources.
        """
        raise NotImplementedError(
            f"read_resource not supported by {self.__class__.__name__}"
        )

    async def list_prompts(
        self,
        timeout_seconds: int = 30,
    ) -> List[MCPPromptDefinition]:
        """
        List available prompts from the MCP server (optional).
        Default: return empty list. Override in transport if server supports prompts.
        """
        return []

    async def get_prompt(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
        timeout_seconds: int = 30,
    ) -> MCPPromptResult:
        """
        Get a prompt by name with optional arguments (optional).
        Default: raise NotImplementedError. Override in transport if server supports prompts.
        """
        raise NotImplementedError(
            f"get_prompt not supported by {self.__class__.__name__}"
        )

    def get_status(self) -> Dict[str, Any]:
        """
        Get transport status information.
        
        This provides diagnostic information about the transport state.
        Subclasses can override to add transport-specific details.
        
        Returns:
            Dictionary with status information
        """
        return {
            "service_id": self.service_id,
            "transport_type": self.__class__.__name__,
            "is_running": self.is_running,
            "worker_id": self.worker_id,
            "config": self._sanitize_config(self.config)
        }
    
    def _sanitize_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Sanitize configuration for status reporting (hide secrets).
        
        Args:
            config: Raw configuration dictionary
            
        Returns:
            Sanitized configuration safe for logging/reporting
        """
        sanitized = config.copy()
        
        # Sanitize environment variables that might contain secrets
        if "env" in sanitized:
            env = sanitized["env"].copy()
            for key in env:
                key_lower = key.lower()
                if any(secret in key_lower for secret in ["secret", "password", "token", "key", "credential"]):
                    env[key] = "***REDACTED***"
            sanitized["env"] = env
        
        # Sanitize vault-related fields
        for key in ["vault_credential_key", "bearer_token", "access_token"]:
            if key in sanitized:
                sanitized[key] = "***REDACTED***"
        
        return sanitized
    
    async def __aenter__(self):
        """Async context manager entry."""
        await self.start()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.stop()
        return False

