"""
Motet - HTTP MCP Transport

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    HTTP-based MCP transport for the Motet distributed framework.
    Provides HTTP transport functionality for MCP servers that expose REST APIs
    with OAuth 2.1 bearer token authentication and vault integration.
    Ideal for multi-user, stateless MCP servers and containerized deployments.

Dependencies:
    - aiohttp: Asynchronous HTTP client for MCP server communication
    - asyncio: Asynchronous transport operations
    - structlog: Structured logging and observability
    - typing: Type hints and annotations
    - Base transport interfaces

Usage:
    from motet.core.tools.mcp_motet.transports.http import HTTPMCPTransport

    # Create HTTP transport
    transport = HTTPMCPTransport(
        base_url="https://mcp-server.example.com",
        auth_token="bearer_token"
    )

    # Start transport
    await transport.start()

    # Execute tool
    result = await transport.execute_tool("tool_name", {"param": "value"})

Notes:
    - Provides HTTP transport for MCP servers with REST APIs
    - Supports OAuth 2.1 bearer token authentication
    - Includes vault integration for credential management
    - Ideal for multi-user, stateless MCP servers
    - Supports containerized and serverless deployments
    - Integrates with MCP transport system
    - Includes comprehensive error handling and logging
"""

from typing import Dict, Any, Optional, List, Union
import asyncio
import aiohttp
import json
import os
import uuid
import structlog
from urllib.parse import urlparse, urlunparse

from motet.core.tools.mcp_motet.transports.base import (
    MCPTransport,
    MCPToolDefinition,
    MCPResourceDefinition,
    MCPResourceContent,
    MCPPromptDefinition,
    MCPPromptMessage,
    MCPPromptResult,
)
from motet.core.tools.mcp_motet.proxy.motet_mcp_stream_bridge import MotetMCPStreamBridge
from motet.core.tools.mcp_motet.protocol import (
    LifecycleDuration,
    MCPRequestMessage,
    MCPResponseMessage,
    StreamType,
    Visibility,
    generate_stream_name,
)

from motet.core.tools.mcp_motet.proxy.mcp_docker_http import DockerSidecarProcess

logger = structlog.get_logger(__name__)


def mcp_http_client_host_for_docker_sidecar() -> str:
    """
    Hostname the worker uses to reach an HTTP MCP Docker sidecar's published port.

    Override with ``MOTET_MCP_HTTP_CLIENT_HOST`` (e.g. ``172.17.0.1`` on Linux when
    ``host.docker.internal`` is unavailable).
    """
    return (os.getenv("MOTET_MCP_HTTP_CLIENT_HOST") or "host.docker.internal").strip() or "host.docker.internal"


def rewrite_localhost_base_url_for_mcp_docker_sidecar(base_url: str, port: int) -> str:
    """
    When ``start_server`` uses a Docker sidecar, ``base_url`` often says ``localhost``
    or ``127.0.0.1`` (subprocess semantics). The HTTP client runs in the worker
    container and must use the Docker host (or gateway) instead.
    """
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    if host not in ("localhost", "127.0.0.1", "::1", ""):
        return base_url
    if host == "" and not parsed.netloc:
        return base_url

    client_host = mcp_http_client_host_for_docker_sidecar()
    userinfo = ""
    if parsed.username is not None:
        userinfo = parsed.username
        if parsed.password:
            userinfo += f":{parsed.password}"
        userinfo += "@"
    # For Docker sidecars the caller passes the effective host-mapped port.
    # Always use it so worker-specific mapped ports override localhost literals.
    use_port = int(port)
    new_netloc = f"{userinfo}{client_host}:{use_port}"
    return urlunparse(
        (parsed.scheme, new_netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
    )


class HTTPMCPTransport(MCPTransport):
    """
    HTTP-based MCP transport with bearer token authentication.
    
    This transport:
    1. Optionally starts HTTP MCP server as subprocess
    2. Creates HTTP client session with bearer token auth
    3. Fetches bearer token from vault per-request
    4. Translates MCP protocol to HTTP REST API
    
    Configuration example:
        {
            "service_id": "google_workspace",
            "transport": "http",
            "start_server": true,  # Optional: start server as subprocess
            "command": "uvx",
            "args": ["workspace-mcp", "--transport", "streamable-http"],
            "env": {
                "MCP_ENABLE_OAUTH21": "true",
                "WORKSPACE_MCP_STATELESS_MODE": "true"
            },
            "port": 8100,
            "base_url": "http://localhost:8100/mcp",
            "use_vault_token": true,
            "vault_credential_key": "oauth:tokens:google_workspace:global",
            "token_field": "access_token"
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
        
        self.session: Optional[aiohttp.ClientSession] = None
        self._process: Optional[Union[asyncio.subprocess.Process, DockerSidecarProcess]] = None
        self._bearer_token: Optional[str] = None
        self._initialized: bool = False
        self._initialize_lock = asyncio.Lock()
        self._mcp_session_id: Optional[str] = None
        self._request_task: Optional[asyncio.Task] = None
        self._pending_requests: Dict[str, float] = {}
        
        # Extract HTTP-specific configuration
        self.start_server = config.get("start_server", False)
        self.base_url = config.get("base_url", "http://localhost:8000/mcp")
        self.port = config.get("port", 8000)
        self.use_vault_token = config.get("use_vault_token", False)
        self.vault_credential_key = config.get("vault_credential_key")
        self.token_field = config.get("token_field", "access_token")
        # Startup probing for HTTP servers that need warm-up/install time (e.g., npx).
        self.startup_timeout_seconds = config.get("startup_timeout_seconds", 45)
        self.startup_probe_interval_seconds = config.get("startup_probe_interval_seconds", 2)
        
        # Server startup configuration (if start_server=true)
        self.command = config.get("command")
        self.args = config.get("args", [])
        self.env = config.get("env", {})
        # ADR-0076: optional Streamable HTTP SSE (when true, parse text/event-stream responses)
        self.streamable_http_sse = config.get("streamable_http_sse", False)
        self.protocol_version = config.get("protocol_version", "2025-11-25")
        self.exec_image: Optional[str] = config.get("exec_image")
        # ADR-0058: context_id is the authoritative instance_key.
        self.context_id = config.get("context_id")
        self.instance_key = self.context_id or f"{self.service_id}:global"
        self.visibility, self.lifecycle = self._resolve_scope(
            service_id=self.service_id,
            explicit_context_id=self.context_id,
        )
        self.stream_bridge = MotetMCPStreamBridge(f"http-{self.service_id}-bridge")
        self.request_stream = generate_stream_name(
            service_id=self.service_id,
            visibility=self.visibility,
            instance_key=self.instance_key,
            stream_type=StreamType.REQUESTS,
            manager_id=self.worker_id,
        )
        self.response_stream = generate_stream_name(
            service_id=self.service_id,
            visibility=self.visibility,
            instance_key=self.instance_key,
            stream_type=StreamType.RESPONSES,
            manager_id=self.worker_id,
        )
        self.control_stream = generate_stream_name(
            service_id=self.service_id,
            visibility=self.visibility,
            instance_key=self.instance_key,
            stream_type=StreamType.CONTROL,
            manager_id=self.worker_id,
        )
        self.consumer_name = f"consumer-http-{self.service_id}-{str(uuid.uuid4())[:8]}"
        self.group_name = f"group-{self.service_id}-{self.instance_key}"
    
    async def start(self) -> bool:
        """
        Start the HTTP transport and optionally the MCP HTTP server.
        """
        try:
            self.logger.info("Starting HTTP transport", service_id=self.service_id)
            
            # Step 1: Start HTTP server as subprocess if configured.
            # Attach-to-singleton instances (start_server=false) still need the
            # Docker-host rewrite: YAML says 127.0.0.1, but the client runs in
            # mcp-manager and must reach the published sidecar port.
            if self.start_server:
                await self._start_http_server()
            else:
                self._rewrite_base_url_for_docker_sidecar_client()
            
            # Step 2: Fetch bearer token from vault if configured
            if self.use_vault_token:
                await self._fetch_vault_token()
            
            # Step 3: Create HTTP client session
            await self._create_http_session()
            await self.stream_bridge.initialize()
            await self._setup_consumer_groups()
            
            # Step 4: Mark as running before verification (needed for list_tools to work)
            self.is_running = True
            self._initialized = False
            self._mcp_session_id = None
            
            # Step 5: Verify server is responsive
            try:
                if not await self._verify_server_connection():
                    self.is_running = False
                    raise RuntimeError("HTTP server not responsive")
            except Exception as e:
                self.is_running = False
                raise
            
            self._request_task = asyncio.create_task(self._process_requests())

            self.logger.info(
                "HTTP transport started successfully",
                service_id=self.service_id,
                base_url=self.base_url,
                has_token=self._bearer_token is not None,
                request_stream=self.request_stream,
                response_stream=self.response_stream,
            )
            
            return True
            
        except Exception as e:
            self.logger.error(
                "Failed to start HTTP transport",
                service_id=self.service_id,
                error=str(e),
                exc_info=True
            )
            self.is_running = False
            
            # Cleanup on failure
            await self._cleanup()
            
            return False
    
    async def stop(self) -> bool:
        """
        Stop the HTTP transport and optionally the MCP HTTP server.
        """
        try:
            self.logger.info("Stopping HTTP transport", service_id=self.service_id)
            
            await self._cleanup()
            
            self.is_running = False
            self.logger.info("HTTP transport stopped", service_id=self.service_id)
            
            return True
            
        except Exception as e:
            self.logger.error(
                "Failed to stop HTTP transport",
                service_id=self.service_id,
                error=str(e),
                exc_info=True
            )
            return False
    
    async def health_check(self) -> bool:
        """
        Check if HTTP transport is healthy.
        
        Verifies:
        1. HTTP session is open
        2. Server responds to health endpoint
        3. Bearer token is valid (if using vault tokens)
        """
        try:
            if not self.is_running or not self.session:
                return False
            session = self.session

            # Check if server subprocess is alive (if we started it)
            if self._process and self._process.returncode is not None:
                self.logger.warning(
                    "HTTP server subprocess died",
                    service_id=self.service_id,
                    exit_code=self._process.returncode
                )
                return False

            # Try a simple HTTP request to verify connectivity
            try:
                headers = self._get_auth_headers()
                async with session.get(
                    f"{self.base_url}/health",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    return response.status in [200, 404]  # 404 is okay if no health endpoint
            except Exception as e:
                self.logger.warning(
                    "HTTP transport health check failed",
                    service_id=self.service_id,
                    error=str(e)
                )
                return False
            
        except Exception as e:
            self.logger.warning(
                "HTTP transport health check exception",
                service_id=self.service_id,
                error=str(e)
            )
            return False
    
    async def list_tools(self, timeout_seconds: int = 30) -> List[MCPToolDefinition]:
        """
        List available tools from the MCP HTTP server.
        
        Makes HTTP POST to /tools/list endpoint.
        
        Args:
            timeout_seconds: Maximum time to wait for response
            
        Returns:
            List of tool definitions
        """
        if not self.is_running or not self.session:
            raise RuntimeError(f"HTTP transport not running for service: {self.service_id}")
        
        try:
            result = await self._post_jsonrpc("tools/list", {}, timeout_seconds)
            tools_data = result.get("tools", [])
            tools = []
            for tool in tools_data:
                tools.append(MCPToolDefinition(
                    name=tool["name"],
                    description=tool.get("description", ""),
                    **{"inputSchema": tool.get("inputSchema", {})},
                ))
            return tools
            
        except aiohttp.ClientError as e:
            self.logger.error(
                "HTTP request failed while listing tools",
                service_id=self.service_id,
                error=str(e),
                exc_info=True
            )
            raise RuntimeError(f"Failed to list tools: {e}") from e
        except Exception as e:
            self.logger.error(
                "Failed to list tools via HTTP transport",
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
        Execute a tool on the MCP HTTP server.
        
        Makes HTTP POST to base URL with tools/call method.
        
        Args:
            tool_name: Name of the tool to execute
            arguments: Tool arguments
            timeout_seconds: Maximum time to wait for response
            
        Returns:
            Tool execution result
        """
        if not self.is_running or not self.session:
            raise RuntimeError(f"HTTP transport not running for service: {self.service_id}")
        
        try:
            result = await self._post_jsonrpc(
                "tools/call",
                {"name": tool_name, "arguments": arguments},
                timeout_seconds,
            )
            return result
            
        except aiohttp.ClientError as e:
            self.logger.error(
                "HTTP request failed while calling tool",
                service_id=self.service_id,
                tool_name=tool_name,
                error=str(e),
                exc_info=True
            )
            raise RuntimeError(f"Failed to call tool {tool_name}: {e}") from e
        except Exception as e:
            self.logger.error(
                "Failed to call tool via HTTP transport",
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
        if not self.is_running or not self.session:
            raise RuntimeError(f"HTTP transport not running for service: {self.service_id}")
        try:
            result = await self._post_jsonrpc("resources/list", {}, timeout_seconds)
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
            self.logger.error(
                "Failed to list resources via HTTP transport",
                service_id=self.service_id,
                error=str(e),
                exc_info=True,
            )
            raise RuntimeError(f"Failed to list resources: {e}") from e

    async def read_resource(
        self,
        uri: str,
        timeout_seconds: int = 30,
    ) -> List[MCPResourceContent]:
        """Read resource content via MCP resources/read (ADR-0076 Scope 3)."""
        if not self.is_running or not self.session:
            raise RuntimeError(f"HTTP transport not running for service: {self.service_id}")
        try:
            result = await self._post_jsonrpc(
                "resources/read",
                {"uri": uri},
                timeout_seconds,
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
        except Exception as e:
            self.logger.error(
                "Failed to read resource via HTTP transport",
                service_id=self.service_id,
                uri=uri,
                error=str(e),
                exc_info=True,
            )
            raise RuntimeError(f"Failed to read resource {uri}: {e}") from e

    async def list_prompts(
        self,
        timeout_seconds: int = 30,
    ) -> List[MCPPromptDefinition]:
        """List prompts via MCP prompts/list (ADR-0076 Scope 3)."""
        if not self.is_running or not self.session:
            raise RuntimeError(f"HTTP transport not running for service: {self.service_id}")
        try:
            result = await self._post_jsonrpc("prompts/list", {}, timeout_seconds)
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
            self.logger.error(
                "Failed to list prompts via HTTP transport",
                service_id=self.service_id,
                error=str(e),
                exc_info=True,
            )
            raise RuntimeError(f"Failed to list prompts: {e}") from e

    async def get_prompt(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
        timeout_seconds: int = 30,
    ) -> MCPPromptResult:
        """Get prompt via MCP prompts/get (ADR-0076 Scope 3)."""
        if not self.is_running or not self.session:
            raise RuntimeError(f"HTTP transport not running for service: {self.service_id}")
        try:
            params: Dict[str, Any] = {"name": name}
            if arguments is not None:
                params["arguments"] = arguments
            result = await self._post_jsonrpc("prompts/get", params, timeout_seconds)
            messages = result.get("messages", [])
            return MCPPromptResult(
                description=result.get("description"),
                messages=[
                    MCPPromptMessage(role=m.get("role", "user"), content=m.get("content", {}))
                    for m in messages
                ],
            )
        except Exception as e:
            self.logger.error(
                "Failed to get prompt via HTTP transport",
                service_id=self.service_id,
                name=name,
                error=str(e),
                exc_info=True,
            )
            raise RuntimeError(f"Failed to get prompt {name}: {e}") from e

    def get_status(self) -> Dict[str, Any]:
        """Get HTTP transport status with additional details."""
        status = super().get_status()
        
        # Add HTTP-specific status
        status.update({
            "base_url": self.base_url,
            "port": self.port,
            "start_server": self.start_server,
            "streamable_http_sse": self.streamable_http_sse,
            "instance_key": self.instance_key,
            "visibility": self.visibility.value,
            "subprocess_pid": self._process.pid if self._process else None,
            "subprocess_exit_code": self._process.returncode if self._process else None,
            "session_open": self.session is not None
            and not getattr(self.session, "closed", False),
            "has_bearer_token": self._bearer_token is not None,
            "use_vault_token": self.use_vault_token
        })
        
        return status

    def _rewrite_base_url_for_docker_sidecar_client(
        self,
        host_port: Optional[int] = None,
    ) -> None:
        """Rewrite localhost base_url so a containerized client can reach the sidecar."""
        from motet.core.execution.mcp_backend import mcp_exec_uses_docker

        if not mcp_exec_uses_docker():
            return
        mapped_port = int(host_port if host_port is not None else self.port)
        rewritten = rewrite_localhost_base_url_for_mcp_docker_sidecar(
            self.base_url,
            mapped_port,
        )
        if rewritten == self.base_url:
            return
        self.logger.info(
            "mcp_http_base_url_rewritten_for_docker_sidecar",
            service_id=self.service_id,
            original_base_url=self.base_url,
            effective_base_url=rewritten,
            client_host=mcp_http_client_host_for_docker_sidecar(),
            mapped_host_port=mapped_port,
            container_port=int(self.port),
            start_server=self.start_server,
        )
        self.base_url = rewritten
    
    # Private helper methods
    
    async def _start_http_server(self) -> None:
        """Start HTTP server as subprocess or Docker sidecar (Phase 2)."""
        try:
            from motet.core.execution.mcp_backend import mcp_exec_uses_docker

            use_docker_srv = mcp_exec_uses_docker()

            # Prepare environment - merge with system environment to include PATH
            env = os.environ.copy()
            if self.env:
                env.update(self.env)

            if not self.command or not isinstance(self.command, str):
                raise ValueError(
                    f"HTTP MCP start_server requires a non-empty string 'command' in config (service_id={self.service_id})"
                )

            if use_docker_srv:
                self.logger.info(
                    "Starting HTTP server Docker sidecar",
                    service_id=self.service_id,
                    command=self.command,
                    args=self.args,
                )
                from motet.core.tools.mcp_motet.proxy.mcp_docker_http import (
                    start_mcp_http_sidecar,
                )

                self._process = await start_mcp_http_sidecar(
                    service_id=self.service_id,
                    command=self.command,
                    args=self.args,
                    env=env,
                    port=int(self.port),
                    exec_image=self.exec_image,
                    worker_id=self.worker_id,
                )
                self._rewrite_base_url_for_docker_sidecar_client(
                    int(getattr(self._process, "host_port", self.port)),
                )
            else:
                self.logger.info(
                    "Starting HTTP server subprocess",
                    service_id=self.service_id,
                    command=self.command,
                    args=self.args,
                )
                self._process = await asyncio.create_subprocess_exec(
                    self.command,
                    *self.args,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self.logger.info(
                    "HTTP server subprocess spawned",
                    service_id=self.service_id,
                    pid=self._process.pid,
                    command=self.command,
                    args=self.args,
                )

            await asyncio.sleep(3)

            proc = self._process
            if proc is not None and proc.returncode is not None:
                if use_docker_srv:
                    raise RuntimeError(
                        f"HTTP MCP Docker sidecar exited early (code {proc.returncode})"
                    )
                out_stream = proc.stdout
                err_stream = proc.stderr
                if out_stream is None or err_stream is None:
                    raise RuntimeError("HTTP server subprocess missing stdout/stderr pipes")
                stdout_data = await out_stream.read()
                stderr_data = await err_stream.read()

                stdout_str = stdout_data.decode("utf-8", errors="replace") if stdout_data else ""
                stderr_str = stderr_data.decode("utf-8", errors="replace") if stderr_data else ""

                self.logger.error(
                    "HTTP server exited immediately",
                    service_id=self.service_id,
                    exit_code=proc.returncode,
                    stdout=stdout_str[:500],
                    stderr=stderr_str[:500],
                )

                raise RuntimeError(
                    f"HTTP server failed to start (exit code: {proc.returncode})\n"
                    f"STDERR: {stderr_str[:200]}\n"
                    f"STDOUT: {stdout_str[:200]}"
                )

            self.logger.info(
                "HTTP server process started",
                service_id=self.service_id,
                pid=getattr(self._process, "pid", None),
                backend="docker" if use_docker_srv else "subprocess",
            )
            
        except Exception as e:
            self.logger.error(
                "Failed to start HTTP server subprocess",
                service_id=self.service_id,
                error=str(e),
                exc_info=True
            )
            raise
    
    async def _fetch_vault_token(self) -> None:
        """Fetch bearer token from vault."""
        try:
            if not self.startup_command_context:
                raise RuntimeError("No command context provided for vault token fetch")
            
            if not self.vault_credential_key:
                raise RuntimeError("No vault_credential_key configured")
            
            self.logger.info(
                "Fetching bearer token from vault",
                service_id=self.service_id,
                credential_key=self.vault_credential_key
            )
            
            # Import vault client (sync)
            from motet.core.security.vault_client import get_vault_client
            
            # Fetch credentials from vault (synchronous)
            vault_client = get_vault_client()
            credentials = vault_client.get_credential(
                credential_key=self.vault_credential_key,
                context=self.startup_command_context
            )

            if not credentials:
                raise RuntimeError(
                    f"Vault returned no credential data for '{self.vault_credential_key}'"
                )

            # Extract bearer token from credentials
            self._bearer_token = credentials.get(self.token_field)
            
            if not self._bearer_token:
                raise RuntimeError(
                    f"No '{self.token_field}' field found in vault credential '{self.vault_credential_key}'"
                )
            
            self.logger.info(
                "Bearer token fetched from vault",
                service_id=self.service_id,
                token_length=len(self._bearer_token)
            )
            
        except Exception as e:
            self.logger.error(
                "Failed to fetch bearer token from vault",
                service_id=self.service_id,
                error=str(e),
                exc_info=True
            )
            raise
    
    async def _create_http_session(self) -> None:
        """Create aiohttp client session."""
        try:
            self.session = aiohttp.ClientSession()
            
            self.logger.info(
                "HTTP client session created",
                service_id=self.service_id
            )
            
        except Exception as e:
            self.logger.error(
                "Failed to create HTTP session",
                service_id=self.service_id,
                error=str(e),
                exc_info=True
            )
            raise
    
    async def _verify_server_connection(self) -> bool:
        """Verify that HTTP server is responsive with bounded retries."""
        self.logger.info(
            "Verifying HTTP server connection",
            service_id=self.service_id,
            base_url=self.base_url,
            startup_timeout_seconds=self.startup_timeout_seconds,
            startup_probe_interval_seconds=self.startup_probe_interval_seconds,
        )

        deadline = asyncio.get_running_loop().time() + float(self.startup_timeout_seconds)
        attempt = 0
        last_error: Optional[Exception] = None

        while asyncio.get_running_loop().time() < deadline:
            attempt += 1
            try:
                # list_tools validates both transport connectivity and MCP request/response path.
                await self.list_tools(timeout_seconds=10)
                self.logger.info(
                    "HTTP server connection verified",
                    service_id=self.service_id,
                    attempt=attempt,
                )
                return True
            except Exception as e:
                last_error = e
                remaining = max(0.0, deadline - asyncio.get_running_loop().time())
                self.logger.info(
                    "HTTP server not ready yet, retrying",
                    service_id=self.service_id,
                    attempt=attempt,
                    remaining_seconds=round(remaining, 1),
                    error=str(e),
                )
                await asyncio.sleep(float(self.startup_probe_interval_seconds))

        self.logger.error(
            "HTTP server connection verification failed",
            service_id=self.service_id,
            attempts=attempt,
            error=str(last_error) if last_error else "unknown",
        )
        return False

    def _resolve_scope(
        self,
        *,
        service_id: str,
        explicit_context_id: Optional[str],
    ) -> tuple[Visibility, LifecycleDuration]:
        """Infer ADR-0058 visibility/lifecycle from manager-provided instance_key."""
        if not explicit_context_id:
            return Visibility.GLOBAL, LifecycleDuration.PERMANENT
        if not isinstance(explicit_context_id, str) or not explicit_context_id.startswith(f"{service_id}:"):
            return Visibility.GLOBAL, LifecycleDuration.PERMANENT
        parts = explicit_context_id.split(":")
        if len(parts) >= 2 and parts[1] == "global":
            visibility = Visibility.GLOBAL
        elif len(parts) >= 4:
            visibility = Visibility.USER
        elif len(parts) == 3:
            visibility = Visibility.MOTET
        elif len(parts) == 2:
            visibility = Visibility.TENANT
        else:
            visibility = Visibility.GLOBAL
        if ":conversation:" in explicit_context_id:
            lifecycle = LifecycleDuration.CONVERSATION
        elif ":task:" in explicit_context_id:
            lifecycle = LifecycleDuration.TASK
        elif ":session:" in explicit_context_id:
            lifecycle = LifecycleDuration.SESSION
        else:
            lifecycle = LifecycleDuration.PERMANENT
        return visibility, lifecycle

    async def _setup_consumer_groups(self) -> None:
        """Create request/control consumer groups used by MotetMCPClient traffic."""
        await self.stream_bridge.create_consumer_group(self.request_stream, self.group_name, "0")
        await self.stream_bridge.create_consumer_group(self.control_stream, self.group_name, "0")

    async def _process_requests(self) -> None:
        """Consume request stream and forward JSON-RPC requests to HTTP MCP server."""
        self.logger.debug(
            "http_transport_started_consuming_requests",
            service_id=self.service_id,
            request_stream=self.request_stream,
            group_name=self.group_name,
            consumer_name=self.consumer_name,
        )
        while self.is_running:
            try:
                messages = await self.stream_bridge.consume_messages(
                    self.request_stream,
                    self.group_name,
                    self.consumer_name,
                    count=10,
                    block_ms=1000,
                )
                for msg in messages:
                    await self._handle_request_message(msg)
            except Exception as e:
                self.logger.error(
                    "HTTP transport request loop error",
                    service_id=self.service_id,
                    error=str(e),
                    exc_info=True,
                )
                await asyncio.sleep(1)

    async def _handle_request_message(self, msg_data: Dict[str, Any]) -> None:
        """Handle one Motet stream request and publish correlated response."""
        request_msg: Optional[MCPRequestMessage] = None
        message_id = msg_data.get("message_id", "unknown")
        jsonrpc_response: Optional[Dict[str, Any]] = None
        try:
            request_msg = MCPRequestMessage(**msg_data["message_data"])
            request_id = request_msg.id
            self._pending_requests[request_id] = asyncio.get_running_loop().time()
            jsonrpc_request = request_msg.jsonrpc_request or {}
            method = str(jsonrpc_request.get("method") or "")
            params = jsonrpc_request.get("params") or {}
            timeout_seconds = max(1, int((request_msg.timeout_ms or 30000) / 1000))

            result = await self._post_jsonrpc(
                method=method,
                params=params,
                timeout_seconds=timeout_seconds,
            )
            jsonrpc_response = {
                "jsonrpc": "2.0",
                "id": jsonrpc_request.get("id", request_id),
                "result": result,
            }
        except Exception as e:
            request_id = request_msg.id if request_msg else "unknown"
            jsonrpc_response = {
                "jsonrpc": "2.0",
                "id": (request_msg.jsonrpc_request or {}).get("id", request_id) if request_msg else request_id,
                "error": {
                    "code": -32000,
                    "message": str(e),
                },
            }
            self.logger.error(
                "HTTP transport failed to process stream request",
                service_id=self.service_id,
                request_id=request_id,
                message_id=message_id,
                error=str(e),
                exc_info=True,
            )
        finally:
            if request_msg is not None and jsonrpc_response is not None:
                started = self._pending_requests.pop(request_msg.id, None)
                processing_ms = None
                if started is not None:
                    processing_ms = int((asyncio.get_running_loop().time() - started) * 1000)
                response_msg = MCPResponseMessage(
                    service_id=self.service_id,
                    instance_key=self.instance_key,
                    request_id=request_msg.id,
                    processing_time_ms=processing_ms,
                    jsonrpc_response=jsonrpc_response,
                )
                await self.stream_bridge.publish_message(self.response_stream, response_msg)
            await self.stream_bridge.acknowledge_message(
                self.request_stream,
                self.group_name,
                message_id,
            )
    
    def _get_auth_headers(self) -> Dict[str, str]:
        """Get authentication headers for HTTP requests."""
        headers = {}

        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"

        return headers

    async def _post_jsonrpc(
        self,
        method: str,
        params: Dict[str, Any],
        timeout_seconds: int = 30,
        ensure_initialized: bool = True,
    ) -> Dict[str, Any]:
        """
        Send a JSON-RPC request via POST and return the result (ADR-0076).
        When streamable_http_sse is True and response is text/event-stream,
        parses SSE and extracts the JSON-RPC response for our request id.
        """
        if ensure_initialized and method != "initialize":
            await self._ensure_initialized(timeout_seconds=timeout_seconds)

        request_id = str(uuid.uuid4())
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        headers = self._get_json_request_headers()
        session = self.session
        if session is None or getattr(session, "closed", False):
            raise RuntimeError("HTTP client session not initialized or closed")
        async with session.post(
            self.base_url,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        ) as response:
            response.raise_for_status()
            mcp_session_id = response.headers.get("mcp-session-id")
            if mcp_session_id:
                self._mcp_session_id = mcp_session_id
            content_type = (response.headers.get("Content-Type") or "").lower()
            if self.streamable_http_sse and "text/event-stream" in content_type:
                return await self._parse_sse_response(response, request_id, timeout_seconds)
            if self.streamable_http_sse and response.status in (202, 204):
                return await self._consume_sse_via_get(request_id, timeout_seconds)
            data = await response.json()
        if "error" in data:
            raise RuntimeError(f"MCP server error: {data['error']}")
        if str(data.get("id")) not in {"", "None", request_id}:
            raise RuntimeError(
                f"MCP response id mismatch for {method}: expected {request_id}, got {data.get('id')}"
            )
        return data.get("result", {})

    async def _post_notification(
        self,
        method: str,
        params: Dict[str, Any],
        timeout_seconds: int = 30,
    ) -> None:
        """Send a JSON-RPC notification via POST (no id/result expected)."""
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        headers = self._get_json_request_headers()
        session = self.session
        if session is None or getattr(session, "closed", False):
            raise RuntimeError("HTTP client session not initialized or closed")
        async with session.post(
            self.base_url,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        ) as response:
            response.raise_for_status()

    async def _ensure_initialized(self, timeout_seconds: int = 30) -> None:
        """Ensure MCP initialize handshake completes once per transport session."""
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await self._post_jsonrpc(
                "initialize",
                {
                    "protocolVersion": self.protocol_version,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "imf-http-mcp-transport",
                        "version": "1.0.0",
                    },
                },
                timeout_seconds=timeout_seconds,
                ensure_initialized=False,
            )
            await self._post_notification(
                "notifications/initialized",
                {},
                timeout_seconds=timeout_seconds,
            )
            self._initialized = True
            self.logger.info(
                "HTTP MCP session initialized",
                service_id=self.service_id,
                protocol_version=self.protocol_version,
            )

    async def _parse_sse_response(
        self,
        response: aiohttp.ClientResponse,
        request_id: str,
        timeout_seconds: int,
    ) -> Dict[str, Any]:
        """
        Parse Server-Sent Events body for JSON-RPC messages (ADR-0076).
        Returns the result of the message whose id matches request_id.
        """
        buffer = ""
        async with asyncio.timeout(timeout_seconds):
            async for chunk in response.content.iter_any():
                if not chunk:
                    continue
                buffer += chunk.decode("utf-8", errors="replace").replace("\r\n", "\n")
                events, buffer = self._extract_sse_events(buffer)
                for event in events:
                    msg = self._decode_sse_event_json(event)
                    if not msg:
                        continue
                    if str(msg.get("id")) != str(request_id):
                        continue
                    if "error" in msg:
                        raise RuntimeError(f"MCP server error: {msg['error']}")
                    return msg.get("result", {})
        raise RuntimeError("SSE stream ended without matching response")

    async def _consume_sse_via_get(
        self,
        request_id: str,
        timeout_seconds: int,
    ) -> Dict[str, Any]:
        """
        Open optional Streamable HTTP GET SSE channel and wait for request result.
        """
        headers = self._get_sse_get_headers()
        session = self.session
        if session is None or getattr(session, "closed", False):
            raise RuntimeError("HTTP client session not initialized or closed")
        async with session.get(
            self.base_url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        ) as response:
            response.raise_for_status()
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "text/event-stream" not in content_type:
                raise RuntimeError(
                    f"Expected text/event-stream for SSE GET, got Content-Type: {content_type}"
                )
            return await self._parse_sse_response(response, request_id, timeout_seconds)

    def _extract_sse_events(self, buffer: str) -> tuple[List[Dict[str, str]], str]:
        """
        Extract complete SSE events from a text buffer.

        SSE events are delimited by a blank line. Returns (events, remainder).
        """
        events: List[Dict[str, str]] = []
        while "\n\n" in buffer:
            raw_event, buffer = buffer.split("\n\n", 1)
            data_lines: List[str] = []
            event: Dict[str, str] = {}
            for raw_line in raw_event.split("\n"):
                if not raw_line:
                    continue
                if raw_line.startswith(":"):
                    continue
                if ":" in raw_line:
                    field, value = raw_line.split(":", 1)
                    value = value.lstrip(" ")
                else:
                    field, value = raw_line, ""
                if field == "data":
                    data_lines.append(value)
                elif field in {"event", "id", "retry"}:
                    event[field] = value
            if data_lines:
                event["data"] = "\n".join(data_lines)
            events.append(event)
        return events, buffer

    def _decode_sse_event_json(self, event: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """Decode JSON payload from an SSE event `data` field."""
        data_str = (event.get("data") or "").strip()
        if not data_str or data_str == "[DONE]":
            return None
        try:
            payload = json.loads(data_str)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            self.logger.debug(
                "Skipping non-JSON SSE data payload",
                service_id=self.service_id,
                payload_preview=data_str[:120],
            )
        return None

    def _get_json_request_headers(self) -> Dict[str, str]:
        """
        Get headers for MCP JSON-RPC POST requests (Streamable HTTP, ADR-0076).

        Includes Content-Type, Accept (to allow server streaming via SSE when supported),
        and auth. Use for tools/list, tools/call, and any other JSON-RPC over POST.
        """
        headers = self._get_auth_headers()
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json, text/event-stream"
        if self._mcp_session_id:
            headers["mcp-session-id"] = self._mcp_session_id
        return headers

    def _get_sse_get_headers(self) -> Dict[str, str]:
        """Get headers for optional Streamable HTTP GET SSE."""
        headers = self._get_auth_headers()
        headers["Accept"] = "text/event-stream"
        if self._mcp_session_id:
            headers["mcp-session-id"] = self._mcp_session_id
        return headers
    
    async def _cleanup(self) -> None:
        """Clean up resources."""
        if self._request_task and not self._request_task.done():
            self._request_task.cancel()
            try:
                await self._request_task
            except asyncio.CancelledError:
                pass
        self._request_task = None
        # Close HTTP session
        if self.session and not getattr(self.session, "closed", False):
            await self.session.close()
            self.session = None
        self._initialized = False
        self._mcp_session_id = None
        try:
            await self.stream_bridge.shutdown()
        except Exception:
            pass  # best-effort cleanup on transport stop
        # Terminate subprocess if we started it
        if self._process and self._process.returncode is None:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, self._process.terminate)
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                await loop.run_in_executor(None, self._process.kill)
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self.logger.warning(
                        "HTTP server subprocess wait timed out after kill",
                        service_id=self.service_id,
                    )
            
            self.logger.info(
                "HTTP server subprocess terminated",
                service_id=self.service_id
            )

