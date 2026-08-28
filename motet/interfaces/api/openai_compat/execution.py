"""
Motet - OpenAI Compatible Execution Modes

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Execution backends for the OpenAI-compatible facade.

    Three modes back the same OpenAI routes at increasing depth:

    - ``passthrough``   canonical inference via model_inference / model_stream.
                        The client owns any tool loop, so Cursor Agent keeps its
                        IDE tools. This is the default.
    - ``hosted_tools``  ``core.agent_loop`` → ``runtime.start`` with a fixed allowlist
                        (no discovery, no agent_turn hooks). Client tools are
    handback. Mixed turns checkpoint like agent mode and resume via ``resume_agent_turn``.
                        One ``task_id``.
    - ``agent``         the full Motet agent stack (memory, artifact RAG,
                        workflows, transcripts) behind the OpenAI wire. Client-
    declared tools ride along as handback tools: when the model calls one, the turn suspends and the facade returns OpenAI ``tool_calls``;
                        the follow-up request with the ``role="tool"`` results
                        resumes the same turn from its checkpoint.

    Every mode runs through existing distributed commands rather than calling a
    provider adapter directly, which is what keeps registry routing,
    budgets, command events, and traces applied to facade traffic.

Dependencies:
    - motet.core.workers.global_invoker: distributed command execution
    - motet.core.commands.builtin.model: model_inference / model_stream
    - motet.core.commands.builtin.tool: tool_execution / tool_list
    - motet.core.orchestration.turn.runtime: resume handle lookup
    - motet.core.reasoning.react.agent: hosted_tools hop (allowlist + handback)
    - motet.core.stack.MotetStack: agent-mode orchestration

Usage:
    from motet.interfaces.api.openai_compat import execution

    result = await execution.run(ctx)
    async for kind, value in execution.stream(ctx):
        ...

Notes:
    - Results are normalized to the model_inference result shape for translation
    - Hosted tool exposure is deny-by-default and independent of client tools
    - The worker tool-listing cache is keyed by (motet, tenant, principal) scope
    - Agent-mode resume: trailing role=tool → runtime.resolve_resume (no
      checkpoints import). Hosted_tools dispatches ``agent`` (runtime.start)
      with a fixed allowlist; mixed turns suspend through the loop like agent mode.
    - Agent mode reuses the same context contract as /api/v1/chat
    - Agent-mode surface_id comes from the agent's effective allow-list when
      unambiguous (single id, or multi with ``openai_compat`` preferred); otherwise
      defaults to ``openai_compat``
    - Agent-mode usage is the turn aggregate from agent_turn's terminal end event
    - Agent-mode client tools are gated by openai_compat_agent_client_tools (on
      by default); trailing role=tool messages that match a recorded handback
      resume the suspended turn, anything else runs as a fresh turn
    - On resume, ctx.conversation_id is rebound to the checkpoint's conversation
      so prompt_cache_key / cost / X-Motet-Conversation-Id stay on the suspend id
      when the client minted a fresh openai-{uuid} for the tool-result POST
    - ctx.messages is banner-free (what the model sees); ctx.raw_messages is the
      transcript as the client sent it and is what session fingerprints hash
    - Tool-call argument fragments are forwarded as ("tool_call_delta", frame)
      through ToolCallDeltaGate, which releases only client-owned calls.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Optional, Set, Tuple

import structlog

from ....core.models.adapters.provider_builtin_tools import tool_wire_to_canonical
from ....core.models.adapters.tool_call_codec import message_has_tool_calls
from ....core.orchestration.turn.budget_continue import (
    BUDGET_STOP_FALLBACK_MESSAGE,
    BUDGET_STOP_REASONS,
    is_budget_stop,
)
from ....core.orchestration.turn.runtime.result import TurnResultKind, coerce_turn_result
from ....core.security.facade_policy import FacadeMode, FacadePolicy
from ....core.types import CanonicalToolSchema, Message, OutputContract, Principal
from .errors import FacadeError
from motet.core.distributed.tenant_keys import task_response_stream

from .streaming import consume_task_events

logger = structlog.get_logger(__name__)

# LoopContext loop_id for hosted_tools (AgentData.agent_id). Hyphenated so it
# cannot be read as an ADR-0083 qualified agent (bundle.agent). Not a registry
# agent — inject_meta_tools=False leaves owning agent_id unset.
HOSTED_TOOLS_LOOP_ID = "openai-compat-hosted-tools"

# Cached worker tool listing. Hosted tool schemas change only when the registry
# changes, and a round trip per loop iteration would dominate latency. Keyed by
# (motet_id, tenant_id, principal_id): tool listings are becoming tenant- and
# principal-scoped (ADR-0075 list_visible, ADR-0126 tenant catalogs), and a
# process-global cache would leak one tenant's listing to another.
_TOOL_CACHE_TTL_SECONDS = 60.0
_tool_cache: Dict[Tuple[str, str, str], Dict[str, Any]] = {}


@dataclass
class FacadeContext:
    """Everything one facade request needs, resolved once at the HTTP edge."""

    mode: FacadeMode
    policy: FacadePolicy
    principal: Principal
    cfg: Any
    provider: str
    registry_key: str
    spec: Any
    model_id: str
    messages: List[Message]
    model_settings: Dict[str, Any]
    conversation_id: str
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    trace_id: Optional[str] = None
    tools: Optional[List[CanonicalToolSchema]] = None
    output_contract: Optional[OutputContract] = None
    agent_id: Optional[str] = None
    # Transcript exactly as the client sent it, before session banners were
    # stripped out of `messages`. Fingerprints must hash this: the client will
    # echo the banner-bearing text back next turn, so a hash over the stripped
    # copy would never match the lookup (ADR-0125 §5d).
    raw_messages: Optional[List[Message]] = None
    # True when this turn opened a brand-new conversation rather than rejoining
    # one, which is what banner mode "first" keys on.
    conversation_is_new: bool = True

    @property
    def transcript_messages(self) -> List[Message]:
        """Messages to fingerprint: the unstripped transcript when available."""
        return self.raw_messages if self.raw_messages is not None else self.messages

    @property
    def tenant_id(self) -> str:
        return getattr(self.principal, "tenant_id", None) or "default"

    @property
    def principal_id(self) -> str:
        return getattr(self.principal, "id", None) or ""

    @property
    def motet_id(self) -> str:
        return getattr(self.principal, "motet_id", None) or "default"


# ---------------------------------------------------------------------------
# Command plumbing
# ---------------------------------------------------------------------------


def _unwrap(envelope: Any, *, operation: str) -> Dict[str, Any]:
    """Unwrap the invoker envelope and the ADR-0029 command response.

    Raises FacadeError with a sanitized message: provider and internal errors
    must not leak verbatim to a third-party client (ADR-0125 §11d).
    """
    if not isinstance(envelope, dict):
        raise FacadeError(502, f"{operation} returned no result", error_type="api_error")

    if envelope.get("status") == "error":
        raise FacadeError(
            502,
            f"{operation} failed: {_error_message(envelope.get('error'))}",
            error_type="api_error",
        )

    inner = envelope.get("result")
    if not isinstance(inner, dict):
        # Some commands return the ADR-0029 body directly.
        inner = envelope

    if inner.get("status") == "error":
        raise FacadeError(
            502,
            f"{operation} failed: {_error_message(inner.get('error'))}",
            error_type="api_error",
        )

    data = inner.get("data")
    if isinstance(data, dict):
        return data
    return inner


def _error_message(error: Any) -> str:
    if isinstance(error, dict):
        return str(error.get("message") or error.get("error") or error)
    return str(error or "unknown error")


async def _execute(command: Any, *, operation: str) -> Dict[str, Any]:
    """Run a distributed command off the event loop and unwrap the result."""
    from ....core.workers import global_invoker

    envelope = await asyncio.to_thread(global_invoker.execute_command, command)
    return _unwrap(envelope, operation=operation)


def _build_request_context(ctx: FacadeContext) -> Any:
    from ....core.types import RequestContext

    return RequestContext(
        tenant_id=ctx.tenant_id,
        principal_id=ctx.principal_id,
        motet_id=ctx.motet_id,
        conversation_id=ctx.conversation_id,
        task_id=ctx.task_id,
        trace_id=ctx.trace_id,
    )


def _inference_command(ctx: FacadeContext, messages: List[Message], tools: Any) -> Any:
    from motet.core.commands.command_data_classes import ModelInferenceData
    from motet.core.commands.builtin.model import model_inference

    return model_inference(
        task_id=ctx.task_id,
        conversation_id=ctx.conversation_id,
        tenant_id=ctx.tenant_id,
        principal_id=ctx.principal_id,
        motet_id=ctx.motet_id,
        trace_id=ctx.trace_id,
        data=ModelInferenceData(
            messages=messages,
            model_settings=ctx.model_settings,
            request_context=_build_request_context(ctx),
            tools=tools,
            output_contract=ctx.output_contract,
        ),
    )


def _stream_command(ctx: FacadeContext, messages: List[Message], tools: Any, task_id: str) -> Any:
    from motet.core.commands.command_data_classes import ModelStreamData
    from motet.core.commands.builtin.model import model_stream

    request_context = _build_request_context(ctx)
    request_context = request_context.model_copy(update={"task_id": task_id})
    return model_stream(
        task_id=task_id,
        conversation_id=ctx.conversation_id,
        tenant_id=ctx.tenant_id,
        principal_id=ctx.principal_id,
        motet_id=ctx.motet_id,
        trace_id=ctx.trace_id,
        data=ModelStreamData(
            messages=messages,
            stream_key=task_response_stream(ctx.tenant_id, task_id),
            model_settings=ctx.model_settings,
            request_context=request_context,
            tools=tools,
            output_contract=ctx.output_contract,
        ),
    )


def _normalize_stream_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Give model_stream results the field names translation expects."""
    normalized = dict(result)
    normalized.setdefault("content", result.get("final_content") or "")
    # model_stream already returns reasoning_content when thinking was enabled.
    if result.get("reasoning_content") and not normalized.get("reasoning_content"):
        normalized["reasoning_content"] = result["reasoning_content"]
    return normalized


def _thinking_yield(payload: Dict[str, Any]) -> Optional[Tuple[str, Any]]:
    """Map a Motet thinking stream payload to a facade yield, or None if empty."""
    text = payload.get("text", "")
    text_s = str(text) if text is not None else ""
    is_complete = bool(payload.get("is_complete", False))
    if not text_s and not is_complete:
        return None
    return ("thinking", {"text": text_s, "is_complete": is_complete})


def _declared_tool_names(ctx: FacadeContext) -> Set[str]:
    """Names of the tools the request declared."""
    names: Set[str] = set()
    for tool in ctx.tools or []:
        raw = getattr(tool, "name", None)
        if raw is None and isinstance(tool, dict):
            raw = tool.get("name")
        if raw:
            names.add(str(raw))
    return names


class ToolCallDeltaGate:
    """Turn raw tool-call fragments into progress frames the client may act on.

    Argument generation is the one part of a turn that can run for minutes with
    nothing on the wire — a whole-file write is thousands of tokens inside a
    single tool call — so the fragments are worth forwarding. Two rules make
    that safe:

    Ownership. A fragment is released only once the tool name is known and names
    a tool the client declared. A Motet-owned call executes on this side, and a
    client that saw it would try to run a tool it does not have. Declared names
    are canonical (``tools_to_canonical``); Chat Completions deltas still carry
    the provider wire form (``mcp__server__tool``), so the check normalizes with
    ``tool_wire_to_canonical`` before comparing.

    Completeness. Fragments that arrive before the name resolves are buffered
    rather than dropped, so whatever the client assembles from the frames is the
    whole argument string. Providers send the name first in practice; buffering
    means a provider that does not is degraded, not corrupt.
    """

    def __init__(self, allowed_names: Set[str]) -> None:
        self._allowed = allowed_names
        self._buffers: Dict[str, str] = {}
        self._released: Set[str] = set()

    def feed(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Return the frame to forward for one fragment, or None to withhold."""
        if not self._allowed:
            return None
        call_id = str(payload.get("call_id") or "")
        if not call_id:
            return None
        fragment = str(payload.get("arguments_delta") or "")
        tool_name = str(payload.get("tool_name") or "")
        # Deltas may still be on the wire form; the allowlist is canonical.
        canonical_name = tool_wire_to_canonical(tool_name) if tool_name else ""

        if call_id in self._released:
            if not fragment:
                return None
            return {
                "call_id": call_id,
                "tool_name": tool_name,
                "arguments_delta": fragment,
                "first": False,
            }

        buffered = self._buffers.get(call_id, "") + fragment
        if canonical_name not in self._allowed:
            self._buffers[call_id] = buffered
            return None

        self._buffers.pop(call_id, None)
        self._released.add(call_id)
        return {
            "call_id": call_id,
            "tool_name": tool_name,
            "arguments_delta": buffered,
            "first": True,
        }


# ---------------------------------------------------------------------------
# Passthrough
# ---------------------------------------------------------------------------


async def run_passthrough(ctx: FacadeContext) -> Dict[str, Any]:
    """Single canonical inference, client owns any tool loop."""
    command = _inference_command(ctx, ctx.messages, ctx.tools)
    return await _execute(command, operation="model inference")


async def stream_passthrough(
    ctx: FacadeContext,
) -> AsyncGenerator[Tuple[str, Any], None]:
    """Stream a single canonical inference.

    Yields ``("delta", text)`` for assistant text and a final ``("result", dict)``
    carrying tool calls, finish reason, and usage.
    """
    async for item in _stream_once(ctx, ctx.messages, ctx.tools, ctx.task_id):
        yield item


async def _stream_once(
    ctx: FacadeContext,
    messages: List[Message],
    tools: Any,
    task_id: str,
    *,
    tool_call_deltas: bool = True,
) -> AsyncGenerator[Tuple[str, Any], None]:
    """Run one streaming inference, forwarding text deltas then the result.

    ``tool_call_deltas`` is off for callers that execute some of the model's
    tool calls themselves (hosted tools), where a forwarded fragment would
    describe work the client is not going to do.
    """
    from ....core.workers import global_invoker

    command = _stream_command(ctx, messages, tools, task_id)
    command_task = asyncio.create_task(
        asyncio.to_thread(global_invoker.execute_command, command)
    )

    thinking_parts: List[str] = []
    gate = ToolCallDeltaGate(_declared_tool_names(ctx) if tool_call_deltas else set())
    try:
        async for item in consume_task_events(
            task_id, command_task, tenant_id=ctx.tenant_id
        ):
            event = item["event"]
            if event == "token":
                text = item["payload"].get("data", "")
                if text:
                    yield ("delta", str(text))
            elif event == "tool_call_delta":
                frame = gate.feed(item.get("payload") or {})
                if frame is not None:
                    yield ("tool_call_delta", frame)
            elif event == "thinking":
                payload = item.get("payload") or {}
                text = payload.get("text", "")
                if text:
                    thinking_parts.append(str(text))
                thinking = _thinking_yield(payload if isinstance(payload, dict) else {})
                if thinking is not None:
                    yield thinking
        envelope = await command_task
    except asyncio.CancelledError:
        command_task.cancel()
        raise
    except Exception:
        command_task.cancel()
        raise

    result = _normalize_stream_result(_unwrap(envelope, operation="model stream"))
    if thinking_parts and not result.get("reasoning_content"):
        result["reasoning_content"] = "".join(thinking_parts)

    # A stream that produced no token frames still has content in the result
    # (short answers can complete before the reader attaches).
    yield ("result", result)


# ---------------------------------------------------------------------------
# Hosted tools
# ---------------------------------------------------------------------------


def _allowlist_patterns(cfg: Any) -> List[str]:
    raw = getattr(cfg, "openai_compat_hosted_tools_allowlist", "") or ""
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def _tool_allowed(name: str, patterns: List[str]) -> bool:
    """Match a canonical tool name against allowlist entries."""
    for pattern in patterns:
        if pattern == "*":
            return True
        if pattern == name:
            return True
        if pattern.endswith(".*") and name.startswith(pattern[:-1]):
            return True
    return False


async def _list_worker_tools(ctx: FacadeContext) -> List[Dict[str, Any]]:
    """Fetch the worker tool registry listing, cached briefly per identity scope."""
    now = time.monotonic()
    cache_key = (ctx.motet_id, ctx.tenant_id, ctx.principal_id)
    entry = _tool_cache.get(cache_key)
    if entry and entry["expires_at"] > now:
        return list(entry["tools"])

    from motet.core.commands.command_data_classes import ToolListData
    from motet.core.commands.builtin.tool import tool_list

    command = tool_list(
        task_id=ctx.task_id,
        conversation_id=ctx.conversation_id,
        tenant_id=ctx.tenant_id,
        principal_id=ctx.principal_id,
        motet_id=ctx.motet_id,
        data=ToolListData(),
    )
    data = await _execute(command, operation="tool listing")
    tools = [t for t in (data.get("tools") or []) if isinstance(t, dict)]
    _tool_cache[cache_key] = {"tools": tools, "expires_at": now + _TOOL_CACHE_TTL_SECONDS}
    return list(tools)


def _looks_like_json_schema(candidate: Any) -> bool:
    return (
        isinstance(candidate, dict)
        and candidate.get("type") == "object"
        and isinstance(candidate.get("properties"), dict)
    )


async def hosted_tool_schemas(ctx: FacadeContext) -> List[CanonicalToolSchema]:
    """Build canonical schemas for the Motet tools this request may execute.

    Exposure is deny-by-default: only tools matching the configured allowlist are
    advertised. Execution still carries the caller's principal into the worker,
    so registry-level scoping applies on top of this list (ADR-0125 §11b).
    """
    patterns = _allowlist_patterns(ctx.cfg)
    if not patterns:
        return []

    schemas: List[CanonicalToolSchema] = []
    for entry in await _list_worker_tools(ctx):
        name = str(entry.get("name") or "")
        if not name or not _tool_allowed(name, patterns):
            continue
        raw_schema = entry.get("schema")
        if _looks_like_json_schema(raw_schema):
            json_schema = raw_schema
        else:
            logger.debug("openai_compat_tool_schema_fallback", tool=name)
            json_schema = {"type": "object", "properties": {}, "additionalProperties": True}
        schemas.append(
            CanonicalToolSchema(
                name=name,
                description=str(entry.get("description") or ""),
                json_schema=json_schema,
            )
        )
    return schemas


def build_hosted_tools_agent_data(
    ctx: FacadeContext,
    hosted_schemas: List[CanonicalToolSchema],
) -> Any:
    """Map a hosted_tools request onto AgentData for ``core.agent_loop``.

    ``tools`` is the advertised set (allowlist + client), never None, so the
    loop skips discovery. ``handback_tools`` is client-declared only.
    ``inject_meta_tools=False`` keeps the allowlist as advertised (no Motet
    fallback prompt; ADR-0138 deleted the meta-tool the flag was named for, and
    the name survives only because it is persisted in checkpoints). ``agent_id`` is
    ``HOSTED_TOOLS_LOOP_ID`` (LoopContext loop_id only). It is not a
    registry agent, so resume cannot attach ``cursor.backend`` (or any
    other) memory hooks. Facade agent mode is what runs the cursor bundle.
    """
    from motet.core.orchestration.turn.prepare import extract_turn_input_text
    from motet.core.reasoning.react.agent_data import (
        DEFAULT_MODEL_NAME,
        DEFAULT_MODEL_PROVIDER,
        AgentData,
    )

    hosted = list(hosted_schemas or [])
    client = list(ctx.tools or [])
    advertised = list(hosted)
    seen = {schema.name for schema in advertised if schema.name}
    for schema in client:
        name = schema.name if hasattr(schema, "name") else str(schema.get("name") or "")
        if name and name not in seen:
            advertised.append(schema)
            seen.add(name)

    raw = extract_turn_input_text(ctx.messages)
    input_text = raw if isinstance(raw, str) else ""
    max_iterations = max(1, int(getattr(ctx.cfg, "openai_compat_max_tool_iterations", 8) or 8))
    settings = ctx.model_settings or {}
    return AgentData(
        agent_id=HOSTED_TOOLS_LOOP_ID,
        inject_meta_tools=False,
        use_task_stream=True,
        input=input_text,
        conversation_history=list(ctx.messages),
        tools=advertised,
        max_iterations=max_iterations,
        model_provider=ctx.provider or DEFAULT_MODEL_PROVIDER,
        model_name=ctx.registry_key or DEFAULT_MODEL_NAME,
        temperature=float(settings.get("temperature") or 0.2),
        enable_thinking=bool(settings.get("enable_thinking")),
        reasoning_effort=settings.get("reasoning_effort") or "medium",
        handback_tools=client or None,
        base_stream_key=task_response_stream(ctx.tenant_id, ctx.task_id),
    )


def _hosted_tools_command(ctx: FacadeContext, hosted_schemas: List[CanonicalToolSchema]) -> Any:
    """HTTP→worker hop: ``core.agent_loop`` with hosted_tools AgentData."""
    from motet.core.reasoning.react.agent import agent_loop

    return agent_loop(
        task_id=ctx.task_id,
        conversation_id=ctx.conversation_id,
        tenant_id=ctx.tenant_id,
        principal_id=ctx.principal_id,
        motet_id=ctx.motet_id,
        trace_id=ctx.trace_id,
        data=build_hosted_tools_agent_data(ctx, hosted_schemas),
    )


def _result_from_hosted_tools(loop_result: Dict[str, Any]) -> Dict[str, Any]:
    """Map an ``agent`` / loop payload onto the facade inference shape.

    Budget exhaustion keeps ``finish_reason=length`` so hosted_tools clients
    that already key off that wire value do not change.
    """
    result = _result_from_resumed_loop(loop_result)
    if is_budget_stop(result.get("stop_reason") or loop_result.get("stop_reason")):
        result["finish_reason"] = "length"
    return result


async def run_hosted_tools(ctx: FacadeContext) -> Dict[str, Any]:
    """Allowlisted Motet tools plus client handback, via ``core.agent_loop`` → start."""
    resume = await _maybe_resume(ctx)
    if resume is not None:
        return await _run_resume(ctx, *resume)

    hosted_schemas = await hosted_tool_schemas(ctx)
    data = await _execute(
        _hosted_tools_command(ctx, hosted_schemas),
        operation="hosted tools turn",
    )
    return _result_from_hosted_tools(data)


async def stream_hosted_tools(
    ctx: FacadeContext,
) -> AsyncGenerator[Tuple[str, Any], None]:
    """Hosted_tools turn, forwarding assistant text from the task stream."""
    resume = await _maybe_resume(ctx)
    if resume is not None:
        async for item in _stream_resume(ctx, *resume):
            yield item
        return

    hosted_schemas = await hosted_tool_schemas(ctx)
    async for item in _stream_turn_command(
        ctx,
        _hosted_tools_command(ctx, hosted_schemas),
        operation="hosted tools turn",
        map_result=_result_from_hosted_tools,
    ):
        yield item


# ---------------------------------------------------------------------------
# Agent stack
# ---------------------------------------------------------------------------


def _client_tools_enabled(cfg: Any) -> bool:
    """Whether agent mode honors client-declared tools as handback tools (§5c.1)."""
    return bool(getattr(cfg, "openai_compat_agent_client_tools", True))


def _surface_id_for_agent(agent_id: Optional[str]) -> str:
    """
    Conversation surface stamped for facade agent-mode turns.

    Asks the agent config (plus manage-UI overlay) for an effective allow-list:
    - one allowed surface → stamp that
    - several → prefer ``openai_compat`` if listed, else the first
    - none / all / lookup failure → ``openai_compat`` (facade default channel)
    """
    facade_default = "openai_compat"
    raw = (agent_id or "").strip()
    if not raw:
        return facade_default
    try:
        from ....core.agents import get_agent_registry, resolve_agent_id
        from ....core.surfaces import resolve_effective_allowlist

        qid = resolve_agent_id(raw)
        cfg = get_agent_registry().get(qid)
        config_ids = None
        if cfg is not None:
            raw_ids = getattr(cfg, "allowed_surface_ids", None)
            if isinstance(raw_ids, list):
                config_ids = list(raw_ids)
        allowed = resolve_effective_allowlist(
            qualified_agent_id=qid,
            config_allowed_surface_ids=config_ids,
        )
        if not allowed:
            return facade_default
        if len(allowed) == 1:
            return allowed[0]
        if facade_default in allowed:
            return facade_default
        return allowed[0]
    except Exception as exc:
        logger.warning(
            "openai_compat_surface_resolve_failed",
            agent_id=raw,
            error=str(exc),
        )
        return facade_default


def _agent_context(ctx: FacadeContext) -> Dict[str, Any]:
    """Build the chat context contract the agent stack expects."""
    principal_roles = list(getattr(ctx.principal, "roles", None) or [])
    context: Dict[str, Any] = {
        "agent_id": ctx.agent_id,
        "conversation_id": ctx.conversation_id,
        "principal_roles": principal_roles,
        "surface_id": _surface_id_for_agent(ctx.agent_id),
        "model_provider": ctx.provider,
        "model_name": ctx.registry_key,
    }
    # Client-opted thinking (ADR-0125): model_settings already gated on
    # CAP_REASONING at the HTTP edge; pass through so agent_turn prefers
    # context over agent_config defaults.
    if ctx.model_settings.get("enable_thinking"):
        context["enable_thinking"] = True
        context["reasoning_effort"] = ctx.model_settings.get("reasoning_effort") or "medium"
    # ADR-0125 §5c.1: client-declared tools ride into the agent stack as
    # handback tools. The loop injects their schemas each iteration and, when
    # the model calls one, suspends the turn (ADR-0127) instead of executing —
    # the facade then returns OpenAI tool_calls so the client runs its tool.
    if ctx.tools and _client_tools_enabled(ctx.cfg):
        context["handback_tools"] = [
            t.model_dump() if hasattr(t, "model_dump") else dict(t) for t in ctx.tools
        ]
    if ctx.output_contract is not None:
        context["output_contract"] = (
            ctx.output_contract.model_dump()
            if hasattr(ctx.output_contract, "model_dump")
            else ctx.output_contract
        )
    return context


def _build_stack(ctx: FacadeContext) -> Any:
    from ....core import MotetStack
    from ..shared.identity import apply_principal_to_config, attach_principal_to_stack

    apply_principal_to_config(ctx.cfg, ctx.principal)
    stack = MotetStack(ctx.cfg)
    attach_principal_to_stack(stack, ctx.principal)
    return stack


def _usage_from_agent_response(response: Any) -> Dict[str, int]:
    """Read turn-aggregated usage from MotetStack.chat / stream end metadata."""
    raw = getattr(response, "raw", None) or {}
    if isinstance(raw, dict) and isinstance(raw.get("usage"), dict):
        return dict(raw["usage"])
    prompt = getattr(response, "usage_tokens_input", None)
    completion = getattr(response, "usage_tokens_output", None)
    if prompt is None and completion is None:
        return {}
    prompt_i = int(prompt or 0)
    completion_i = int(completion or 0)
    return {
        "prompt_tokens": prompt_i,
        "completion_tokens": completion_i,
        "total_tokens": prompt_i + completion_i,
    }


def _agent_result(
    content: str,
    citations: Any = None,
    usage: Optional[Dict[str, Any]] = None,
    reasoning_content: Optional[str] = None,
    stop_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Shape an agent turn like an inference result for translation.

    Prefer the turn-aggregated usage envelope emitted by ``agent_turn`` (summed
    across model calls inside ``agentic_loop``). When that envelope is absent,
    report zeros rather than inventing a single-call number.

    Motet ``stop_reason`` (issue #188) is carried for correlation headers and
    budget-continue tips; OpenAI ``finish_reason`` stays ``stop`` unless a
    suspended handback overrides it to ``tool_calls``.
    """
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))
    result: Dict[str, Any] = {
        "content": content or "",
        "finish_reason": "stop",
        "citations": citations or [],
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    for key in ("cache_read_tokens", "cache_creation_tokens", "reasoning_tokens"):
        if usage.get(key) is not None:
            result[key] = int(usage.get(key) or 0)
    if isinstance(reasoning_content, str) and reasoning_content:
        result["reasoning_content"] = reasoning_content
    if stop_reason:
        result["stop_reason"] = str(stop_reason)
    return result


def _suspended_agent_result(
    *,
    content: str,
    handed_back: List[Dict[str, Any]],
    usage: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Shape a suspended agent turn (ADR-0127) as an OpenAI tool-call turn.

    The handed-back calls become ``tool_calls_canonical`` so the shared
    translation layer renders standard ``tool_calls`` with
    ``finish_reason="tool_calls"`` — the same wire behavior as the
    ``hosted_tools`` mixed-ownership split (ADR-0125 §5c.1).
    """
    result = _agent_result(content, usage=usage)
    result["finish_reason"] = "tool_calls"
    result["tool_calls_canonical"] = [
        {
            "call_id": call.get("tool_call_id"),
            "tool_name": call.get("tool_name"),
            "arguments_json": json.dumps(call.get("parameters") or {}),
        }
        for call in handed_back
    ]
    return result


# ---------------------------------------------------------------------------
# Agent-mode resume (ADR-0125 §5c.1 / ADR-0127)
# ---------------------------------------------------------------------------


def _split_trailing_observations(
    messages: List[Message],
) -> Optional[Tuple[List[Message], List[Dict[str, Any]]]]:
    """Split an OpenAI tool-loop continuation request into history + observations.

    A client continuing a handed-back turn resends the conversation ending in
    the assistant ``tool_calls`` message followed only by ``role="tool"``
    results. Returns ``(history_ending_with_assistant, observations)`` when the
    request has that shape, else None (a normal turn).
    """
    idx = len(messages)
    while idx > 0 and getattr(messages[idx - 1], "role", "") == "tool":
        idx -= 1
    if idx == len(messages) or idx == 0:
        return None
    prior = messages[idx - 1]
    if getattr(prior, "role", "") != "assistant" or not message_has_tool_calls(prior):
        return None

    observations: List[Dict[str, Any]] = []
    for message in messages[idx:]:
        call_id = str(getattr(message, "tool_call_id", "") or "").strip()
        if not call_id:
            # A tool result without an id cannot be matched to a handback.
            return None
        observations.append(
            {"tool_call_id": call_id, "content": getattr(message, "content", "") or ""}
        )
    return list(messages[:idx]), observations


def _rebind_ctx_conversation(
    ctx: FacadeContext, *, checkpoint_id: str, conversation_id: Optional[str]
) -> Optional[str]:
    """Point ``ctx.conversation_id`` at the conversation that suspended the turn.

    Clients that omit session-chaining headers mint a fresh ``openai-{uuid}``
    on every tool-result POST. Rebinding here keeps ``X-Motet-Conversation-Id``,
    ``previous_response_id`` mapping, ``prompt_cache_key``, and cost events on
    the suspend conversation (ADR-0124 / ADR-0127).
    """
    cid = (conversation_id or "").strip()
    if not cid:
        return None
    if cid == ctx.conversation_id:
        return cid
    previous = ctx.conversation_id
    ctx.conversation_id = cid
    logger.info(
        "openai_compat_resume_conversation_rebound",
        checkpoint_id=checkpoint_id,
        from_conversation_id=previous,
        to_conversation_id=cid,
        task_id=ctx.task_id,
    )
    return cid


async def _maybe_resume(
    ctx: FacadeContext,
) -> Optional[Tuple[str, List[Message], List[Dict[str, Any]]]]:
    """Detect a resume request from trailing tool observations (ADR-0134).

    Wire adapter: split trailing tool observations → runtime.resolve_resume
    → rebind conversation → return handles for ``resume_agent_turn``.
    """
    split = _split_trailing_observations(ctx.messages)
    if split is None:
        return None
    history, observations = split
    from motet.core.orchestration.turn.runtime import resolve_resume

    handle = await asyncio.to_thread(
        resolve_resume,
        tenant_id=ctx.tenant_id,
        motet_id=ctx.motet_id,
        tool_call_ids=[o["tool_call_id"] for o in observations],
    )
    if handle is None:
        logger.info(
            "openai_compat_no_resumable_checkpoint",
            tool_call_ids=[o["tool_call_id"] for o in observations],
            conversation_id=ctx.conversation_id,
            task_id=ctx.task_id,
        )
        return None
    bound = _rebind_ctx_conversation(
        ctx,
        checkpoint_id=handle.checkpoint_id,
        conversation_id=handle.conversation_id,
    )
    logger.info(
        "openai_compat_resuming_turn",
        checkpoint_id=handle.checkpoint_id,
        observation_count=len(observations),
        conversation_id=ctx.conversation_id,
        rebound_conversation_id=bound,
        task_id=ctx.task_id,
    )
    return handle.checkpoint_id, history, observations


def _resume_command(
    ctx: FacadeContext,
    checkpoint_id: str,
    history: List[Message],
    observations: List[Dict[str, Any]],
) -> Any:
    # Issue #147: orchestration-owned resume applies TurnOutcome + finalize +
    # complete_agent_turn; resume_turn remains the loop re-entry primitive.
    from motet.core.orchestration.turn.resume_agent_turn import (
        ResumeAgentTurnData,
        resume_agent_turn,
    )

    return resume_agent_turn(
        task_id=ctx.task_id,
        conversation_id=ctx.conversation_id,
        tenant_id=ctx.tenant_id,
        principal_id=ctx.principal_id,
        motet_id=ctx.motet_id,
        trace_id=ctx.trace_id,
        data=ResumeAgentTurnData(
            checkpoint_id=checkpoint_id,
            observations=observations,
            # Client-authoritative history (ADR-0127): the wire transcript the
            # client resent replaces the checkpointed copy on resume.
            conversation_history=[
                m.model_dump(mode="json") if hasattr(m, "model_dump") else m
                for m in history
            ],
        ),
    )


def _map_resume_error(exc: FacadeError) -> FacadeError:
    """Map resume_turn failures to client-appropriate OpenAI errors.

    The command envelope only carries message text, so this matches on the
    stable prefixes resume_turn raises with. Anything unrecognized stays a 502.
    """
    message = exc.message or ""
    if "different principal" in message:
        return FacadeError(
            404,
            "tool results do not correspond to a resumable turn for this credential",
            code="checkpoint_not_found",
        )
    if "not found or expired" in message or "no checkpoint found" in message:
        return FacadeError(
            404,
            "the suspended turn has expired; resend the request as a new turn",
            code="checkpoint_not_found",
        )
    if "resume_turn:" in message:
        # Observation validation: forged/missing/duplicate tool_call_ids.
        return FacadeError(
            400,
            "tool results do not match the tool calls handed back for this turn",
            error_type="invalid_request_error",
            code="invalid_tool_observations",
        )
    return exc


def _nonempty_loop_content(loop_result: Dict[str, Any]) -> str:
    """Prefer loop text; never return empty for budget/stop reasons on the wire."""
    content = str(loop_result.get("final_response") or "").strip()
    if content:
        return content
    if loop_result.get("stop_reason") in BUDGET_STOP_REASONS:
        return BUDGET_STOP_FALLBACK_MESSAGE
    return str(loop_result.get("final_response") or "")


def _result_from_resumed_loop(loop_result: Dict[str, Any]) -> Dict[str, Any]:
    """Map a resume_agent_turn result onto the facade result shape.

    Branches on ``TurnResult.kind`` (ADR-0134). The command still returns the
    orchestration dict; ``coerce_turn_result`` classifies it. Wire usage is
    per-request (``usage_this_request``), not the loop's turn-cumulative
    ``usage``: a client doing its own context accounting treats the reported
    numbers as this call's cost, and the cumulative total made Cursor believe
    the context was overfull and summarize away the transcript every turn
    (ADR-0125 §5f).
    """
    turn = coerce_turn_result(loop_result)
    payload = turn.payload if turn.payload else loop_result
    usage = payload.get("usage_this_request")
    if not isinstance(usage, dict):
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
    if turn.kind is TurnResultKind.SUSPENDED:
        return _suspended_agent_result(
            content=str(payload.get("final_response") or turn.final_response or ""),
            handed_back=list(payload.get("handed_back_tool_calls") or []),
            usage=usage,
        )
    stop_reason = payload.get("stop_reason") or turn.stop_reason
    return _agent_result(
        _nonempty_loop_content(payload),
        usage=usage,
        stop_reason=str(stop_reason) if stop_reason else None,
    )


async def _run_resume(
    ctx: FacadeContext,
    checkpoint_id: str,
    history: List[Message],
    observations: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Resume a suspended agent turn and shape the outcome for translation."""
    command = _resume_command(ctx, checkpoint_id, history, observations)
    try:
        data = await _execute(command, operation="resume turn")
    except FacadeError as exc:
        raise _map_resume_error(exc) from exc
    return _result_from_resumed_loop(data)


async def _stream_turn_command(
    ctx: FacadeContext,
    command: Any,
    *,
    operation: str,
    map_result: Any,
    map_error: Any = None,
) -> AsyncGenerator[Tuple[str, Any], None]:
    """Run one turn command and forward its task-stream text / thinking / handback."""
    from ....core.workers import global_invoker

    command_task = asyncio.create_task(
        asyncio.to_thread(global_invoker.execute_command, command)
    )
    thinking_parts: List[str] = []
    allow_client_deltas = ctx.mode is FacadeMode.HOSTED_TOOLS or (
        ctx.mode is FacadeMode.AGENT and _client_tools_enabled(ctx.cfg)
    )
    gate = ToolCallDeltaGate(
        _declared_tool_names(ctx) if allow_client_deltas else set()
    )
    try:
        async for item in consume_task_events(
            ctx.task_id, command_task, tenant_id=ctx.tenant_id
        ):
            event = item["event"]
            if event == "token":
                text = item["payload"].get("data", "")
                if text:
                    yield ("delta", str(text))
            elif event == "tool_call_delta":
                frame = gate.feed(item.get("payload") or {})
                if frame is not None:
                    yield ("tool_call_delta", frame)
            elif event == "thinking":
                payload = item.get("payload") or {}
                text = payload.get("text", "") if isinstance(payload, dict) else ""
                if text:
                    thinking_parts.append(str(text))
                thinking = _thinking_yield(payload if isinstance(payload, dict) else {})
                if thinking is not None:
                    yield thinking
        envelope = await command_task
    except asyncio.CancelledError:
        command_task.cancel()
        raise
    except Exception:
        command_task.cancel()
        raise

    try:
        data = _unwrap(envelope, operation=operation)
    except FacadeError as exc:
        if map_error is not None:
            raise map_error(exc) from exc
        raise
    result = map_result(data)
    if thinking_parts and not result.get("reasoning_content"):
        result["reasoning_content"] = "".join(thinking_parts)
    yield ("result", result)


async def _stream_resume(
    ctx: FacadeContext,
    checkpoint_id: str,
    history: List[Message],
    observations: List[Dict[str, Any]],
) -> AsyncGenerator[Tuple[str, Any], None]:
    """Resume a suspended turn, forwarding its text deltas."""
    command = _resume_command(ctx, checkpoint_id, history, observations)
    async for item in _stream_turn_command(
        ctx,
        command,
        operation="resume turn",
        map_result=_result_from_resumed_loop,
        map_error=_map_resume_error,
    ):
        yield item


async def run_agent(ctx: FacadeContext) -> Dict[str, Any]:
    """Run a full Motet agent turn behind the OpenAI wire."""
    resume = await _maybe_resume(ctx)
    if resume is not None:
        return await _run_resume(ctx, *resume)

    stack = _build_stack(ctx)
    response = await stack.chat(ctx.messages, context=_agent_context(ctx))
    raw = getattr(response, "raw", None) or {}
    if isinstance(raw, dict) and raw.get("suspended"):
        # ADR-0127: the turn suspended on a client-owned tool call; hand the
        # calls back as OpenAI tool_calls and let the client continue the loop.
        return _suspended_agent_result(
            content=getattr(response, "content", "") or "",
            handed_back=raw.get("handed_back_tool_calls") or [],
            usage=raw.get("usage") if isinstance(raw.get("usage"), dict) else None,
        )
    content = getattr(response, "content", "") or ""
    raw_stop = raw.get("stop_reason") if isinstance(raw, dict) else None
    if not str(content).strip() and is_budget_stop(
        str(raw_stop) if raw_stop else None
    ):
        content = BUDGET_STOP_FALLBACK_MESSAGE
    return _agent_result(
        content,
        getattr(response, "citations", None),
        usage=_usage_from_agent_response(response),
        stop_reason=str(raw_stop) if raw_stop else None,
    )


async def stream_agent(ctx: FacadeContext) -> AsyncGenerator[Tuple[str, Any], None]:
    """Stream a Motet agent turn as OpenAI deltas (+ optional thinking).

    Motet-native tool/reasoning-step events still have no OpenAI representation
    and remain server-side only. Thinking events are forwarded when the client
    opted in (mapped to ``reasoning_content`` / Responses ``reasoning`` items
    at the route layer).
    """
    resume = await _maybe_resume(ctx)
    if resume is not None:
        async for item in _stream_resume(ctx, *resume):
            yield item
        return

    stack = _build_stack(ctx)
    collected: List[str] = []
    thinking_parts: List[str] = []
    final_content = ""
    usage: Dict[str, Any] = {}
    suspended: Optional[Dict[str, Any]] = None
    end_stop_reason: Optional[str] = None
    # Client-declared tools are exactly the ones a suspension hands back
    # (ADR-0127), so their fragments are the only ones safe to forward.
    gate = ToolCallDeltaGate(
        _declared_tool_names(ctx) if _client_tools_enabled(ctx.cfg) else set()
    )

    async for event in stack.orchestrator.stream_events(
        stack, ctx.messages, context=_agent_context(ctx)
    ):
        kind = event.get("event")
        if kind == "token":
            text = str(event.get("data") or "")
            if text:
                collected.append(text)
                yield ("delta", text)
        elif kind == "tool_call_delta":
            frame = gate.feed(event)
            if frame is not None:
                yield ("tool_call_delta", frame)
        elif kind == "thinking":
            text = str(event.get("text") or "")
            if text:
                thinking_parts.append(text)
            thinking = _thinking_yield(
                {"text": text, "is_complete": bool(event.get("is_complete", False))}
            )
            if thinking is not None:
                yield thinking
        elif kind == "error":
            raise FacadeError(
                502,
                f"agent turn failed: {_error_message(event.get('error'))}",
                error_type="api_error",
            )
        elif kind == "suspended":
            # ADR-0127 terminal handback event: the turn continues later via a
            # follow-up request carrying the client's tool results.
            suspended = {k: v for k, v in event.items() if k != "event"}
        elif kind == "end":
            final_content = str(event.get("content") or "")
            if isinstance(event.get("usage"), dict):
                usage = dict(event["usage"])
            if event.get("stop_reason"):
                end_stop_reason = str(event.get("stop_reason"))

    reasoning_content = "".join(thinking_parts) or None
    if suspended is not None:
        suspended_result = _suspended_agent_result(
            content=str(suspended.get("content") or "") or "".join(collected),
            handed_back=suspended.get("handed_back_tool_calls") or [],
            usage=suspended.get("usage") if isinstance(suspended.get("usage"), dict) else None,
        )
        if reasoning_content:
            suspended_result["reasoning_content"] = reasoning_content
        yield ("result", suspended_result)
        return

    wire_content = final_content or "".join(collected)
    if not str(wire_content).strip() and is_budget_stop(end_stop_reason):
        wire_content = BUDGET_STOP_FALLBACK_MESSAGE

    if not collected and wire_content:
        # Non-token-streaming agents deliver the whole answer on the end event.
        yield ("delta", wire_content)

    yield (
        "result",
        _agent_result(
            wire_content,
            usage=usage,
            reasoning_content=reasoning_content,
            stop_reason=end_stop_reason,
        ),
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def run(ctx: FacadeContext) -> Dict[str, Any]:
    """Execute a non-streaming request in the resolved mode."""
    if ctx.mode is FacadeMode.AGENT:
        return await run_agent(ctx)
    if ctx.mode is FacadeMode.HOSTED_TOOLS:
        return await run_hosted_tools(ctx)
    return await run_passthrough(ctx)


def stream(ctx: FacadeContext) -> AsyncGenerator[Tuple[str, Any], None]:
    """Execute a streaming request in the resolved mode."""
    if ctx.mode is FacadeMode.AGENT:
        return stream_agent(ctx)
    if ctx.mode is FacadeMode.HOSTED_TOOLS:
        return stream_hosted_tools(ctx)
    return stream_passthrough(ctx)


__all__ = [
    "FacadeContext",
    "HOSTED_TOOLS_LOOP_ID",
    "build_hosted_tools_agent_data",
    "hosted_tool_schemas",
    "run",
    "run_agent",
    "run_hosted_tools",
    "run_passthrough",
    "stream",
    "stream_agent",
    "stream_hosted_tools",
    "stream_passthrough",
]
