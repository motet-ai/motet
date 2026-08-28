"""
Motet - Chat API

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Chat API for the Motet distributed framework.
    Provides REST API endpoint for chat completions with streaming support,
    including deterministic artifact RAG scope/source controls.
    Also provides WebSocket endpoint for real-time bidirectional chat communication.
    Budget-stop Continue (issue #188): ``stop_reason`` on non-stream responses and
    SSE ``end``; optional ``continue_after_budget`` starts a fresh-budget new turn.
    SSE disconnect cancels the live task only when the stream did not
    reach a clean terminal (``end`` or ``suspended``).

Dependencies:
    - fastapi: Web framework for REST API
    - motet.core: MotetStack and chat functionality

Usage:
    from motet.interfaces.api.v1.chat import router
    
    # Include in FastAPI app
    app.include_router(router)

Notes:
    - Provides chat completion endpoint with streaming support (SSE)
    - Provides WebSocket endpoint for real-time bidirectional communication
    - Supports conversation history and session management
    - Uses unified authentication (JWT, service accounts, headers)
    - Part of Phase 2: API Organization and URL Standardization
    - Streaming (SSE and WebSocket): event JSON may include optional ``agent_id`` (qualified
      registry id for the turn, e.g. ``core.default``) on tokens, lifecycle, tool/reasoning,
      thinking, and error frames. Nested loops also carry ``parent_agent_id``.
    - Inbound messages may include optional per-message ``agent_id`` so Motet-aware
      clients can round-trip transcript provenance into prepare_context merge (issue #138)
"""

import json
from typing import List, Optional, Any, Dict, AsyncGenerator, Callable, Union, cast
from fastapi import APIRouter, HTTPException, Header, Body, Request, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import AliasChoices, BaseModel, Field
import structlog

from ..shared import (
    apply_principal_to_config,
    attach_principal_to_stack,
    get_current_principal,
)
from ..shared.identity import get_principal_context
from ....core.conversations.ownership import (
    ACCESS_DENIED_MESSAGE,
    is_conversation_access_denied,
    require_not_owned_by_other_sync,
)
from ....core.orchestration.turn.budget_continue import (
    CONTINUE_AFTER_BUDGET_USER_MESSAGE,
)
from ....core.types import Principal, Message as CoreMessage

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


_STREAM_AGENT_FIELD_KEYS = ("agent_id", "parent_agent_id")


def _attach_stream_agent_fields(ev: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    """Copy orchestrator task-stream agent_id / parent_agent_id onto a JSON body."""
    out = dict(body)
    for key in _STREAM_AGENT_FIELD_KEYS:
        val = ev.get(key)
        if val:
            out[key] = val
    return out


def _stream_event_body_with_agent_id(ev: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    """Attach orchestrator task-stream agent ids to JSON bodies when present."""
    return _attach_stream_agent_fields(ev, body)


def _orchestrator_data_with_agent_id(ev: Dict[str, Any]) -> Dict[str, Any]:
    """Merge top-level agent ids into the `data` object for tool/reasoning SSE frames."""
    raw = ev.get("data")
    out: Dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    return _attach_stream_agent_fields(ev, out)


# Orchestrator stream events whose SSE/WS payload is `ev["data"]` plus top-level agent_id (ADR-0083).
_ORCHESTRATOR_DATA_EVENTS = frozenset(
    {
        "reasoning_step",
        "workflow_step",
        "reasoning_meta",
        "reasoning",
        "conversation_analyzed",
        "tool_execution_started",
        "tool_execution_completed",
        "tool_execution_failed",
    }
)

# Orchestrator lifecycle events forwarded as flat JSON body (+ optional agent_id).
_ORCHESTRATOR_LIFECYCLE_EVENTS = frozenset(
    {
        "agent_turn_start",
        "agent_turn_complete",
    }
)


def _sse_line(event: str, data: Dict[str, Any]) -> bytes:
    """Single Server-Sent Event frame (event + JSON data line)."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")


def _sse_orchestrator_line(ev: Dict[str, Any], sse_event: str) -> bytes:
    """SSE frame for orchestrator events backed by merged `data` + agent_id."""
    try:
        return _sse_line(sse_event, _orchestrator_data_with_agent_id(ev))
    except Exception:
        return _sse_line(sse_event, {})


def _sse_stream_body_line(ev: Dict[str, Any], sse_event: str, body: Dict[str, Any]) -> bytes:
    """SSE frame using `_stream_event_body_with_agent_id` (flat JSON body)."""
    try:
        return _sse_line(sse_event, _stream_event_body_with_agent_id(ev, body))
    except Exception:
        return _sse_line(sse_event, {})


async def _ws_send_with_agent(ws: WebSocket, ev: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """WebSocket JSON with optional top-level agent_id merged from the orchestrator event."""
    await ws.send_json(_stream_event_body_with_agent_id(ev, payload))


async def _ws_send_orchestrator_event(ws: WebSocket, ev: Dict[str, Any], name: str) -> None:
    """WebSocket message shaped like ``{event, data, agent_id?}`` for tool/reasoning frames."""
    await _ws_send_with_agent(ws, ev, {"event": name, "data": ev.get("data")})


class Message(BaseModel):
    """Chat message model.

    Optional ``agent_id`` lets Motet-aware clients round-trip assistant provenance
    from conversation history so prepare_context merge can dedupe
    strictly against stored transcript copies (issue #138).
    """
    role: str = Field(..., description="Message role", json_schema_extra={"example": "user"})
    content: str = Field(..., description="Message content", json_schema_extra={"example": "What is AI?"})
    attachments: Optional[List[Dict[str, Any]]] = Field(None, description="List of UploadAttachment objects")
    agent_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional qualified registry id of the agent that authored this message "
            "(assistant/tool turns). Round-trip from conversation history when available "
            "so transcript merge can match stored provenance."
        ),
        json_schema_extra={"example": "core.default"},
    )
    parent_agent_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional qualified registry id of the agent that started this loop when "
            "the author is nested (for example a spawn child)."
        ),
        json_schema_extra={"example": "core.default"},
    )


def _to_core_messages(messages: List[Message]) -> List[CoreMessage]:
    """Map inbound API messages to canonical core Message (preserves agent ids)."""
    out: List[CoreMessage] = []
    for m in messages:
        kwargs: Dict[str, Any] = {
            "role": m.role,
            "content": m.content,
        }
        if m.attachments:
            kwargs["attachments"] = m.attachments
        aid = (m.agent_id or "").strip() if m.agent_id else ""
        if aid:
            kwargs["agent_id"] = aid
        parent = (m.parent_agent_id or "").strip() if m.parent_agent_id else ""
        if parent:
            kwargs["parent_agent_id"] = parent
        out.append(CoreMessage(**kwargs))
    return out


class ChatRequest(BaseModel):
    """Request model for chat completion."""
    messages: List[Message] = Field(
        ...,
        description=(
            'List of chat messages. Each message may include optional agent_id (qualified registry id) to round-trip assistant provenance.'
        ),
    )
    stream: bool = Field(default=False, description="Enable streaming response via SSE", json_schema_extra={"example": False})
    overrides: Optional[Dict[str, Any]] = Field(None, description="Config overrides for this request")
    conversation_id: Optional[str] = Field(None, description="Conversation ID for session history", json_schema_extra={"example": "conv-123"})
    context: Optional[Dict[str, Any]] = Field(None, description="Additional context for the chat", json_schema_extra={"example": {"user_id": "user123"}})
    artifact_rag_scope: Optional[str] = Field(
        default=None,
        description="Optional RAG retrieval scope: conversation, principal, or motet. Broader scopes require authorization.",
    )
    artifact_ids: Optional[List[str]] = Field(
        default=None,
        description="Optional artifact IDs to restrict artifact RAG retrieval to.",
    )
    artifact_tags: Optional[List[str]] = Field(
        default=None,
        description="Optional artifact tags to narrow artifact RAG retrieval within the selected scope.",
    )
    artifact_collection_id: Optional[str] = Field(
        default=None,
        description="Optional artifact collection identifier for future collection-scoped RAG retrieval.",
    )
    allow_broader_artifact_rag_scope: bool = Field(
        default=False,
        description="Server/UI-authorized flag allowing principal or motet RAG scope for this request.",
    )
    agent_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("agent_id", "persona"),
        description="Agent to use (e.g. 'default', 'motet_admin'). Resolved via registry; unknown IDs return 404.",
        json_schema_extra={"example": "default"},
    )
    surface_id: Optional[str] = Field(
        default=None,
        description="Surface/channel for conversation scoping; e.g. 'demo_chat', 'ops_dashboard'. Passed to registry on register.",
        json_schema_extra={"example": "demo_chat"},
    )
    prefilled_tool_calls: Optional[Union[Dict[str, Any], List[Dict[str, Any]]]] = Field(
        default=None,
        description=(
            "Caller-supplied first tool call(s) for a deterministic turn. When set, the agent loop synthesizes and executes these tool call(s) as the turn's first action instead of a planning model call, then continues normally. Each entry is {tool_name, arguments}; multiple entries execute in parallel. A single object is accepted and coerced to a one-element list. Must respect the agent's tool filter; unknown or excluded tools fail the turn."
        ),
        json_schema_extra={
            "example": [
                {
                    "tool_name": "workflow_memo.archivist_assessment",
                    "arguments": {"asset_id": "asset-123", "artifact_id": "artifact-456"},
                }
            ]
        },
    )
    continue_after_budget: bool = Field(
        default=False,
        description=(
            "Issue #188: structured Continue after a prior turn stopped with "
            "stop_reason max_iterations or max_model_calls. Starts a **new** agent "
            "turn with a fresh iteration/model-call budget (not a same-budget resume). "
            "When true, ensures the last user message is the typed continue text "
            f"({CONTINUE_AFTER_BUDGET_USER_MESSAGE!r}). Requires conversation_id."
        ),
        json_schema_extra={"example": False},
    )


class ChatResponse(BaseModel):
    """Response model for chat completion."""
    content: str = Field(..., description="Chat completion content", json_schema_extra={"example": "AI stands for Artificial Intelligence..."})
    citations: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Optional artifact RAG citation metadata for retrieved chunks used during the turn",
    )
    stop_reason: Optional[str] = Field(
        default=None,
        description=(
            'How the agent turn ended (issue #188). Budget stops use max_iterations or max_model_calls — clients should offer Continue (continue_after_budget or the typed continue message). Suspended is a handback (same budget on resume), not Continue.'
        ),
        json_schema_extra={"example": "max_iterations"},
    )


@router.post(
    "",
    summary="Chat completion",
    description=(
        'Chat completion with optional Server-Sent Events (SSE) when ``stream=true``. Each inbound message may include optional **agent_id** (qualified registry id) to round-trip assistant provenance from conversation history. Stream event JSON bodies may include an optional **agent_id** field (qualified agent id, e.g. ``core.default``) on turn, token, end, tool, reasoning, thinking, and error events, matching the orchestrator task stream. Nested loops also include ``parent_agent_id``.'
    ),
    response_description="Chat completion or SSE stream",
)
async def chat(
    request: Request,
    req: ChatRequest = Body(...),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    role: Optional[str] = Header(default=None, alias="X-Role"),
    principal: Principal = Depends(get_current_principal)
):
    """
    Chat completion.
    
    Provides chat completions with support for:
    - Non-streaming responses (default)
    - Streaming responses via Server-Sent Events (SSE) when stream=true
    - Conversation history via conversation_id
    - Planner mode for complex multi-step reasoning
    - Config overrides for request-specific settings
    - Role-based access control via X-Role header
    
    **Streaming Mode:**
    When stream=true, returns an SSE stream with real-time updates including:
    - Token generation events
    - Reasoning step events (if planner mode enabled)
    - Tool execution events
    
    Stream **data** lines are JSON. When the server attributes the underlying task stream, frames
    may include **agent_id**: the resolved qualified registry id for the agent handling the turn
    (e.g. ``core.default``, ``core.motet_admin``). It appears in the JSON for ``turn`` (with
    ``state``), ``token`` (with ``t``), ``thinking``, ``error``, ``end``, and inside the payload
    for tool/reasoning-style events that carry a ``data`` object. Omitted when unknown.
    
    **Session Management:**
    Provide conversation_id to:
    - Retrieve previous conversation history automatically
    - Store new messages in the session
    - Enable memory tagging for recall
    
    Args:
        req: Chat request with messages and options
        request: FastAPI request object for identity/tenant info
        x_api_key: API key for authentication (optional, JWT/service account can be used instead)
        role: Optional role for RBAC (from X-Role header)
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        ChatResponse with content (non-streaming) or StreamingResponse (streaming)
        
    Raises:
        HTTPException: 401 if authentication fails, 500 if chat fails
    """
    
    try:
        from ....core import MotetStack
        from ....core.config import Config
        from ....core.observability import trace_store as tracing
        import time as _t
        import asyncio

        # Issue #139: a conversation_id is a capability. Reject one owned by a
        # different principal before doing any work, because the streaming
        # branch below returns a StreamingResponse whose status cannot change
        # once headers are sent. The command layer still binds and re-checks.
        if req.conversation_id:
            _motet_id, _tenant_id, _principal_id = get_principal_context(principal)
            try:
                await asyncio.to_thread(
                    require_not_owned_by_other_sync,
                    motet_id=_motet_id,
                    tenant_id=_tenant_id,
                    principal_id=_principal_id,
                    conversation_id=req.conversation_id,
                )
            except Exception as e:
                if is_conversation_access_denied(e):
                    raise HTTPException(status_code=403, detail=ACCESS_DENIED_MESSAGE)
                raise

        cfg = Config()
        apply_principal_to_config(cfg, principal)
        stack = MotetStack(cfg)
        attach_principal_to_stack(stack, principal)
        
        logger.info(
            "chat_request",
            stream=req.stream,
            num_messages=len(req.messages),
            continue_after_budget=bool(req.continue_after_budget),
        )

        # Keep API as thin dispatcher: root command resolves agent config/tools/auth.
        # Map to canonical Message so optional per-message agent_id survives into
        # prepare_context merge / Celery command payloads (issue #138).
        msgs = _to_core_messages(req.messages)
        if req.continue_after_budget:
            # Issue #188: structured Continue = new turn + fresh budget (not resume).
            if not (req.conversation_id or "").strip():
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "continue_after_budget requires conversation_id so the "
                        "new turn continues the same conversation"
                    ),
                )
            last = msgs[-1] if msgs else None
            last_content = (
                str(getattr(last, "content", "") or "").strip() if last else ""
            )
            if (
                last is None
                or getattr(last, "role", None) != "user"
                or last_content != CONTINUE_AFTER_BUDGET_USER_MESSAGE
            ):
                msgs = list(msgs) + [
                    CoreMessage(
                        role="user",
                        content=CONTINUE_AFTER_BUDGET_USER_MESSAGE,
                    )
                ]
        principal_roles = list(getattr(principal, "roles", []) or [])
        if role:
            principal_roles.append(role)
        # Preserve order while de-duplicating.
        principal_roles = list(dict.fromkeys([r for r in principal_roles if r]))

        # Resolve aliases / empty → qualified id (e.g. None/"default" → core.default)
        # so agent_turn Inputs and conversation registry always show a concrete agent.
        from ....core.agents import resolve_agent_id

        raw_agent_id = getattr(req, "agent_id", None)
        resolved_agent_id = resolve_agent_id(raw_agent_id)
        conv_id = req.conversation_id
        chat_context = {
            "agent_id": resolved_agent_id,
            "conversation_id": conv_id,
            "principal_roles": principal_roles,
            "role": role,
            # Workers boot with their own MOTET_MEMORY_AGENT_SCOPE_MODE; stamp the
            # API Config mode so prepare_context / hybrid_retrieve honor caller policy.
            "memory_agent_scope_mode": str(
                getattr(cfg, "memory_agent_scope_mode", "prefer") or "prefer"
            ),
        }
        if req.continue_after_budget:
            # Issue #188: agent_turn rehydrates budget_continue checkpoint (fresh budget).
            chat_context["continue_after_budget"] = True
        if req.surface_id:
            # ADR-0083 / surfaces catalog: validate surface exists and agent may use it.
            # Explicit register only — chat does not auto-create catalog entries.
            try:
                from ....core.surfaces import SurfaceRegistry, agent_may_use_surface
                from ..shared.surfaces import require_catalog_surface

                surface_reg = SurfaceRegistry()
                surface_id = require_catalog_surface(
                    req.surface_id, registry=surface_reg
                )

                config_surfaces = None
                try:
                    from ....core.agents import get_agent_registry

                    cfg = get_agent_registry().get(resolved_agent_id)
                    if cfg is not None:
                        raw_surfaces = getattr(cfg, "allowed_surface_ids", None)
                        if isinstance(raw_surfaces, list):
                            config_surfaces = list(raw_surfaces)
                except Exception as e:
                    logger.warning(
                        "chat_surface_agent_config_lookup_failed",
                        agent_id=resolved_agent_id,
                        error=str(e),
                    )

                if not agent_may_use_surface(
                    qualified_agent_id=resolved_agent_id,
                    surface_id=surface_id,
                    config_allowed_surface_ids=config_surfaces,
                    registry=surface_reg,
                ):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Agent {resolved_agent_id} is not allowed on surface "
                            f"{surface_id}"
                        ),
                    )
                chat_context["surface_id"] = surface_id
            except HTTPException:
                raise
            except Exception as e:
                logger.error(
                    "chat_surface_validation_failed",
                    surface_id=req.surface_id,
                    error=str(e),
                    exc_info=True,
                )
                raise HTTPException(
                    status_code=500,
                    detail=f"Surface validation failed: {e}",
                ) from e
        if req.artifact_rag_scope:
            chat_context["artifact_rag_scope"] = req.artifact_rag_scope
        if req.artifact_ids:
            chat_context["artifact_ids"] = list(req.artifact_ids)
        if req.artifact_tags:
            chat_context["artifact_tags"] = list(req.artifact_tags)
        if req.artifact_collection_id:
            chat_context["artifact_collection_id"] = req.artifact_collection_id
        if req.allow_broader_artifact_rag_scope:
            chat_context["allow_broader_artifact_rag_scope"] = True
        if req.prefilled_tool_calls:
            # ADR-0111: forward caller-supplied first tool call(s) so the agent loop can
            # skip the planning model call on deterministic turns. Normalize a single
            # object to a one-element list for the downstream context contract.
            prefilled = req.prefilled_tool_calls
            chat_context["prefilled_tool_calls"] = (
                prefilled if isinstance(prefilled, list) else [prefilled]
            )
        if req.context:
            for key, value in req.context.items():
                chat_context.setdefault(key, value)
        if req.overrides:
            # Ensure agent_turn receives UI-selected model routing/tuning values.
            for key in (
                "model_provider",
                "model_name",
                "model_profile_name",
                "enable_thinking",
                "reasoning_effort",
            ):
                if key in req.overrides:
                    chat_context[key] = req.overrides.get(key)
        
        # Get trace ID from request
        trace_id = getattr(request.state, "trace_id", None) if request else None
        
        # Set role on orchestrator for this request
        if getattr(stack, "orchestrator", None) and role:
            stack.orchestrator.set_role(role)
        
        # Non-streaming response
        if not req.stream:
            _t0 = _t.perf_counter()

            # Apply config overrides if provided (keep parity with streaming path).
            _restore: Optional[Callable[[], None]] = None
            if req.overrides:
                try:
                    allowed = {
                        "pii_allowlist",
                        "model_provider",
                        "model_name",
                        "model_profile_name",
                        "enable_thinking",
                        "reasoning_effort",
                    }
                    prev = {}
                    for k, v in req.overrides.items():
                        if k in allowed and hasattr(stack.config, k):
                            prev[k] = getattr(stack.config, k, None)
                            setattr(stack.config, k, v)

                    def _undo_config_overrides() -> None:
                        for k, val in prev.items():
                            if hasattr(stack.config, k):
                                setattr(stack.config, k, val)

                    _restore = _undo_config_overrides
                except Exception as e:
                    logger.debug("Config override failed", error=str(e))
            
            try:
                setattr(stack, "_current_conversation_id", conv_id)
            except Exception as e:
                logger.debug("Conversation ID hint failed", error=str(e))
            
            _t1 = _t.perf_counter()
            logger.info("chat_prepared", ms=round((_t1 - _t0)*1000, 2), num_msgs=len(msgs))
            
            try:
                resp = await stack.chat(msgs, context=chat_context)
            finally:
                if _restore:
                    try:
                        _restore()
                    except Exception:
                        pass  # best-effort config restore

            _t2 = _t.perf_counter()
            logger.info("chat_model_done", ms=round((_t2 - _t1)*1000, 2), total_ms=round((_t2 - _t0)*1000, 2))
            logger.info("chat_response", content_len=len(resp.content))
            
            # Record trace event
            try:
                if trace_id:
                    tracing.record_event(trace_id, {"kind": "chat_final", "content_len": len(resp.content)})
            except Exception as e:
                logger.debug("Tracing failed", error=str(e))
            
            # Note: Session history persistence would require app.state access
            # This is simplified for the standalone API
            
            raw_response = resp.raw if isinstance(resp.raw, dict) else {}
            citations = raw_response.get("artifact_rag_citations")
            stop_reason = raw_response.get("stop_reason")
            return ChatResponse(
                content=resp.content,
                citations=citations if isinstance(citations, list) else None,
                stop_reason=str(stop_reason) if stop_reason else None,
            )
        
        # Streaming response via SSE
        try:
            setattr(stack, "_current_conversation_id", conv_id)
        except Exception as e:
            logger.debug("Conversation ID hint failed for streaming", error=str(e))
        
        async def event_stream() -> AsyncGenerator[bytes, None]:
            """Generate SSE stream for chat response using orchestrator streaming."""
            # Apply config overrides if provided (matching old endpoint behavior)
            _restore: Optional[Callable[[], None]] = None
            if req.overrides:
                try:
                    allowed = {
                        "pii_allowlist",
                        "model_provider",
                        "model_name",
                        "model_profile_name",
                        "enable_thinking",
                        "reasoning_effort",
                    }
                    prev = {}
                    for k, v in req.overrides.items():
                        if k in allowed and hasattr(stack.config, k):
                            prev[k] = getattr(stack.config, k, None)
                            setattr(stack.config, k, v)
                    
                    def _undo_config_overrides() -> None:
                        for k, val in prev.items():
                            if hasattr(stack.config, k):
                                setattr(stack.config, k, val)

                    _restore = _undo_config_overrides
                except Exception as e:
                    logger.debug("Config override failed", error=str(e))
            
            task_ref: Dict[str, Any] = {"id": None}
            # Clean terminal: end (success) or suspended (ADR-0127 handback).
            # error leaves this False so finally cancels orphans; disconnect too.
            stream_clean_terminal = False
            try:
                if getattr(stack, "orchestrator", None):
                    # Propagate identity and trace (use verified principal from JWT)
                    try:
                        attach_principal_to_stack(stack, principal)
                        setattr(
                            stack,
                            "_current_trace_id",
                            getattr(request.state, "trace_id", None) if request else None
                        )
                    except Exception as e:
                        logger.debug("Identity/trace propagation failed", error=str(e))
                    
                    stream_iter = stack.orchestrator.stream_events(stack, msgs, context=chat_context)
                    
                    assistant_buf_parts: list[str] = []
                    
                    async for ev in stream_iter:
                        # Capture task_id for filtering
                        try:
                            if ev.get("event") == "task_created" and ev.get("task_id"):
                                task_ref["id"] = ev.get("task_id")
                        except Exception:
                            pass  # non-critical task_id capture
                        
                        # SSE framing - match old endpoint format
                        if ev.get("event") == "turn":
                            try:
                                yield _sse_stream_body_line(ev, "turn", {"state": ev.get("state")})
                            except Exception:
                                pass  # non-critical SSE frame; skip turn event
                        elif ev.get("event") == "step":
                            yield f"event: step\ndata: {ev.get('data')}\n\n".encode("utf-8")
                        elif ev.get("event") in _ORCHESTRATOR_DATA_EVENTS:
                            yield _sse_orchestrator_line(ev, ev["event"])
                        elif ev.get("event") in _ORCHESTRATOR_LIFECYCLE_EVENTS:
                            lifecycle_body = {
                                k: v
                                for k, v in ev.items()
                                if k not in {"event", "agent_id", "parent_agent_id"}
                            }
                            yield _sse_stream_body_line(ev, ev["event"], lifecycle_body)
                        elif ev.get("event") == "thinking":
                            # ADR-0064: Model reasoning/thinking traces (o1/o3, Claude extended thinking, etc.)
                            yield _sse_stream_body_line(
                                ev,
                                "thinking",
                                {"text": ev.get("text", ""), "is_complete": ev.get("is_complete", False)},
                            )
                        elif ev.get("event") == "auth_required":
                            # ADR-0057: OAuth authorization required for MCP service
                            try:
                                # Extract auth data from event (keys are at top level, not in 'data')
                                auth_data = {k: v for k, v in ev.items() if k != "event"}
                                yield f"event: auth_required\ndata: {json.dumps(auth_data)}\n\n".encode("utf-8")
                            except Exception:
                                yield f"event: auth_required\ndata: {{}}\n\n".encode("utf-8")
                        elif ev.get("event") == "token":
                            tok = ev.get('data')
                            if isinstance(tok, str):
                                try:
                                    assistant_buf_parts.append(tok)
                                except Exception:
                                    pass  # buffer append optional; stream continues
                            try:
                                yield _sse_line("token", _stream_event_body_with_agent_id(ev, {"t": tok}))
                            except Exception:
                                yield f"event: token\ndata: {tok}\n\n".encode("utf-8")
                        elif ev.get("event") == "error":
                            # Surface orchestrator/worker errors to the client (previously dropped, causing empty SSE body).
                            try:
                                err_payload = _stream_event_body_with_agent_id(
                                    ev,
                                    {
                                        "error": ev.get("error")
                                        or (ev.get("data") or {}).get("error")
                                        or (ev.get("data") or {}).get("message")
                                        or "unknown error"
                                    },
                                )
                                yield _sse_line("error", err_payload)
                            except Exception:
                                yield _sse_line("error", {"error": "unknown error"})
                            # Not a clean terminal — finally may cancel orphans.
                        elif ev.get("event") == "suspended":
                            # ADR-0127: handback / external wait — terminal for this
                            # stream but the task must stay alive for resume.
                            try:
                                suspend_body = {
                                    k: v
                                    for k, v in ev.items()
                                    if k not in {"event", "agent_id", "parent_agent_id"}
                                }
                                if task_ref["id"]:
                                    suspend_body["task_id"] = task_ref["id"]
                                yield _sse_stream_body_line(ev, "suspended", suspend_body)
                            except Exception:
                                yield b"event: suspended\ndata: {}\n\n"
                            stream_clean_terminal = True
                        elif ev.get("event") == "end":
                            stream_clean_terminal = True
                            # Include optional metadata on end
                            try:
                                payload = {k: v for k, v in ev.items() if k != "event"}
                                if task_ref["id"]:
                                    payload["task_id"] = task_ref["id"]
                                yield f"event: end\ndata: {json.dumps(payload)}\n\n".encode("utf-8")
                            except Exception:
                                yield b"event: end\n\n"
                else:
                    # No orchestrator - fall back to simple response
                    resp = await stack.chat(msgs, context=chat_context)
                    
                    payload = {"content": resp.content, "done": True}
                    yield f"event: message\ndata: {json.dumps(payload)}\n\n".encode("utf-8")
                    
                    # Send end event to signal completion
                    end_payload = {"task_id": getattr(request.state, "trace_id", None) if request else None}
                    yield f"event: end\ndata: {json.dumps(end_payload)}\n\n".encode("utf-8")
                    stream_clean_terminal = True
                    
            except Exception as e:
                logger.error("Streaming failed", error=str(e), exc_info=True)
                error_payload = {"error": str(e)}
                yield f"event: error\ndata: {json.dumps(error_payload)}\n\n".encode("utf-8")
            finally:
                # ADR-0131: client disconnect / aborted or error stream → cancel.
                # Do not cancel after clean end or ADR-0127 suspended (resume).
                if task_ref.get("id") and not stream_clean_terminal:
                    try:
                        from motet.core.distributed.task_control import request_task_cancel

                        _cancel_tenant = (
                            get_principal_context(principal)[1] if principal else None
                        )
                        request_task_cancel(
                            task_ref["id"],
                            reason="client_disconnect",
                            principal_id=getattr(principal, "id", None) if principal else None,
                            source="sse_disconnect",
                            tenant_id=_cancel_tenant,
                        )
                    except Exception as cancel_err:
                        logger.warning(
                            "chat_sse_disconnect_cancel_failed",
                            task_id=task_ref.get("id"),
                            error=str(cancel_err),
                        )
                # Restore config overrides
                if _restore:
                    try:
                        _restore()
                    except Exception as e:
                        logger.debug("Config restore failed", error=str(e))
        
        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        msg = str(e)
        if "Unknown agent:" in msg:
            raise HTTPException(status_code=404, detail=msg)
        if "Role not authorized for agent" in msg:
            raise HTTPException(status_code=403, detail=msg)
        if is_conversation_access_denied(e):
            # Issue #139: cross-principal conversation_id must not succeed.
            raise HTTPException(status_code=403, detail=ACCESS_DENIED_MESSAGE)
        logger.error("Chat failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Chat failed: {str(e)}")


@router.websocket("/ws")
async def ws_chat(ws: WebSocket):
    """
    WebSocket endpoint for real-time chat.

    Provides bidirectional communication for chat completions with streaming support.
    Supports the same features as the REST chat endpoint but via WebSocket protocol.

    **Authentication:**
    - Supports JWT (Bearer token in Authorization header)
    - Supports service account tokens (sa_* prefix)
    - Supports header-based auth in dev mode (X-Principal-Id, X-Tenant-Id)
    - Legacy: API key in message payload (deprecated, use headers instead)

    **Message Format:**
    ```json
    {
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": true,
    "role": "assistant",
    "conversation_id": "conv-123"
    }
    ```

    **Response Events:**
    - `{"event": "step", "data": {...}}` - Reasoning step (optional top-level `agent_id`)
    - `{"token": "..."}` - Streaming token (optional top-level `agent_id`)
    - `{"event": "agent_turn_start",...}` - Agent turn lifecycle start (optional `agent_id`)
    - `{"event": "agent_turn_complete",...}` - Agent turn lifecycle complete (optional `agent_id`)
    - `{"event": "end"}` - Completion (optional `agent_id`)
    - `{"content": "..."}` - Non-streaming response
    - `{"error": "..."}` - Error message
    - When streaming, messages may include **agent_id** (qualified id) on any of the above shapes
    for turn/tool/reasoning/thinking events, same semantics as the HTTP SSE stream.

    Args:
        ws: WebSocket connection

    Raises:
        WebSocketDisconnect: When client disconnects
    """
    from ....core import MotetStack
    from ....core.config import Config
    from ....core.security import extract_principal, require_api_key
    from ....core.security import RateLimiter
    
    await ws.accept()
    
    try:
        cfg = Config()
        
        # Extract principal from WebSocket headers
        # WebSocket headers are accessed via ws.headers (case-insensitive dict-like)
        # Create a Request-like object for extract_principal
        class WebSocketRequest:
            def __init__(self, ws_headers):
                # Convert WebSocket headers to dict with get method
                # extract_principal uses request.headers.get(), so headers must be a dict-like object
                self.headers = {}
                for key, value in ws_headers.items():
                    # Store both original case and lowercase for compatibility
                    self.headers[key] = value
                    self.headers[key.lower()] = value
        
        # Get headers from WebSocket
        ws_headers_dict = {}
        for key, value in ws.headers.items():
            ws_headers_dict[key] = value
        
        ws_request = WebSocketRequest(ws_headers_dict)
        # Shim only provides .headers; cast for extract_principal (same header access path as HTTP).
        principal = extract_principal(cfg, cast(Request, ws_request))
        if principal:
            apply_principal_to_config(cfg, principal)
        
        # If no principal and API key is configured, check for API key in headers
        api_key = None
        if not principal and cfg.api_key:
            api_key = ws.headers.get("X-API-Key") or ws.headers.get("x-api-key")
            if not api_key:
                # Try Authorization header
                auth_header = ws.headers.get("Authorization") or ws.headers.get("authorization", "")
                if auth_header.startswith("Bearer "):
                    # For API key in Bearer format, we'll check it in the message payload
                    pass
                else:
                    api_key = auth_header
            
            if api_key and api_key != cfg.api_key:
                await ws.send_json({"error": "invalid api key"})
                await ws.close(code=4401)
                return
            if api_key:
                require_api_key(cfg, api_key)
        
        # Rate limiting
        rate_limiter = RateLimiter(
            backend=cfg.rate_limit_backend,
            redis_url=cfg.redis_url if cfg.rate_limit_backend == "redis" else None,
            limit_per_minute=cfg.rate_limit_per_minute,
        )
        rate_limiter.rate_limit(principal.id if principal else (api_key or "public"))
        
        # Initialize stack
        stack = MotetStack(cfg)
        if principal:
            attach_principal_to_stack(stack, principal)
        
        while True:
            data = await ws.receive_json()
            
            # Legacy: Check API key in message payload (deprecated)
            if cfg.api_key and data.get("api_key"):
                if data.get("api_key") != cfg.api_key:
                    await ws.send_json({"error": "invalid api key"})
                    await ws.close(code=4401)
                    return
                rate_limiter.rate_limit(data.get("api_key") or "public")
            
            # Parse messages
            messages = []
            for m in data.get("messages", []):
                msg = CoreMessage(**m)
                # Ensure attachments are preserved in metadata if present in message payload but not in metadata
                if "attachments" in m and m["attachments"]:
                    if not msg.metadata:
                        msg.metadata = {}
                    msg.metadata["attachments"] = m["attachments"]
                messages.append(msg)
            
            stream = bool(data.get("stream", True))
            
            # Set role on orchestrator for this websocket session
            if getattr(stack, "orchestrator", None):
                stack.orchestrator.set_role(data.get("role"))
            
            # Handle streaming mode
            if getattr(stack, "orchestrator", None) and stream:
                async for ev in stack.orchestrator.stream_events(stack, messages):
                    ev_name = ev.get("event")
                    if ev_name == "step":
                        await _ws_send_with_agent(ws, ev, {"event": "step", "data": ev.get("data")})
                    elif ev_name == "token":
                        await _ws_send_with_agent(ws, ev, {"token": ev.get("data")})
                    elif ev_name == "end":
                        await _ws_send_with_agent(ws, ev, {"event": "end"})
                    elif ev_name == "suspended":
                        # ADR-0127 handback — terminal for this stream; no cancel here.
                        suspend_payload = {
                            k: v
                            for k, v in ev.items()
                            if k not in {"event", "agent_id", "parent_agent_id"}
                        }
                        await _ws_send_with_agent(
                            ws,
                            ev,
                            {"event": "suspended", "data": suspend_payload},
                        )
                    elif ev_name == "plan":
                        await _ws_send_with_agent(ws, ev, {"event": "plan", "names": ev.get("names", [])})
                    elif ev_name in _ORCHESTRATOR_DATA_EVENTS:
                        await _ws_send_orchestrator_event(ws, ev, ev["event"])
                    elif ev_name in _ORCHESTRATOR_LIFECYCLE_EVENTS:
                        lifecycle_payload = {
                            k: v
                            for k, v in ev.items()
                            if k not in {"event", "agent_id", "parent_agent_id"}
                        }
                        await _ws_send_with_agent(
                            ws,
                            ev,
                            {"event": ev_name, "data": lifecycle_payload},
                        )
                    elif ev_name == "thinking":
                        await _ws_send_with_agent(
                            ws,
                            ev,
                            {
                                "event": "thinking",
                                "data": {"text": ev.get("text", ""), "is_complete": ev.get("is_complete", False)},
                            },
                        )
            elif stream:
                # Orchestrator is mandatory for streaming
                await ws.send_json({"event": "error", "error": "orchestrator disabled"})
            else:
                # Non-streaming mode
                resp = await stack.chat(messages)
                await ws.send_json({"content": resp.content})
                
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
        return
    except Exception as e:
        logger.error("WebSocket chat error", error=str(e), exc_info=True)
        try:
            await ws.send_json({"error": str(e)})
            await ws.close(code=1011)  # Internal error
        except Exception:
            pass  # best-effort error send; client may be gone
        return

