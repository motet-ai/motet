"""
Motet - Tools API

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Tool management API for the Motet distributed framework.
    Provides REST API endpoints for listing, describing, and executing tools.
    Maps unknown-tool failures from the distributed invoker to HTTP 404 (same
    contract as the local registry fallback and other resource APIs).

Dependencies:
    - fastapi: Web framework for REST API
    - motet.core.tools: Tool registry and execution
    - motet.core.commands.builtin.tool: Distributed tool commands

Usage:
    from motet.interfaces.api.v1.tools import router
    
    # Include in FastAPI app
    app.include_router(router)

Notes:
    - Provides tool listing, description, and execution
    - Integrates with distributed tool system
    - Part of Phase 2: API Organization and URL Standardization
    - /execute stamps tenant_id / principal_id / motet_id from the authenticated
      principal onto the distributed command (same as memories/commands APIs)
    - Unknown tools return HTTP 404 whether the failure comes from the nested
      invoker envelope or the local registry fallback
"""

from typing import Dict, List, Optional, Any
from fastapi import APIRouter, HTTPException, Header, Body, Request, Depends
from pydantic import BaseModel, Field
import structlog
import asyncio
from uuid import uuid4

from ..shared.auth import get_current_principal
from ..shared.identity import get_principal_context
from ....core.types import Principal

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/tools", tags=["tools"])


def _invoker_error_message(result: Any) -> Optional[str]:
    """
    Extract an error message from ``global_invoker.execute_command`` output.

    The invoker often returns HTTP-transport success as
    ``{"status": "completed", "result": {ADR-0029 error envelope}}``. Callers
    must inspect the nested ADR-0029 payload, not only the outer status.
    """
    if not isinstance(result, dict):
        return None

    if result.get("status") == "error":
        err = result.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("detail")
            return str(msg) if msg else str(err)
        if err is not None:
            return str(err)

    inner = result.get("result")
    if isinstance(inner, dict) and inner.get("status") == "error":
        err = inner.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("detail")
            return str(msg) if msg else str(err)
        if err is not None:
            return str(err)
        return "Unknown error"

    return None


def _http_exception_for_tool_error(error_msg: str) -> HTTPException:
    """Map tool-execution failure text to HTTP status (404 unknown tool, else 500)."""
    lower = (error_msg or "").lower()
    if "tool not found" in lower or "not found in registry" in lower:
        return HTTPException(status_code=404, detail="tool not found")
    return HTTPException(status_code=500, detail=error_msg or "Tool execution failed")


class ToolRequest(BaseModel):
    """Request model for tool execution."""
    name: str = Field(
        ...,
        description="Tool name to execute",
        json_schema_extra={"example": "search"}
    )
    params: Dict[str, Any] = Field(
        ...,
        description="Tool parameters",
        json_schema_extra={"example": {"query": "motet ai stack", "max_results": 10}}
    )


@router.get(
    "",
    summary="List available tools",
    description="Get list of all available tools with their schemas (requires authentication)",
    response_description="Dictionary of tools with descriptions and schemas"
)
async def list_tools(
    principal: Principal = Depends(get_current_principal)
):
    """
    List all available tools.
    
    Uses distributed tool listing to get tools from worker processes.
    Falls back to local tool registry if distributed listing fails.
    
    Returns a dictionary mapping tool names to their descriptions and schemas.
    Tool schemas are JSON Schema representations of the tool's parameters.
    
    Args:
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        Dictionary of tools: {tool_name: {description, schema}}
        
    Raises:
        HTTPException: 401 if authentication fails
    """
    
    # Use distributed tool listing to get tools from worker processes
    try:
        from motet.core.commands.builtin.tool import tool_list, ToolListData
        from ....core.workers import global_invoker
        
        logger.debug("Executing distributed tool list command")
        
        # Create distributed tool list command using decorated function
        command_data = ToolListData()
        tool_list_factory: Any = tool_list
        command = tool_list_factory(
            task_id=str(uuid4()),
            conversation_id="",
            data=command_data,
            tenant_id="default",
            principal_id="",
            trace_id=None,
            timeout_seconds=30,
            priority=5,
            max_retries=2
        )
        
        # Execute via distributed system (timeout so we fall back to local when no workers)
        result = await asyncio.wait_for(
            asyncio.to_thread(global_invoker.execute_command, command),
            timeout=3.0,
        )
        logger.debug("Distributed tool list result", result_status=result.get("status"))
        
        # Check for errors (ADR-0029 response format)
        if result.get("status") == "error":
            logger.warning("Distributed tool list failed, falling back to local", error=result.get("error"))
            # Fallback to local tool registry
        else:
            # global_invoker returns {status, result: ADR-0029, ...}
            # where ADR-0029 = {status: "completed", data: {...}}
            adr_response = result.get("result", {})
            response_data = adr_response.get("data", {})
            tools_list = response_data.get("tools", [])
            logger.info("Found tools from distributed list", tool_count=len(tools_list))
            out = {}
            for tool in tools_list:
                name = tool.get("name", "")
                description = tool.get("description", "")
                schema = tool.get("schema")
                out[name] = {"description": description, "schema": schema}
            return out
            
    except Exception as e:
        logger.warning("Exception in distributed tool list, falling back to local", error=str(e))
        # Fallback to local tool registry
    
    # Fallback: Use local tool registry
    try:
        from ....core.tools import registry as tools
        
        # Preserve legacy dict shape for backwards compatibility (registry uses list_items())
        out = {}
        for name, rt in tools.list_items().items():
            schema = None
            if getattr(rt, "tool_schema", None):
                try:
                    schema = rt.tool_schema.model_json_schema()  # type: ignore[attr-defined]
                except Exception as e:
                    logger.debug("Failed to get tool schema", tool=name, error=str(e))
            out[name] = {"description": rt.description, "schema": schema}
        return out
        
    except Exception as e:
        logger.error("Failed to list tools", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list tools: {str(e)}")


@router.get(
    "/describe",
    summary="Describe tools",
    description="Get detailed descriptions of all available tools (requires authentication)",
    response_description="List of tool descriptions"
)
async def describe_tools(
    principal: Principal = Depends(get_current_principal)
):
    """
    Get detailed descriptions of all available tools.
    
    Returns a simpler format than /tools, focusing on tool descriptions
    without full schemas. Useful for displaying tools to users.
    
    Args:
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        List of tool descriptions
        
    Raises:
        HTTPException: 401 if authentication fails
    """
    
    try:
        from ....core.tools import registry as tools
        return tools.describe()  # type: ignore[attr-defined]
    except Exception as e:
        logger.warning("Failed to describe tools", error=str(e))
        return []


@router.post(
    "/execute",
    summary="Execute a tool",
    description="Execute a tool with given parameters (requires authentication)",
    response_description="Tool execution result"
)
async def execute_tool(
    req: ToolRequest = Body(...),
    role: Optional[str] = Header(default=None, alias="X-Role"),
    principal: Principal = Depends(get_current_principal)
):
    """
    Execute a tool with the given parameters.
    
    Uses distributed tool execution to run tools on worker processes.
    Falls back to local execution if distributed execution fails.
    
    Supports role-based access control via X-Role header.
    Tool allow/deny lists can be configured per deployment.
    
    Args:
        req: Tool execution request with tool name and parameters
        role: Optional role for RBAC (from X-Role header)
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        Tool execution result (format depends on tool)
        
    Raises:
        HTTPException: 401 if auth fails, 404 if tool not found, 500 if execution fails
    """
    
    # Use distributed tool execution for all tools
    try:
        from motet.core.commands.builtin.tool import tool_execution, ToolExecutionData
        from ....core.workers import global_invoker
        
        # Create tool execution data for decorated command
        data = ToolExecutionData(
            tool_name=req.name,
            parameters=req.params,
            conversation_history=None,
            reasoning_task=None
        )
        
        # Stamp identity onto the command — without this the worker runs as
        # tenant=default / principal="" and principal-scoped memory/tools miss.
        motet_id, tenant_id, principal_id = get_principal_context(principal)
        tool_execution_factory: Any = tool_execution
        command = tool_execution_factory(
            task_id=str(uuid4()),
            conversation_id="",
            data=data,
            motet_id=motet_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
        )
        
        # Execute via distributed system (timeout so we fall back when no workers)
        result = await asyncio.wait_for(
            asyncio.to_thread(global_invoker.execute_command, command),
            timeout=10.0,
        )

        # Map invoker / ADR-0029 errors to HTTP (unknown tool → 404).
        error_msg = _invoker_error_message(result)
        if error_msg is not None:
            raise _http_exception_for_tool_error(error_msg)

        # Return the result (already rehydrated by global_invoker)
        return result

    except HTTPException:
        raise
    except (asyncio.TimeoutError, Exception) as e:
        if isinstance(e, asyncio.TimeoutError):
            logger.warning("Distributed tool execution timed out, falling back to local", tool=req.name)
        else:
            logger.warning("Distributed tool execution failed, falling back to local", tool=req.name, error=str(e))

        # Fallback to local execution for non-distributed tools
        try:
            from ....core.tools import registry as tools
            from ....core.config import Config

            cfg = Config()
            allow = set(t.strip() for t in (cfg.tool_allowlist or "").split(",") if t.strip()) if cfg.tool_allowlist else None
            deny = set(t.strip() for t in (cfg.tool_denylist or "").split(",") if t.strip()) if cfg.tool_denylist else None

            result = await asyncio.to_thread(
                tools.execute, req.name, req.params, allow=allow, deny=deny, role=role
            )
            if result.get("error") == "tool not found":
                raise HTTPException(status_code=404, detail="tool not found")
            return result

        except HTTPException:
            raise
        except Exception as e2:
            logger.error("Local tool execution failed", tool=req.name, error=str(e2), exc_info=True)
            raise HTTPException(status_code=500, detail=f"Tool execution failed: {str(e2)}")

