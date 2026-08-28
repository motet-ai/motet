"""
Motet - Stdio MCP Transport

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Stdio-based MCP transport for the Motet distributed framework.
    Wraps MotetMCPProxy to provide stdio transport functionality through the
    transport abstraction layer. Manages MCP servers as subprocesses with
    stdin/stdout communication bridged to Redis Motet Streams.

Dependencies:
    - asyncio: Asynchronous transport operations and subprocess management
    - structlog: Structured logging and observability
    - typing: Type hints and annotations
    - Base transport interfaces and MCP proxy

Usage:
    from motet.core.tools.mcp_motet.transports.stdio import StdioMCPTransport

    # Create stdio transport
    transport = StdioMCPTransport(
        server_config=MCPServerConfig(
            command="mcp-server",
            args=["--config", "config.json"]
        )
    )

    # Start transport
    await transport.start()

    # Execute tool
    result = await transport.execute_tool("tool_name", {"param": "value"})

Notes:
    - Provides stdio transport for MCP servers as subprocesses
    - Includes stdin/stdout communication bridged to Redis Streams
    - Supports MCP server lifecycle management
    - Includes comprehensive error handling and logging
    - Supports distributed coordination through Motet Streams
    - Integrates with MCP transport system
    - Includes comprehensive observability and monitoring
"""

from typing import Dict, Any, Optional, List, Union
import asyncio
import structlog

from motet.core.tools.mcp_motet.transports.base import (
    MCPTransport,
    MCPToolDefinition,
    MCPResourceDefinition,
    MCPResourceContent,
    MCPPromptDefinition,
    MCPPromptMessage,
    MCPPromptResult,
)
from motet.core.tools.mcp_motet.proxy.motet_mcp_proxy import (
    MotetMCPProxy,
    MCPServerConfig,
)
from motet.core.tools.mcp_motet.proxy.mcp_docker_stdio import DockerMCPAsyncProcess
from motet.core.tools.mcp_motet.client.motet_mcp_client import MotetMCPClient

logger = structlog.get_logger(__name__)


class StdioMCPTransport(MCPTransport):
    """
    Stdio-based MCP transport using MotetMCPProxy.
    
    This transport:
    1. Spawns MCP server as subprocess
    2. Uses MotetMCPProxy to bridge Redis streams ↔ stdio
    3. Communicates via MotetMCPClient (Redis streams)
    
    Configuration example:
        {
            "service_id": "weather",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@timlukahorstmann/mcp-weather"],
            "env": {"ACCUWEATHER_API_KEY": "..."},
            "instances": 2,
            "is_stateful": false
        }
    """
    
    def __init__(
        self,
        service_id: str,
        config: Dict[str, Any],
        worker_id: Optional[str] = None,
        startup_command_context: Optional[Any] = None
    ):
        super().__init__(service_id, config, worker_id, startup_command_context)
        
        self.proxy: Optional[MotetMCPProxy] = None
        self.client: Optional[MotetMCPClient] = None
        self._process: Optional[Union[asyncio.subprocess.Process, DockerMCPAsyncProcess]] = None
        
        # Extract context parameters from config
        # NOTE: `context_id` is an ADR-0058 instance_key produced by MCPInstanceManager.
        # Passing it through ensures MotetMCPProxy uses the configured lifecycle/visibility
        # rather than heuristic inference from presence of conversation_id.
        self.context_id = config.get("context_id")
        self.conversation_id = config.get("conversation_id")
        self.task_id = config.get("task_id")
        self.tenant_id = config.get("tenant_id")
        self.principal_id = config.get("principal_id")
        self.motet_id = config.get("motet_id")
    
    async def start(self) -> bool:
        """
        Start the stdio transport and MCP server subprocess.
        """
        try:
            self.logger.info("Starting stdio transport", service_id=self.service_id)

            command = self.config.get("command")
            if not command or not isinstance(command, str):
                raise ValueError(
                    f"stdio MCP transport requires a non-empty string 'command' in config (service_id={self.service_id})"
                )

            # Create proxy configuration
            proxy_config = MCPServerConfig(
                server_id=self.service_id,
                command=command,
                args=self.config.get("args", []),
                env=self.config.get("env", {}),
                working_dir=self.config.get("working_dir"),
                exec_image=self.config.get("exec_image"),
            )
            
            # Create MotetMCPProxy to manage subprocess and Redis stream bridge
            self.proxy = MotetMCPProxy(
                config=proxy_config,
                context_id=self.context_id,
                conversation_id=self.conversation_id,
                task_id=self.task_id,
                tenant_id=self.tenant_id,
                principal_id=self.principal_id,
                motet_id=self.motet_id,
                worker_id=self.worker_id
            )
            
            # Start the proxy (spawns subprocess and starts stream bridge)
            await self.proxy.start()
            
            # Store process reference
            self._process = self.proxy.mcp_process
            
            # Create client for tool calls
            self.client = MotetMCPClient()
            
            self.is_running = True
            self.logger.info(
                "Stdio transport started successfully",
                service_id=self.service_id,
                pid=self._process.pid if self._process else None
            )
            
            return True
            
        except Exception as e:
            self.logger.error(
                "Failed to start stdio transport",
                service_id=self.service_id,
                error=str(e),
                exc_info=True
            )
            self.is_running = False
            return False
    
    async def stop(self) -> bool:
        """
        Stop the stdio transport and MCP server subprocess.
        """
        try:
            self.logger.info("Stopping stdio transport", service_id=self.service_id)
            
            # Stop the proxy (terminates subprocess and stream bridge)
            if self.proxy:
                await self.proxy.stop()
            
            self.is_running = False
            self.logger.info("Stdio transport stopped", service_id=self.service_id)
            
            return True
            
        except Exception as e:
            self.logger.error(
                "Failed to stop stdio transport",
                service_id=self.service_id,
                error=str(e),
                exc_info=True
            )
            return False
    
    async def health_check(self) -> bool:
        """
        Check if stdio transport is healthy.
        
        Verifies:
        1. Proxy is running
        2. Subprocess is alive
        3. Can list tools (basic connectivity test)
        """
        try:
            if not self.is_running or not self.proxy:
                return False
            
            # Check if subprocess is still alive
            if self._process and self._process.returncode is not None:
                self.logger.warning(
                    "Stdio transport subprocess died",
                    service_id=self.service_id,
                    exit_code=self._process.returncode
                )
                return False
            
            # Try listing tools as a connectivity test (with short timeout)
            try:
                await asyncio.wait_for(
                    self.list_tools(timeout_seconds=5),
                    timeout=6.0
                )
                return True
            except asyncio.TimeoutError:
                self.logger.warning(
                    "Stdio transport health check timeout",
                    service_id=self.service_id
                )
                return False
            
        except Exception as e:
            self.logger.warning(
                "Stdio transport health check failed",
                service_id=self.service_id,
                error=str(e)
            )
            return False
    
    async def list_tools(self, timeout_seconds: int = 30) -> List[MCPToolDefinition]:
        """
        List available tools from the MCP server via Redis streams.
        
        Args:
            timeout_seconds: Maximum time to wait for response
            
        Returns:
            List of tool definitions
        """
        if not self.is_running or not self.client:
            raise RuntimeError(f"Stdio transport not running for service: {self.service_id}")

        client = self.client
        try:
            # Use MotetMCPClient to list tools via Redis streams
            # This is synchronous but we're in an async context, so we need to run in executor
            loop = asyncio.get_event_loop()
            tools_response = await loop.run_in_executor(
                None,
                lambda: client.list_tools(
                    service_id=self.service_id,
                    conversation_id=self.conversation_id,
                    task_id=self.task_id,
                    tenant_id=self.tenant_id,
                    principal_id=self.principal_id,
                    target_worker_id=self.worker_id,
                    timeout_seconds=timeout_seconds,
                )
            )

            # Convert to MCPToolDefinition objects
            tools = []
            for tool in tools_response.get("tools", []):
                tools.append(MCPToolDefinition(
                    name=tool["name"],
                    description=tool.get("description", ""),
                    **{"inputSchema": tool.get("inputSchema", {})},
                ))
            
            return tools
            
        except Exception as e:
            self.logger.error(
                "Failed to list tools via stdio transport",
                service_id=self.service_id,
                error=str(e),
                exc_info=True
            )
            raise RuntimeError(f"Failed to list tools: {e}") from e
    
    async def call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        timeout_seconds: int = 30
    ) -> Dict[str, Any]:
        """
        Execute a tool on the MCP server via Redis streams.
        
        Args:
            tool_name: Name of the tool to execute
            arguments: Tool arguments
            timeout_seconds: Maximum time to wait for response
            
        Returns:
            Tool execution result
        """
        if not self.is_running or not self.client:
            raise RuntimeError(f"Stdio transport not running for service: {self.service_id}")

        client = self.client
        try:
            # Use MotetMCPClient to call tool via Redis streams
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: client.call_tool(
                    service_id=self.service_id,
                    tool_name=tool_name,
                    params=arguments,
                    conversation_id=self.conversation_id,
                    task_id=self.task_id,
                    tenant_id=self.tenant_id,
                    principal_id=self.principal_id,  # Required for USER visibility services
                    motet_id=self.motet_id,  # Required for MOTET visibility services
                    target_worker_id=self.worker_id,
                    timeout_seconds=timeout_seconds,
                    command_context=self.startup_command_context
                )
            )
            
            return result
            
        except Exception as e:
            self.logger.error(
                "Failed to call tool via stdio transport",
                service_id=self.service_id,
                tool_name=tool_name,
                error=str(e),
                exc_info=True
            )
            raise RuntimeError(f"Failed to call tool {tool_name}: {e}") from e

    async def list_resources(
        self,
        timeout_seconds: int = 30,
    ) -> List[MCPResourceDefinition]:
        """List resources via MCP resources/list (ADR-0076 Scope 3)."""
        if not self.is_running or not self.client:
            return []
        client = self.client
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: client.list_resources(
                    service_id=self.service_id,
                    conversation_id=self.conversation_id,
                    task_id=self.task_id,
                    tenant_id=self.tenant_id,
                    principal_id=self.principal_id,
                    motet_id=self.motet_id,
                    target_worker_id=self.worker_id,
                    timeout_seconds=timeout_seconds,
                    command_context=self.startup_command_context,
                ),
            )
            resources = result.get("resources", [])
            return [
                MCPResourceDefinition(
                    uri=r["uri"],
                    name=r.get("name", r["uri"]),
                    title=r.get("title"),
                    description=r.get("description"),
                    mimeType=r.get("mimeType"),
                    icons=r.get("icons"),
                )
                for r in resources
            ]
        except Exception as e:
            self.logger.warning(
                "Failed to list resources via stdio transport",
                service_id=self.service_id,
                error=str(e),
            )
            return []

    async def read_resource(
        self,
        uri: str,
        timeout_seconds: int = 30,
    ) -> List[MCPResourceContent]:
        """Read resource via MCP resources/read (ADR-0076 Scope 3)."""
        if not self.is_running or not self.client:
            raise RuntimeError(f"Stdio transport not running for service: {self.service_id}")
        client = self.client
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: client.read_resource(
                service_id=self.service_id,
                uri=uri,
                conversation_id=self.conversation_id,
                task_id=self.task_id,
                tenant_id=self.tenant_id,
                principal_id=self.principal_id,
                motet_id=self.motet_id,
                target_worker_id=self.worker_id,
                timeout_seconds=timeout_seconds,
                command_context=self.startup_command_context,
            ),
        )
        contents = result.get("contents", [])
        return [
            MCPResourceContent(
                uri=c.get("uri", uri),
                mimeType=c.get("mimeType"),
                text=c.get("text"),
                blob=c.get("blob"),
            )
            for c in contents
        ]

    async def list_prompts(
        self,
        timeout_seconds: int = 30,
    ) -> List[MCPPromptDefinition]:
        """List prompts via MCP prompts/list (ADR-0076 Scope 3)."""
        if not self.is_running or not self.client:
            return []
        client = self.client
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: client.list_prompts(
                    service_id=self.service_id,
                    conversation_id=self.conversation_id,
                    task_id=self.task_id,
                    tenant_id=self.tenant_id,
                    principal_id=self.principal_id,
                    motet_id=self.motet_id,
                    target_worker_id=self.worker_id,
                    timeout_seconds=timeout_seconds,
                    command_context=self.startup_command_context,
                ),
            )
            prompts = result.get("prompts", [])
            return [
                MCPPromptDefinition(
                    name=p["name"],
                    title=p.get("title"),
                    description=p.get("description"),
                    arguments=p.get("arguments"),
                )
                for p in prompts
            ]
        except Exception as e:
            self.logger.warning(
                "Failed to list prompts via stdio transport",
                service_id=self.service_id,
                error=str(e),
            )
            return []

    async def get_prompt(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
        timeout_seconds: int = 30,
    ) -> MCPPromptResult:
        """Get prompt via MCP prompts/get (ADR-0076 Scope 3)."""
        if not self.is_running or not self.client:
            raise RuntimeError(f"Stdio transport not running for service: {self.service_id}")
        client = self.client
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: client.get_prompt(
                service_id=self.service_id,
                name=name,
                arguments=arguments,
                conversation_id=self.conversation_id,
                task_id=self.task_id,
                tenant_id=self.tenant_id,
                principal_id=self.principal_id,
                motet_id=self.motet_id,
                target_worker_id=self.worker_id,
                timeout_seconds=timeout_seconds,
                command_context=self.startup_command_context,
            ),
        )
        messages = result.get("messages", [])
        return MCPPromptResult(
            description=result.get("description"),
            messages=[
                MCPPromptMessage(role=m.get("role", "user"), content=m.get("content", {}))
                for m in messages
            ],
        )

    def get_status(self) -> Dict[str, Any]:
        """Get stdio transport status with additional details."""
        status = super().get_status()
        
        # Add stdio-specific status
        status.update({
            "subprocess_pid": self._process.pid if self._process else None,
            "subprocess_exit_code": self._process.returncode if self._process else None,
            "proxy_status": "running" if self.proxy and self.is_running else "stopped",
            "conversation_id": self.conversation_id,
            "task_id": self.task_id,
            "tenant_id": self.tenant_id
        })
        
        return status

