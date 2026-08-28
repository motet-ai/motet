"""
Motet - OpenAI Compatible API Routes

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    HTTP routes for the OpenAI-compatible facade: ``/models``,
    ``/chat/completions``, and ``/responses``.

    This router is mounted at a drop-in prefix (``/v1`` by default) rather than
    ``/api/v1``, a documented exception to, because OpenAI clients
    hard-code the path suffix and only expose a base URL. The router carries no
    prefix of its own so the mount point stays configurable; interfaces/http.py
    supplies it.

    Requests authenticate with existing Motet credentials, preferably ``sa_*``
    service account tokens, which also carry the facade policy that decides
    execution mode, model access, and optional force_thinking.

    When thinking is enabled (client opt-in or policy ``force_thinking``) and the
    model has ``CAP_REASONING``, streams emit Chat Completions
    ``reasoning_content`` deltas and Responses ``reasoning`` output items
    (summary text only).

Dependencies:
    - motet.interfaces.api.shared.auth: shared principal resolution
    - motet.core.security.facade_policy: per-credential mode, allowlist, force_thinking
      -.execution: passthrough / hosted_tools / agent backends
      -.translation: OpenAI <-> canonical conversion

Usage:
    from motet.interfaces.api.openai_compat import router

    app.include_router(router, prefix="/v1")

Notes:
    - Mode selection precedence is service account, then model alias, then request
    - Header-derived dev-mode principals are rejected on all facade routes (§11e)
    - Every response carries correlation headers joining client ids to Motet ids
    - Streaming bodies emit keepalives and a documented mid-stream error frame
    - Tool-call arguments stream as they are generated on both endpoints: as
      `delta.tool_calls` fragments on chat.completions (dropped from the terminal
      chunk, which is an increment) and as function_call item + argument delta
      events on /responses (kept in the terminal snapshot) — §5f
    - Agent-mode replies carry a visible session banner naming the conversation,
      and are fingerprinted on success, so stateless clients that resend the
      transcript with no session reference rejoin the conversation. Inbound banners are stripped before the agent runs, and
      the unstripped transcript is kept on the context for fingerprinting
    - Budget stops (issue #188) expose ``X-Motet-Stop-Reason`` on non-streaming
      replies and append a Continue tip before the session banner (streaming and
      non-streaming). Continue is a new turn with a fresh budget, not resume.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import structlog
from fastapi import APIRouter, Body, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ....core.config import Config
from ....core.orchestration.turn.budget_continue import (
    STOP_REASON_HEADER,
    budget_continue_tip,
    is_budget_stop,
)
from ....core.security.facade_policy import (
    FacadeMode,
    FacadePolicy,
    parse_facade_mode,
    resolve_facade_policy,
)
from ....core.types import Message, Principal
from ..shared.auth import get_current_principal
from . import execution, sessions, streaming, translation
from .errors import FacadeError, error_payload
from .wire import (
    ChatCompletionRequest,
    ResponsesRequest,
    new_completion_id,
    new_response_id,
    now_ts,
)

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["openai-compat"])


# ---------------------------------------------------------------------------
# Request preparation
# ---------------------------------------------------------------------------


def _split_model_alias(model: str) -> Tuple[str, Optional[FacadeMode]]:
    """Split a ``model:mode`` alias into its parts.

    A suffix alias is the only mode selector that survives every client, since
    model pickers accept arbitrary strings while custom headers do not exist in
    UIs like Cursor (ADR-0125 §5c).
    """
    if ":" not in model:
        return model, None
    base, _, suffix = model.rpartition(":")
    mode = parse_facade_mode(suffix)
    if mode is None:
        return model, None
    return base, mode


def _resolve_mode(
    *,
    alias_mode: Optional[FacadeMode],
    requested_mode: Optional[FacadeMode],
    policy: FacadePolicy,
    cfg: Any,
) -> FacadeMode:
    """Apply mode precedence and the credential ceiling."""
    explicit = None
    if requested_mode is not None:
        if not bool(getattr(cfg, "openai_compat_allow_request_mode_override", False)):
            raise FacadeError(
                400,
                "request-level facade mode override is disabled; bind the mode to the credential",
                code="mode_override_disabled",
            )
        explicit = requested_mode
    elif alias_mode is not None:
        explicit = alias_mode

    if explicit is None:
        return policy.mode
    if not policy.permits_mode(explicit):
        raise FacadeError(
            403,
            f"credential is not permitted to run in '{explicit.value}' mode",
            error_type="permission_error",
            code="mode_not_permitted",
        )
    return explicit


def _reject_insecure_principal(principal: Principal) -> None:
    """Reject dev-mode header identities on facade routes (ADR-0125 §11e).

    Facade policy (mode ceiling, model allowlist) hangs off the credential, so
    an unauthenticated ``X-Principal-Id`` header must not mint one — even when
    ``allow_insecure_principal_headers`` is on for local development of the
    native API.
    """
    claims = getattr(principal, "claims", None) or {}
    if str(claims.get("type") or "") == "header":
        raise FacadeError(
            401,
            "facade routes require a service account token or JWT; "
            "header-based dev identities are not accepted",
            error_type="authentication_error",
            code="insecure_auth_rejected",
        )


def _resolve_agent_id(
    req: ChatCompletionRequest,
    mode: FacadeMode,
    policy: FacadePolicy,
) -> Optional[str]:
    """Request ``motet_agent_id``, else SA/config policy in agent mode, else None.

    Precedence: request body extension, then credential ``agent_id`` (resolved
    from the service account or ``MOTET_OPENAI_COMPAT_DEFAULT_AGENT_ID``).
    """
    requested = (getattr(req, "motet_agent_id", None) or "").strip()
    if requested:
        return requested
    if mode is FacadeMode.AGENT:
        bound = (policy.agent_id or "").strip()
        return bound or None
    return None


def _requested_mode(req: ChatCompletionRequest, header_mode: Optional[str]) -> Optional[FacadeMode]:
    """Parse an explicit mode request, rejecting values that parse to nothing.

    Silently ignoring a typo like ``motet_mode: "agnt"`` would run the request
    in a different mode than the caller asked for (§5f: no accept-and-ignore).
    """
    raw = str(req.motet_mode or header_mode or "").strip()
    if not raw:
        return None
    mode = parse_facade_mode(raw)
    if mode is None:
        raise FacadeError(
            400,
            f"unknown facade mode '{raw}'; expected one of: "
            + ", ".join(m.value for m in FacadeMode),
            code="invalid_facade_mode",
            param="motet_mode",
        )
    return mode


async def _prepare(
    req: ChatCompletionRequest,
    request: Request,
    principal: Principal,
    *,
    header_mode: Optional[str],
    header_conversation_id: Optional[str],
) -> execution.FacadeContext:
    """Validate, authorize, and translate one facade request."""
    cfg = Config()
    _reject_insecure_principal(principal)
    policy = resolve_facade_policy(principal, cfg)

    translation.validate_supported(req)

    model_base, alias_mode = _split_model_alias(str(req.model or ""))
    mode = _resolve_mode(
        alias_mode=alias_mode,
        requested_mode=_requested_mode(req, header_mode),
        policy=policy,
        cfg=cfg,
    )

    provider, registry_key, spec = translation.resolve_model(model_base, policy)
    messages = translation.messages_to_canonical(req)
    tools = translation.tools_to_canonical(req)
    output_contract = translation.output_contract_from_request(req)

    if mode is FacadeMode.PASSTHROUGH:
        # Deeper modes may satisfy structured output or tools through the agent
        # stack, but passthrough hands the request straight to the model.
        translation.capability_check(
            spec, needs_tools=bool(tools), needs_structured=bool(output_contract)
        )

    resolved = await sessions.resolve_conversation(
        req,
        principal,
        cfg,
        header_conversation_id=header_conversation_id,
        messages=messages,
        # Only agent mode carries memory across turns, so only agent mode
        # benefits from rejoining a conversation a stateless client never
        # named (ADR-0125 §5d banner and transcript inference).
        infer_from_transcript=(mode is FacadeMode.AGENT),
    )
    conversation_id = resolved.conversation_id
    if mode is FacadeMode.AGENT:
        # Memory-bearing mode: fail a cross-principal conversation id here with
        # a clean 404 instead of a mid-stream error frame (§11f). Core still
        # enforces ownership authoritatively inside agent_turn. A banner echoed
        # back by the client is a caller-supplied id like any other, so a
        # hand-edited one cannot reach another principal's conversation.
        await sessions.ensure_conversation_access(conversation_id, principal)

    # The transcript keeps its banners for fingerprinting; the model gets a copy
    # without them, plus the guard line asking it not to drop one it rewrites.
    raw_messages = messages
    if mode is FacadeMode.AGENT:
        messages = sessions.strip_session_banners(messages)
        if sessions.banner_mode(cfg) != "off" and sessions.banner_guard_enabled(cfg):
            messages = _with_banner_guard(messages)

    return execution.FacadeContext(
        mode=mode,
        policy=policy,
        principal=principal,
        cfg=cfg,
        provider=provider,
        registry_key=registry_key,
        spec=spec,
        model_id=translation.facade_model_id(provider, registry_key),
        messages=messages,
        raw_messages=raw_messages,
        conversation_is_new=resolved.is_new,
        model_settings=translation.model_settings_from_request(
            req,
            provider=provider,
            registry_key=registry_key,
            spec=spec,
            force_thinking=policy.force_thinking,
            force_thinking_effort=policy.force_thinking_effort,
        ),
        conversation_id=conversation_id,
        trace_id=getattr(request.state, "trace_id", None) if request else None,
        tools=tools,
        output_contract=output_contract,
        agent_id=_resolve_agent_id(req, mode, policy),
    )


def _correlation_headers(
    ctx: execution.FacadeContext,
    *,
    stop_reason: Optional[str] = None,
) -> Dict[str, str]:
    """Headers that let an operator join a client request to Motet records."""
    headers = {
        "X-Motet-Task-Id": ctx.task_id,
        "X-Motet-Conversation-Id": ctx.conversation_id,
        "X-Motet-Facade-Mode": ctx.mode.value,
        "X-Motet-Model": ctx.model_id,
    }
    if ctx.trace_id:
        headers["X-Trace-Id"] = ctx.trace_id
    if stop_reason:
        headers[STOP_REASON_HEADER] = str(stop_reason)
    return headers


def _with_banner_guard(messages: List[Message]) -> List[Message]:
    """Prepend the banner-preservation instruction as a system message.

    Placed ahead of the client's own system prompt so a client that ends its
    instructions with something absolute ("output only JSON") does not read as
    overriding it.
    """
    return [Message(role="system", content=sessions.banner_guard_instruction()), *messages]


def _session_banner(ctx: execution.FacadeContext, *, has_tool_calls: bool) -> str:
    """Banner to append to this turn's reply, or "" when it does not apply.

    Skipped for tool-call turns: that assistant message is a call request the
    client answers with tool results, not a reply anyone reads, and appending
    prose to it would show up as stray text in the client's tool UI. The next
    turn in the same conversation carries the banner instead.
    """
    if ctx.mode is not FacadeMode.AGENT or has_tool_calls:
        return ""
    mode = sessions.banner_mode(ctx.cfg)
    if mode == "off" or (mode == "first" and not ctx.conversation_is_new):
        return ""
    return sessions.build_session_banner(ctx.conversation_id)


def _budget_continue_tip_delta(result: Dict[str, Any]) -> str:
    """Return the Continue tip for a budget stop, or "" if already present / N/A."""
    stop_reason = result.get("stop_reason")
    if not is_budget_stop(str(stop_reason) if stop_reason else None):
        return ""
    tip = budget_continue_tip(str(stop_reason))
    body = str(result.get("content") or "")
    if tip.strip() in body:
        return ""
    return tip


def _apply_session_banner(
    ctx: execution.FacadeContext, result: Dict[str, Any]
) -> Dict[str, Any]:
    """Return *result* with budget-continue tip + session banner appended.

    Tip precedes the banner so ``_BANNER_RE`` (end-anchored) still matches.
    """
    out = result
    tip = _budget_continue_tip_delta(out)
    if tip:
        out = {**out, "content": f"{(out.get('content') or '').rstrip()}{tip}"}
    banner = _session_banner(ctx, has_tool_calls=bool(out.get("tool_calls_canonical")))
    if not banner:
        return out
    return {**out, "content": f"{out.get('content') or ''}{banner}"}


async def _remember_turn_transcript(
    ctx: execution.FacadeContext, result: Dict[str, Any]
) -> None:
    """Fingerprint this turn so a stateless client's next request rejoins it.

    Agent mode only: other modes carry no cross-turn memory, so there is
    nothing for the next turn to rejoin. Uses ctx.conversation_id as it stands
    after execution — a resumed turn has already been rebound to the
    checkpoint's conversation by then (ADR-0127).

    Hashes ``transcript_messages`` (banners intact) and expects *result* to
    carry the banner too: the fingerprint has to describe the transcript the
    client will send back, not the cleaned copy the model saw.
    """
    if ctx.mode is not FacadeMode.AGENT:
        return
    await sessions.remember_transcript(
        ctx.transcript_messages, result, ctx.conversation_id, ctx.principal, ctx.cfg
    )


def _publish_facade_event(ctx: execution.FacadeContext, *, route: str, outcome: str) -> None:
    """Emit a facade-level event so this traffic is visible in ops surfaces."""
    try:
        from ....core.workers import global_bus

        global_bus.publish(
            {
                "kind": "openai_compat_request",
                "source": "openai_compat_facade",
                "route": route,
                "outcome": outcome,
                "mode": ctx.mode.value,
                "model": ctx.model_id,
                "task_id": ctx.task_id,
                "conversation_id": ctx.conversation_id,
                "tenant_id": ctx.tenant_id,
                "principal_id": ctx.principal_id,
            }
        )
    except Exception as exc:
        logger.debug("openai_compat_event_publish_failed", error=str(exc))


def _facade_error_response(exc: FacadeError, *, route: str) -> JSONResponse:
    logger.warning(
        "openai_compat_request_rejected",
        route=route,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
    )
    return JSONResponse(status_code=exc.status_code, content=exc.to_payload())


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@router.get(
    "/models",
    summary="List models",
    description=(
        "List models this credential may use, in OpenAI models format. The list is "
        "filtered by the credential's allowlist so a client's model picker never shows "
        "a model the key cannot call."
    ),
    response_description="OpenAI-shaped model list",
    responses={
        200: {"description": "Models the credential may use"},
        401: {"description": "Authentication required"},
    },
)
async def list_models(principal: Principal = Depends(get_current_principal)) -> Any:
    """List allowlisted models in OpenAI format."""
    cfg = Config()
    try:
        _reject_insecure_principal(principal)
    except FacadeError as exc:
        return _facade_error_response(exc, route="models.list")
    policy = resolve_facade_policy(principal, cfg)
    entries = translation.allowed_models(policy)
    logger.info(
        "openai_compat_models_listed",
        count=len(entries),
        tenant_id=getattr(principal, "tenant_id", None),
        allowlist_source=policy.allowlist_source,
    )
    return {
        "object": "list",
        "data": [translation.model_card(provider, key, spec) for provider, key, spec in entries],
    }


@router.get(
    "/models/{model_id:path}",
    summary="Retrieve model",
    description="Retrieve one model by facade id, subject to the credential's allowlist.",
    response_description="OpenAI-shaped model object",
    responses={
        200: {"description": "Model metadata"},
        404: {"description": "Model not found or not allowlisted"},
    },
)
async def retrieve_model(
    model_id: str,
    principal: Principal = Depends(get_current_principal),
) -> Any:
    """Retrieve a single model card."""
    cfg = Config()
    try:
        _reject_insecure_principal(principal)
        policy = resolve_facade_policy(principal, cfg)
        base, _ = _split_model_alias(model_id)
        provider, registry_key, spec = translation.resolve_model(base, policy)
    except FacadeError as exc:
        return _facade_error_response(exc, route="models.retrieve")
    return translation.model_card(provider, registry_key, spec)


# ---------------------------------------------------------------------------
# Chat completions
# ---------------------------------------------------------------------------


async def _chat_stream_body(
    ctx: execution.FacadeContext,
    *,
    completion_id: str,
    include_usage: bool,
) -> AsyncGenerator[bytes, None]:
    """Render a chat.completion.chunk stream for one facade request."""
    created = now_ts()
    yield streaming.sse_data(
        translation.completion_chunk(
            completion_id=completion_id,
            model_id=ctx.model_id,
            delta={"role": "assistant", "content": ""},
            created=created,
        )
    )

    collected: List[str] = []
    thinking_parts: List[str] = []
    result: Dict[str, Any] = {}
    streamed_call_indices: Dict[str, int] = {}
    try:
        async for kind, value in execution.stream(ctx):
            if kind == "delta":
                collected.append(value)
                yield streaming.sse_data(
                    translation.completion_chunk(
                        completion_id=completion_id,
                        model_id=ctx.model_id,
                        delta={"content": value},
                        created=created,
                    )
                )
            elif kind == "tool_call_delta":
                yield streaming.sse_data(
                    translation.completion_chunk(
                        completion_id=completion_id,
                        model_id=ctx.model_id,
                        delta={
                            "tool_calls": [
                                translation.tool_call_delta_to_openai(
                                    value, streamed_call_indices
                                )
                            ]
                        },
                        created=created,
                    )
                )
            elif kind == "thinking":
                text = ""
                if isinstance(value, dict):
                    text = str(value.get("text") or "")
                if text:
                    thinking_parts.append(text)
                    yield streaming.sse_data(
                        translation.completion_chunk(
                            completion_id=completion_id,
                            model_id=ctx.model_id,
                            delta={"reasoning_content": text},
                            created=created,
                        )
                    )
            else:
                result = value
    except FacadeError as exc:
        _publish_facade_event(ctx, route="chat.completions", outcome="error")
        yield streaming.sse_error(exc.message, error_type=exc.error_type, code=exc.code)
        return
    except Exception as exc:
        logger.error(
            "openai_compat_stream_failed",
            task_id=ctx.task_id,
            error=str(exc),
            exc_info=True,
        )
        _publish_facade_event(ctx, route="chat.completions", outcome="error")
        yield streaming.sse_error("stream failed", error_type="api_error")
        return

    tool_calls = translation.tool_calls_to_openai(result)
    tip = _budget_continue_tip_delta(result)
    if tip:
        yield streaming.sse_data(
            translation.completion_chunk(
                completion_id=completion_id,
                model_id=ctx.model_id,
                delta={"content": tip},
                created=created,
            )
        )
        result = {
            **result,
            "content": f"{(result.get('content') or '').rstrip()}{tip}",
        }
    banner = _session_banner(ctx, has_tool_calls=bool(tool_calls))
    if banner:
        yield streaming.sse_data(
            translation.completion_chunk(
                completion_id=completion_id,
                model_id=ctx.model_id,
                delta={"content": banner},
                created=created,
            )
        )

    # Calls whose fragments already went out are complete on the client side;
    # resending them here would double their arguments.
    remaining = translation.tool_calls_not_yet_streamed(
        tool_calls, streamed_call_indices, task_id=ctx.task_id
    )
    if remaining:
        yield streaming.sse_data(
            translation.completion_chunk(
                completion_id=completion_id,
                model_id=ctx.model_id,
                delta={"tool_calls": remaining},
                created=created,
            )
        )

    yield streaming.sse_data(
        translation.completion_chunk(
            completion_id=completion_id,
            model_id=ctx.model_id,
            delta={},
            finish_reason=translation.finish_reason_from_result(
                result, has_tool_calls=bool(tool_calls)
            ),
            created=created,
        )
    )

    if include_usage:
        yield streaming.sse_data(
            translation.usage_chunk(
                completion_id=completion_id,
                model_id=ctx.model_id,
                result=result,
                created=created,
            )
        )

    if thinking_parts and not result.get("reasoning_content"):
        result = {**result, "reasoning_content": "".join(thinking_parts)}

    # Record the chatcmpl id like /responses records resp ids: a hybrid client
    # may chain previous_response_id off either endpoint's id, and refusing an
    # id we minted ourselves would be a self-inflicted 404 (§5d step 3).
    await sessions.remember_response(completion_id, ctx.conversation_id, ctx.principal, ctx.cfg)
    # Fingerprint against the text the client actually received: the echoed
    # assistant message next turn is the streamed text plus tip/banner, not the
    # raw result.
    await _remember_turn_transcript(
        ctx,
        {
            **result,
            "content": f"{result.get('content') or ''.join(collected)}{banner}",
        },
    )
    _publish_facade_event(ctx, route="chat.completions", outcome="success")
    yield streaming.DONE_SENTINEL


@router.post(
    "/chat/completions",
    summary="Create chat completion",
    description=(
        "OpenAI Chat Completions endpoint backed by Motet. Supports streaming via "
        "Server-Sent Events and accepts Responses-shaped bodies for clients such as "
        "Cursor that post an 'input' field to this path."
    ),
    response_description="chat.completion object, or an SSE stream of chat.completion.chunk",
    response_model=None,
    responses={
        200: {"description": "Completion or SSE stream"},
        400: {"description": "Unsupported or malformed request"},
        401: {"description": "Authentication required"},
        403: {"description": "Mode not permitted for this credential"},
        404: {"description": "Model not found or not allowlisted"},
    },
)
async def create_chat_completion(
    request: Request,
    req: ChatCompletionRequest = Body(...),
    x_motet_facade_mode: Optional[str] = Header(default=None, alias="X-Motet-Facade-Mode"),
    x_motet_conversation_id: Optional[str] = Header(default=None, alias="X-Motet-Conversation-Id"),
    principal: Principal = Depends(get_current_principal),
) -> Any:
    """Create a chat completion in the credential's facade mode."""
    try:
        ctx = await _prepare(
            req,
            request,
            principal,
            header_mode=x_motet_facade_mode,
            header_conversation_id=x_motet_conversation_id,
        )
    except FacadeError as exc:
        return _facade_error_response(exc, route="chat.completions")

    logger.info(
        "openai_compat_chat_request",
        mode=ctx.mode.value,
        model=ctx.model_id,
        stream=req.stream,
        message_count=len(ctx.messages),
        tool_count=len(ctx.tools or []),
        task_id=ctx.task_id,
        conversation_id=ctx.conversation_id,
        tenant_id=ctx.tenant_id,
        principal_id=ctx.principal_id,
    )

    completion_id = new_completion_id()
    headers = _correlation_headers(ctx)

    if req.stream:
        include_usage = bool(req.stream_options and req.stream_options.include_usage)
        body = streaming.with_keepalive(
            _chat_stream_body(ctx, completion_id=completion_id, include_usage=include_usage),
            float(getattr(ctx.cfg, "openai_compat_stream_keepalive_seconds", 15) or 0),
        )
        # Streaming cannot revise headers after the body starts; clients read
        # budget stops from the Continue tip in content (issue #188).
        return StreamingResponse(
            body,
            media_type="text/event-stream",
            headers={**streaming.SSE_HEADERS, **headers},
        )

    try:
        result = await execution.run(ctx)
    except FacadeError as exc:
        _publish_facade_event(ctx, route="chat.completions", outcome="error")
        return _facade_error_response(exc, route="chat.completions")
    except Exception as exc:
        logger.error(
            "openai_compat_chat_failed",
            task_id=ctx.task_id,
            error=str(exc),
            exc_info=True,
        )
        _publish_facade_event(ctx, route="chat.completions", outcome="error")
        return JSONResponse(
            status_code=502,
            content=error_payload("inference failed", error_type="api_error"),
        )

    # Same correlation as /responses: the returned chatcmpl id must be able to
    # continue this conversation via previous_response_id (§5d step 3).
    result = _apply_session_banner(ctx, result)
    stop_reason = result.get("stop_reason")
    if stop_reason:
        headers = _correlation_headers(ctx, stop_reason=str(stop_reason))
    await sessions.remember_response(completion_id, ctx.conversation_id, principal, ctx.cfg)
    await _remember_turn_transcript(ctx, result)
    _publish_facade_event(ctx, route="chat.completions", outcome="success")
    payload = translation.completion_payload(
        result, model_id=ctx.model_id, completion_id=completion_id
    )
    return JSONResponse(content=payload, headers=headers)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


def _responses_envelope(ctx: execution.FacadeContext, response_id: str, status: str) -> Dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": now_ts(),
        "status": status,
        "model": ctx.model_id,
        "output": [],
        "output_text": "",
    }


async def _responses_stream_body(
    ctx: execution.FacadeContext,
    *,
    response_id: str,
) -> AsyncGenerator[bytes, None]:
    """Render a Responses API event stream for one facade request.

    Item scaffolding is delayed until the content that needs it arrives, and
    output indices are handed out in arrival order, so reasoning precedes the
    message and function calls follow it — the ordering OpenAI Responses clients
    expect and the same order the final snapshot lists them in.
    """
    from .wire import new_message_id

    sequence = 0

    def frame(event_type: str, payload: Dict[str, Any]) -> bytes:
        nonlocal sequence
        body = {"type": event_type, "sequence_number": sequence, **payload}
        sequence += 1
        return streaming.sse_named(event_type, body)

    yield frame("response.created", {"response": _responses_envelope(ctx, response_id, "in_progress")})

    collected: List[str] = []
    thinking_parts: List[str] = []
    result: Dict[str, Any] = {}
    reasoning_item_id: Optional[str] = None
    reasoning_output_index = 0
    reasoning_open = False
    message_item_id: Optional[str] = None
    message_output_index = 0
    message_open = False
    # Where this message item's text starts in `collected`, so a second item
    # opened after a function call does not repeat the first item's text.
    message_text_start = 0
    next_output_index = 0
    # call_id -> {item_id, output_index, tool_name, arguments}
    function_calls: Dict[str, Dict[str, Any]] = {}

    def _take_output_index() -> int:
        nonlocal next_output_index
        index = next_output_index
        next_output_index += 1
        return index

    def _close_reasoning() -> List[bytes]:
        nonlocal reasoning_open
        if not reasoning_open or reasoning_item_id is None:
            return []
        text = "".join(thinking_parts)
        frames = [
            frame(
                "response.reasoning_summary_text.done",
                {
                    "item_id": reasoning_item_id,
                    "output_index": reasoning_output_index,
                    "summary_index": 0,
                    "text": text,
                },
            ),
            frame(
                "response.output_item.done",
                {
                    "output_index": reasoning_output_index,
                    "item": {
                        "type": "reasoning",
                        "id": reasoning_item_id,
                        "status": "completed",
                        "summary": [{"type": "summary_text", "text": text}],
                    },
                },
            ),
        ]
        reasoning_open = False
        return frames

    def _open_message() -> List[bytes]:
        nonlocal message_item_id, message_output_index, message_open, message_text_start
        if message_open:
            return []
        message_item_id = new_message_id()
        message_output_index = _take_output_index()
        message_text_start = len(collected)
        message_open = True
        return [
            frame(
                "response.output_item.added",
                {
                    "output_index": message_output_index,
                    "item": {
                        "type": "message",
                        "id": message_item_id,
                        "status": "in_progress",
                        "role": "assistant",
                        "content": [],
                    },
                },
            ),
            frame(
                "response.content_part.added",
                {
                    "item_id": message_item_id,
                    "output_index": message_output_index,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                },
            ),
        ]

    def _close_message(final_text: Optional[str] = None) -> List[bytes]:
        """Close the open message item.

        Mid-stream (a function call is starting) only the streamed text exists.
        At the end the result's content wins, since a non-token-streaming turn
        carries its answer there — but only for the first message item, because
        the result holds the turn's whole text and a later item owns just its own.
        """
        nonlocal message_open
        if not message_open or message_item_id is None:
            return []
        streamed = "".join(collected[message_text_start:])
        text = streamed if final_text is None or message_text_start else final_text
        frames = [
            frame(
                "response.output_text.done",
                {
                    "item_id": message_item_id,
                    "output_index": message_output_index,
                    "content_index": 0,
                    "text": text,
                },
            ),
            frame(
                "response.content_part.done",
                {
                    "item_id": message_item_id,
                    "output_index": message_output_index,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": text, "annotations": []},
                },
            ),
            frame(
                "response.output_item.done",
                {
                    "output_index": message_output_index,
                    "item": {
                        "type": "message",
                        "id": message_item_id,
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text, "annotations": []}],
                    },
                },
            ),
        ]
        message_open = False
        return frames

    def _function_call_fragment(value: Dict[str, Any]) -> List[bytes]:
        """Scaffold a function_call item on first sight, then stream its arguments."""
        call_id = str(value.get("call_id") or "")
        fragment = str(value.get("arguments_delta") or "")
        frames: List[bytes] = []
        state = function_calls.get(call_id)
        if state is None:
            tool_name = str(value.get("tool_name") or "")
            state = {
                "item_id": f"fc_{call_id}",
                "output_index": _take_output_index(),
                "tool_name": tool_name,
                "arguments": "",
            }
            function_calls[call_id] = state
            frames.append(
                frame(
                    "response.output_item.added",
                    {
                        "output_index": state["output_index"],
                        "item": translation.function_call_item(call_id, tool_name),
                    },
                )
            )
        if fragment:
            state["arguments"] = str(state["arguments"]) + fragment
            frames.append(
                frame(
                    "response.function_call_arguments.delta",
                    {
                        "item_id": state["item_id"],
                        "output_index": state["output_index"],
                        "delta": fragment,
                    },
                )
            )
        return frames

    def _close_function_calls() -> List[bytes]:
        frames: List[bytes] = []
        for call_id, state in function_calls.items():
            arguments = str(state["arguments"])
            frames.append(
                frame(
                    "response.function_call_arguments.done",
                    {
                        "item_id": state["item_id"],
                        "output_index": state["output_index"],
                        "arguments": arguments,
                    },
                )
            )
            frames.append(
                frame(
                    "response.output_item.done",
                    {
                        "output_index": state["output_index"],
                        "item": translation.function_call_item(
                            call_id,
                            str(state["tool_name"]),
                            arguments=arguments,
                            status="completed",
                        ),
                    },
                )
            )
        return frames

    try:
        async for kind, value in execution.stream(ctx):
            if kind == "thinking":
                text = ""
                if isinstance(value, dict):
                    text = str(value.get("text") or "")
                if text:
                    thinking_parts.append(text)
                # Thinking after the assistant message has started is accumulated
                # for the final payload only — OpenAI ordering expects reasoning first.
                if message_open:
                    continue
                if not text and not (isinstance(value, dict) and value.get("is_complete")):
                    continue
                if not reasoning_open:
                    reasoning_item_id = f"rs_{new_message_id().removeprefix('msg_')}"
                    reasoning_output_index = _take_output_index()
                    reasoning_open = True
                    yield frame(
                        "response.output_item.added",
                        {
                            "output_index": reasoning_output_index,
                            "item": {
                                "type": "reasoning",
                                "id": reasoning_item_id,
                                "status": "in_progress",
                                "summary": [],
                            },
                        },
                    )
                if text:
                    yield frame(
                        "response.reasoning_summary_text.delta",
                        {
                            "item_id": reasoning_item_id,
                            "output_index": reasoning_output_index,
                            "summary_index": 0,
                            "delta": text,
                        },
                    )
                if isinstance(value, dict) and value.get("is_complete"):
                    for closed in _close_reasoning():
                        yield closed
            elif kind == "delta":
                if reasoning_open:
                    for closed in _close_reasoning():
                        yield closed
                for opened in _open_message():
                    yield opened
                collected.append(value)
                yield frame(
                    "response.output_text.delta",
                    {
                        "item_id": message_item_id,
                        "output_index": message_output_index,
                        "content_index": 0,
                        "delta": value,
                    },
                )
            elif kind == "tool_call_delta":
                # An output item must be done before the next one starts, so any
                # open reasoning or message item closes first.
                for closed in _close_reasoning():
                    yield closed
                for closed in _close_message():
                    yield closed
                for opened in _function_call_fragment(value):
                    yield opened
            else:
                result = value
    except FacadeError as exc:
        _publish_facade_event(ctx, route="responses", outcome="error")
        yield frame("error", exc.to_payload()["error"])
        return
    except Exception as exc:
        logger.error(
            "openai_compat_responses_stream_failed",
            task_id=ctx.task_id,
            error=str(exc),
            exc_info=True,
        )
        _publish_facade_event(ctx, route="responses", outcome="error")
        yield frame("error", error_payload("stream failed", error_type="api_error")["error"])
        return

    if reasoning_open:
        for closed in _close_reasoning():
            yield closed

    text = result.get("content") or "".join(collected)
    # Always close with a message item even when empty, unless the turn's output
    # was function calls: OpenAI emits no message item for those, and the final
    # snapshot omits it too, so inventing one here would disagree with both.
    if not message_open and not function_calls:
        for opened in _open_message():
            yield opened

    tip = _budget_continue_tip_delta(result)
    if tip:
        yield frame(
            "response.output_text.delta",
            {
                "item_id": message_item_id,
                "output_index": message_output_index,
                "content_index": 0,
                "delta": tip,
            },
        )
        text = f"{text.rstrip()}{tip}"
        result = {**result, "content": f"{(result.get('content') or '').rstrip()}{tip}"}

    banner = _session_banner(ctx, has_tool_calls=bool(function_calls))
    if banner:
        yield frame(
            "response.output_text.delta",
            {
                "item_id": message_item_id,
                "output_index": message_output_index,
                "content_index": 0,
                "delta": banner,
            },
        )
        text += banner

    for closed in _close_message(text):
        yield closed

    for closed in _close_function_calls():
        yield closed
    translation.warn_abandoned_streamed_calls(
        translation.tool_calls_to_openai(result),
        function_calls.keys(),
        task_id=ctx.task_id,
    )

    reasoning_content = result.get("reasoning_content") or (
        "".join(thinking_parts) if thinking_parts else None
    )
    final = translation.responses_payload(
        {**result, "content": text, "reasoning_content": reasoning_content},
        model_id=ctx.model_id,
        response_id=response_id,
        conversation_id=ctx.conversation_id,
    )
    await sessions.remember_response(response_id, ctx.conversation_id, ctx.principal, ctx.cfg)
    await _remember_turn_transcript(ctx, {**result, "content": text})
    _publish_facade_event(ctx, route="responses", outcome="success")
    yield frame("response.completed", {"response": final})
    yield streaming.DONE_SENTINEL


@router.post(
    "/responses",
    summary="Create response",
    description=(
        "OpenAI Responses endpoint backed by Motet. Supports streaming, tool calls, and "
        "session continuation through 'conversation' or 'previous_response_id', which map "
        "to a Motet conversation."
    ),
    response_description="response object, or an SSE stream of response events",
    response_model=None,
    responses={
        200: {"description": "Response or SSE stream"},
        400: {"description": "Unsupported or malformed request"},
        401: {"description": "Authentication required"},
        403: {"description": "Mode not permitted for this credential"},
        404: {"description": "Model not found or not allowlisted"},
    },
)
async def create_response(
    request: Request,
    req: ResponsesRequest = Body(...),
    x_motet_facade_mode: Optional[str] = Header(default=None, alias="X-Motet-Facade-Mode"),
    x_motet_conversation_id: Optional[str] = Header(default=None, alias="X-Motet-Conversation-Id"),
    principal: Principal = Depends(get_current_principal),
) -> Any:
    """Create a response in the credential's facade mode."""
    try:
        ctx = await _prepare(
            req,
            request,
            principal,
            header_mode=x_motet_facade_mode,
            header_conversation_id=x_motet_conversation_id,
        )
    except FacadeError as exc:
        return _facade_error_response(exc, route="responses")

    logger.info(
        "openai_compat_responses_request",
        mode=ctx.mode.value,
        model=ctx.model_id,
        stream=req.stream,
        message_count=len(ctx.messages),
        task_id=ctx.task_id,
        conversation_id=ctx.conversation_id,
        tenant_id=ctx.tenant_id,
        principal_id=ctx.principal_id,
    )

    response_id = new_response_id()
    headers = _correlation_headers(ctx)

    if req.stream:
        body = streaming.with_keepalive(
            _responses_stream_body(ctx, response_id=response_id),
            float(getattr(ctx.cfg, "openai_compat_stream_keepalive_seconds", 15) or 0),
        )
        return StreamingResponse(
            body,
            media_type="text/event-stream",
            headers={**streaming.SSE_HEADERS, **headers},
        )

    try:
        result = await execution.run(ctx)
    except FacadeError as exc:
        _publish_facade_event(ctx, route="responses", outcome="error")
        return _facade_error_response(exc, route="responses")
    except Exception as exc:
        logger.error(
            "openai_compat_responses_failed",
            task_id=ctx.task_id,
            error=str(exc),
            exc_info=True,
        )
        _publish_facade_event(ctx, route="responses", outcome="error")
        return JSONResponse(
            status_code=502,
            content=error_payload("inference failed", error_type="api_error"),
        )

    result = _apply_session_banner(ctx, result)
    stop_reason = result.get("stop_reason")
    if stop_reason:
        headers = _correlation_headers(ctx, stop_reason=str(stop_reason))
    await sessions.remember_response(response_id, ctx.conversation_id, principal, ctx.cfg)
    await _remember_turn_transcript(ctx, result)
    _publish_facade_event(ctx, route="responses", outcome="success")
    payload = translation.responses_payload(
        result,
        model_id=ctx.model_id,
        response_id=response_id,
        conversation_id=ctx.conversation_id,
    )
    return JSONResponse(content=payload, headers=headers)


__all__ = ["router"]
