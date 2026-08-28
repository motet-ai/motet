"""
Motet - Moonshot (Kimi) Chat Completions Adapter

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Moonshot/Kimi provider adapter that extends the OpenAI Chat Completions adapter
    with Moonshot-specific wire format requirements:

    when ``enable_prompt_caching`` is set and the model has CAP_PROMPT_CACHING,
    sets ``prompt_cache_key`` from ``conversation_id`` (complete + stream).

    - reasoning_content: Required on assistant tool-call messages when thinking mode is enabled
    (K2.x and K3). Multi-turn tool loops must replay the full assistant message.
    - $web_search: Moonshot's builtin web search tool uses $ prefix on wire (K2.x)
    - temperature: Kimi K2.5 requires temperature=1.0 (thinking enabled) or 0.6 (thinking disabled)
    - thinking mode (K2.5): Controlled by request.model_settings.enable_thinking (chat UI toggle).
    Only the builtin $web_search tool is incompatible with K2.5 thinking (Moonshot docs);
    other tools can use thinking when enable_thinking is True. When $web_search is in the
    tool list, thinking is forced off for kimi-k2.5. Without tools (or with non-web_search
    tools), enable_thinking=True requests thinking (enabled + temperature=1.0);
    enable_thinking=False requests thinking disabled (extra_body + temperature=0.6).
    ThinkingEvent is only emitted when enable_thinking is True.
    - Kimi K3: Always-on thinking via top-level reasoning_effort (currently only "max").
    Do not send the K2.x thinking{} block. Sampling params are fixed by the API — omit them.
    Prefer max_completion_tokens. ThinkingEvent is always emitted when reasoning streams.

    This adapter inherits from OpenAIChatCompletionsAdapter and overrides only the
    Moonshot-specific behavior, keeping the base adapter clean.

Dependencies:
    - openai: OpenAI Python SDK (sync)
    - motet.core.models.adapters.providers.openai_chat_completions: Base adapter

Usage:
    adapter = MoonshotChatCompletionsAdapter(
        provider="moonshot",
        adapter_name="chat_completions",
        credentials={"api_key": "...", "base_url": "https://api.moonshot.ai/v1"}
    )
    resp = adapter.complete(LLMRequest(messages=[...], tools=[...]))

Notes:
    - Moonshot API is OpenAI-compatible but has specific requirements for thinking mode
    - reasoning_content must be present (even empty string) on assistant messages with tool_calls
    - Tool names use $ prefix on wire (web_search → $web_search)
    - LLMRequest.output_contract is mapped to response_format. A json_schema
      contract requests strict schema enforcement; a schema-less json contract falls back
      to json_object. Schema validation / fallback_policy is handled outside the adapter.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

import structlog

from ....types import (
    CanonicalToolSchema,
    ErrorEvent,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    LLMUsage,
    MediaPart,
    Message,
    OutputContract,
    RequestContext,
    StopEvent,
    StopReason,
    TextDeltaEvent,
    TextPart,
    ThinkingEvent,
    ToolCallCompleteEvent,
    ToolCallDeltaEvent,
    ToolCallRequest,
    UsageEvent,
)
from ....observability.metrics import observe_model_latency, increment_model_errors
from ....observability.tracing import get_tracer
from ....config import Config
from ....resilience import get_breaker_configured
from ....workers.concurrency_primitives import worker_sleep
from ..base import CapabilityDescriptor
from ..prompt_caching import apply_prompt_cache_key
from ...registry import get_model_spec
from ...rendering import get_renderer
from ...rendering.base import RenderingContext
from ...specs import (
    CAP_JSON_MODE,
    CAP_STREAM,
    CAP_SYSTEM_PROMPT,
    CAP_TOOL_USE,
    CAP_VISION,
)
from ....artifacts import get_artifact_store
from .message_history_sanitizer import sanitize_orphan_tool_call_messages
from .chat_completions_deltas import ChatCompletionsToolCallAssembler
from ..tool_call_codec import (
    tool_call_requests_to_openai_chat,
    tool_calls_from_message,
)

logger = structlog.get_logger(__name__)


# =============================================================================
# Moonshot-Specific Wire Format Helpers
# =============================================================================


def _get_reasoning_content(m: Any) -> Optional[str]:
    """Extract reasoning_content from a message (Message or dict)."""
    rc = getattr(m, "reasoning_content", None)
    if rc is None and isinstance(m, dict):
        rc = m.get("reasoning_content")
    return rc if (rc is not None and isinstance(rc, str)) else None


def _map_tool_name_to_wire(canonical_name: str) -> str:
    """Map canonical tool names to Moonshot wire format (web_search → $web_search)."""
    name = (canonical_name or "").strip()
    if name in ("web_search", "moonshot.web_search"):
        return "$web_search"
    return name


def _map_tool_name_from_wire(wire_name: str) -> str:
    """Map Moonshot wire tool names back to canonical ($web_search → web_search)."""
    name = (wire_name or "").strip()
    if name == "$web_search":
        return "web_search"
    return name


def _moonshot_tool_calls_wire(m: Any) -> Optional[List[Dict[str, Any]]]:
    """Render Message tool calls as Moonshot Chat Completions wire dicts (ADR-0137)."""
    calls = tool_calls_from_message(m)
    if not calls:
        return None
    return _map_tool_calls_to_wire(tool_call_requests_to_openai_chat(calls))


def _map_tool_calls_to_wire(tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Map tool calls to Moonshot wire format (function names)."""
    if not tool_calls:
        return tool_calls
    out = []
    for tc in tool_calls:
        tc = dict(tc)
        fn = tc.get("function")
        if isinstance(fn, dict):
            name = fn.get("name", "")
            wire_name = _map_tool_name_to_wire(name)
            if wire_name != name:
                fn = dict(fn)
                fn["name"] = wire_name
                tc["function"] = fn
        out.append(tc)
    return out


def _canonical_tools_to_moonshot(
    tools: Optional[List[CanonicalToolSchema]],
) -> Optional[List[Dict[str, Any]]]:
    """Map canonical tool schemas to Moonshot wire format."""
    if not tools:
        return None
    out: List[Dict[str, Any]] = []
    for t in tools:
        name = (t.name or "").strip()
        # Moonshot builtin web_search uses special format
        if name in ("moonshot.web_search", "web_search"):
            out.append({"type": "builtin_function", "function": {"name": "$web_search"}})
            continue
        # Regular tools use standard OpenAI format
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.json_schema,
                },
            }
        )
    return out


def _build_response_format(request: LLMRequest) -> Optional[Dict[str, Any]]:
    """Map a canonical OutputContract to a Moonshot/OpenAI `response_format` (ADR-0114).

    Moonshot's Chat Completions API is OpenAI-compatible. When the request asks
    for JSON with a schema we request strict ``json_schema`` enforcement; when
    only ``format="json"`` is requested (no schema) we fall back to
    ``json_object`` so the output is at least valid JSON. Schema validation and
    the OutputContract.fallback_policy are handled outside the adapter
    (ADR-0064 R2); the adapter only attaches the provider config.
    """
    contract: Optional[OutputContract] = request.output_contract
    if not contract or contract.format != "json":
        return None
    if contract.json_schema:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "structured_output",
                "schema": contract.json_schema,
                "strict": bool(contract.strict),
            },
        }
    return {"type": "json_object"}


def _tools_include_web_search(moonshot_tools: List[Dict[str, Any]]) -> bool:
    """True if the tool list includes Moonshot's $web_search (incompatible with thinking)."""
    for t in moonshot_tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if isinstance(fn, dict) and (fn.get("name") or "").strip() == "$web_search":
            return True
    return False


def _build_kimi_thinking_params(enable_thinking: bool, web_search_in_use: bool) -> Dict[str, Any]:
    """
    Return extra_body and temperature for kimi-k2.5 thinking mode (Moonshot docs).

    $web_search is incompatible with thinking; all other tool combinations respect the
    enable_thinking flag. When thinking is off (or forced off by web_search), temperature
    must be 0.6; when on, temperature must be 1.0.
    """
    if web_search_in_use or not enable_thinking:
        return {"extra_body": {"thinking": {"type": "disabled"}}, "temperature": 0.6}
    return {"extra_body": {"thinking": {"type": "enabled"}}, "temperature": 1.0}


def _is_kimi_k3(model_name: str) -> bool:
    """True for Kimi K3 model IDs (always-on reasoning_effort path)."""
    return (model_name or "").strip().lower() == "kimi-k3"


def _is_kimi_k25(model_name: str) -> bool:
    """True for Kimi K2.5 (toggleable thinking{} + temperature constraints)."""
    return (model_name or "").strip().lower() == "kimi-k2.5"


def _apply_moonshot_generation_params(
    params: Dict[str, Any],
    *,
    model_name: str,
    settings: Dict[str, Any],
    moonshot_tools: Optional[List[Dict[str, Any]]],
) -> bool:
    """
    Attach tools and model-specific generation settings to Chat Completions params.

    Returns:
        True when ThinkingEvent / reasoning_content should be surfaced to callers.
    """
    enable_thinking = bool(settings.get("enable_thinking", False))
    web_search_in_use = _tools_include_web_search(moonshot_tools or [])

    if moonshot_tools:
        params["tools"] = moonshot_tools

    from ...output_limits import resolve_max_output_tokens

    if _is_kimi_k3(model_name):
        # K3: always-on thinking via top-level reasoning_effort (only "max" today).
        # Do not send K2.x thinking{} or fixed sampling overrides (API rejects / ignores).
        # Prefer max_completion_tokens (wire key) when both request keys are set.
        max_out = None
        try:
            raw_mct = settings.get("max_completion_tokens")
            if raw_mct is not None:
                max_out = int(raw_mct)
                if max_out <= 0:
                    max_out = None
        except (TypeError, ValueError):
            max_out = None
        if max_out is None:
            max_out = resolve_max_output_tokens(
                settings,
                provider="moonshot",
                model_name=model_name,
                fallback=None,
            )
        if max_out is not None:
            params["max_completion_tokens"] = max_out
        effort = str(settings.get("reasoning_effort") or "max").lower().strip()
        params["reasoning_effort"] = effort if effort == "max" else "max"
        return True

    max_tokens = resolve_max_output_tokens(
        settings,
        provider="moonshot",
        model_name=model_name,
        fallback=None,
    )
    if max_tokens is not None:
        params["max_tokens"] = max_tokens

    if _is_kimi_k25(model_name):
        params.update(_build_kimi_thinking_params(enable_thinking, web_search_in_use))
        if web_search_in_use:
            logger.info(
                "moonshot_thinking_disabled",
                model=model_name,
                reason="web_search_in_use",
                temperature=0.6,
            )
        return enable_thinking

    temp = settings.get("temperature")
    if temp is not None:
        params["temperature"] = temp
    return enable_thinking


def _format_messages_for_moonshot(
    messages: List[Any],
    *,
    model_name: str,
    request_context: Optional[RequestContext],
) -> List[Dict[str, Any]]:
    """
    Format canonical messages into Moonshot Chat Completions wire schema.
    
    Handles both Message objects and dict messages (after serialization between commands).
    
    Key Moonshot requirements:
    - reasoning_content: Required (even empty string) on assistant messages with tool_calls
    - Tool names: Use $ prefix for builtin tools (web_search → $web_search)
    """
    enable_multimodal = bool(getattr(request_context, "enable_multimodal", False))
    has_parts = any(bool(getattr(m, "content_parts", None)) for m in messages)

    formatted: List[Dict[str, Any]] = []

    if has_parts:
        # Detect image parts across all messages
        has_image_parts = False
        for m in messages:
            parts = getattr(m, "content_parts", None) or []
            for part in parts:
                if isinstance(part, MediaPart) and part.media_type == "image":
                    has_image_parts = True
                    break
                if isinstance(part, dict) and part.get("type") == "media" and part.get("media_type") == "image":
                    has_image_parts = True
                    break
            if has_image_parts:
                break

        # If any image parts exist and multimodal is enabled, use the renderer
        if has_image_parts and enable_multimodal:
            if not request_context or not request_context.tenant_id or not request_context.principal_id:
                raise ValueError(
                    "Multimodal rendering requires RequestContext with tenant_id and principal_id."
                )

            renderer = get_renderer("canonical")
            ctx = RenderingContext(
                provider="moonshot",
                model_name=model_name,
                tenant_id=str(request_context.tenant_id),
                principal_id=str(request_context.principal_id),
                motet_id=str(request_context.motet_id) if request_context.motet_id is not None else None,
                artifact_store=get_artifact_store(),
                max_images=int(getattr(request_context, "max_images", 8)),
                max_image_bytes=int(getattr(request_context, "max_image_bytes", 20 * 1024 * 1024)),
            )
            rendered_messages = renderer.render(messages, context=ctx)

            for m in rendered_messages:
                msg: Dict[str, Any] = {"role": m.role}
                if hasattr(m, "name") and m.name:
                    msg["name"] = _map_tool_name_to_wire(m.name)
                wire_calls = _moonshot_tool_calls_wire(m)
                if wire_calls:
                    msg["tool_calls"] = wire_calls
                if hasattr(m, "tool_call_id") and m.tool_call_id:
                    msg["tool_call_id"] = m.tool_call_id

                parts = getattr(m, "content_parts", None) or []
                if parts:
                    content_blocks: List[Dict[str, Any]] = []
                    for part in parts:
                        if isinstance(part, TextPart):
                            content_blocks.append({"type": "text", "text": part.text})
                            continue
                        if isinstance(part, MediaPart) and part.media_type == "image":
                            if not isinstance(part.mime_type, str) or not part.mime_type.startswith("image/"):
                                raise ValueError(f"Invalid image mime_type for MediaPart: {part.mime_type!r}")
                            if not isinstance(part.base64_data, str) or not part.base64_data:
                                raise ValueError("Expected MediaPart.base64_data after canonical rendering")
                            data_url = f"data:{part.mime_type};base64,{part.base64_data}"
                            image_block: Dict[str, Any] = {"type": "image_url", "image_url": {"url": data_url}}
                            if (part.detail or "") in {"low", "high"}:
                                image_block["image_url"]["detail"] = part.detail
                            content_blocks.append(image_block)
                            continue
                        raise ValueError(f"Unsupported content part for Moonshot multimodal: {type(part).__name__}")
                    msg["content"] = content_blocks
                else:
                    msg["content"] = m.content

                # CRITICAL: Moonshot requires reasoning_content on assistant messages with tool_calls
                if m.role == "assistant":
                    msg["reasoning_content"] = _get_reasoning_content(m) or ""

                formatted.append(msg)

            return formatted

        # Text-only parts path
        for m in messages:
            parts = getattr(m, "content_parts", None) or []
            text_chunks: List[str] = []
            for part in parts:
                if isinstance(part, TextPart):
                    text_chunks.append(part.text)
                elif isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                    text_chunks.append(part["text"])

            msg_dict: Dict[str, Any] = {
                "role": m.role,
                "content": ("\n\n".join(text_chunks) if text_chunks else m.content),
            }
            if hasattr(m, "name") and m.name:
                msg_dict["name"] = _map_tool_name_to_wire(m.name)
            wire_calls = _moonshot_tool_calls_wire(m)
            if wire_calls:
                msg_dict["tool_calls"] = wire_calls
            if hasattr(m, "tool_call_id") and m.tool_call_id:
                msg_dict["tool_call_id"] = m.tool_call_id
            
            # CRITICAL: Moonshot requires reasoning_content on assistant messages
            if m.role == "assistant":
                msg_dict["reasoning_content"] = _get_reasoning_content(m) or ""
            
            formatted.append(msg_dict)
        return formatted

    # No content_parts - simple message path
    for m in messages:
        if isinstance(m, dict):
            m_d: Dict[str, Any] = m
            role = str(m_d.get("role") or "user")
            content: Any = m_d.get("content")
            if content is None:
                content = ""
            name_val = m_d.get("name")
            tool_call_id_raw = m_d.get("tool_call_id")
        else:
            role = str(getattr(m, "role", None) or "user")
            content = getattr(m, "content", None)
            if content is None:
                content = ""
            name_val = getattr(m, "name", None)
            tool_call_id_raw = getattr(m, "tool_call_id", None)

        # NOTE: no type annotation here on purpose; `msg_dict` is already annotated in the
        # text-only-parts branch above, and re-annotating in the same function scope trips
        # basedpyright's "obscured by a declaration of the same name" check.
        msg_dict = {"role": role, "content": content}

        if name_val:
            msg_dict["name"] = _map_tool_name_to_wire(str(name_val))

        wire_calls = _moonshot_tool_calls_wire(m)
        if wire_calls:
            msg_dict["tool_calls"] = wire_calls

        if tool_call_id_raw:
            msg_dict["tool_call_id"] = tool_call_id_raw

        # CRITICAL: Moonshot requires reasoning_content on assistant messages with tool_calls
        if role == "assistant":
            msg_dict["reasoning_content"] = _get_reasoning_content(m) or ""

        formatted.append(msg_dict)
    
    return formatted


def _ensure_reasoning_content(messages: List[Dict[str, Any]]) -> None:
    """
    Final guard: Ensure reasoning_content exists on all assistant messages with tool_calls.
    Mutates the messages list in place.
    """
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            rc = msg.get("reasoning_content")
            if not isinstance(rc, str):
                msg["reasoning_content"] = ""


def _build_moonshot_client(credentials: Optional[Dict[str, Any]]) -> Any:
    """Build OpenAI client configured for Moonshot API."""
    try:
        from openai import OpenAI
    except Exception as exc:
        raise RuntimeError("openai package not available") from exc

    creds = credentials or {}
    api_key = creds.get("moonshot_api_key") or creds.get("api_key")
    base_url = creds.get("base_url") or "https://api.moonshot.ai/v1"
    return OpenAI(api_key=api_key, base_url=base_url)


def _finish_reason_to_stop_reason(finish_reason: str, *, has_tool_calls: bool) -> StopReason:
    """Map Moonshot finish_reason to canonical StopReason."""
    if has_tool_calls:
        return StopReason.TOOL_CALLS
    mapped = {
        "stop": StopReason.NATURAL_STOP,
        "length": StopReason.LENGTH_LIMIT,
        "content_filter": StopReason.SAFETY_FILTER,
        "tool_calls": StopReason.TOOL_CALLS,
        "function_call": StopReason.TOOL_CALLS,
    }.get(finish_reason, StopReason.NATURAL_STOP)
    return mapped


# =============================================================================
# Moonshot Chat Completions Adapter
# =============================================================================


@dataclass
class MoonshotChatCompletionsAdapter:
    """
    Moonshot/Kimi adapter for Chat Completions API.
    
    Handles Moonshot-specific requirements:
    - reasoning_content on assistant tool-call messages (K2.x and K3)
    - $web_search builtin tool mapping (K2.x)
    - K2.5: toggleable thinking{} + temperature constraints
    - K3: always-on reasoning_effort + max_completion_tokens
    """
    
    provider: str
    adapter_name: str
    credentials: Optional[Dict[str, Any]] = None

    def capabilities(self, *, model: str) -> CapabilityDescriptor:
        """Return capability descriptor for Moonshot models."""
        spec = get_model_spec(self.provider, model)
        caps = set(spec.capabilities) if spec else set()
        return CapabilityDescriptor(
            provider=self.provider,
            model=model,
            supports_streaming=CAP_STREAM in caps,
            supports_tools=CAP_TOOL_USE in caps,
            supports_parallel_tool_calls=CAP_TOOL_USE in caps,
            supports_tool_call_id=True,
            supports_vision=CAP_VISION in caps,
            supports_json_mode=CAP_JSON_MODE in caps,
            # ADR-0114: mapped to response_format json_schema (adapter-level, model-dependent).
            supports_json_schema_strict=CAP_JSON_MODE in caps,
            supports_stateful_sessions=False,
            supports_builtin_tools=bool(getattr(spec, "supported_builtin_tools", None)),
            supports_system_prompt=CAP_SYSTEM_PROMPT in caps if spec else True,
            supports_reasoning=("reasoning" in caps),
            provider_metadata={"adapter": "moonshot_chat_completions"},
        )

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Execute non-streaming completion with Moonshot."""
        settings = request.model_settings or {}
        model_name = settings.get("model_name") or settings.get("model") or ""
        if not model_name:
            raise ValueError("LLMRequest.model_settings.model_name is required for Moonshot adapter")

        client = _build_moonshot_client(self.credentials)
        moonshot_tools = _canonical_tools_to_moonshot(request.tools)
        safe_messages, sanitize_stats = sanitize_orphan_tool_call_messages(request.messages)
        if sanitize_stats["removed_assistant_calls"] > 0 or sanitize_stats["removed_tool_messages"] > 0:
            logger.warning(
                "provider_boundary_orphan_tool_calls_pruned",
                provider=self.provider,
                model=model_name,
                removed_assistant_calls=sanitize_stats["removed_assistant_calls"],
                removed_tool_messages=sanitize_stats["removed_tool_messages"],
            )
        messages = safe_messages
        formatted = _format_messages_for_moonshot(
            messages,
            model_name=model_name,
            request_context=request.request_context,
        )
        # Final guard - ensure reasoning_content exists
        _ensure_reasoning_content(formatted)
        
        # Debug logging for troubleshooting (only when explicitly needed)
        # Removed: structlog handles log level filtering internally

        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                start = time.perf_counter()
                tracer = get_tracer("imf.model")
                with tracer.start_as_current_span(f"model:moonshot:{model_name}"):
                    cfg = Config()
                    br = get_breaker_configured(
                        f"model:moonshot:{model_name}",
                        default_failure_threshold=int(getattr(cfg, "breaker_model_failure_threshold", 5) or 5),
                        default_reset_timeout_seconds=float(getattr(cfg, "breaker_model_reset_timeout_seconds", 60.0) or 60.0),
                    )

                    def _call() -> Any:
                        params: Dict[str, Any] = {
                            "model": model_name,
                            "messages": formatted,
                        }
                        # ADR-0114: structured output via response_format when requested.
                        response_format = _build_response_format(request)
                        if response_format is not None:
                            params["response_format"] = response_format
                            logger.info(
                                "moonshot_structured_output_constrained",
                                model=model_name,
                                mode=response_format.get("type"),
                            )
                        _apply_moonshot_generation_params(
                            params,
                            model_name=model_name,
                            settings=settings,
                            moonshot_tools=moonshot_tools,
                        )
                        # ADR-0124: prompt_cache_key when enabled + capable.
                        apply_prompt_cache_key(params, request, provider=self.provider)
                        return client.chat.completions.create(**params)

                    result = br.call(_call)

                raw = result.model_dump() if hasattr(result, "model_dump") else {}
                finish_reason = None
                if isinstance(raw, dict) and raw.get("choices"):
                    finish_reason = raw["choices"][0].get("finish_reason")

                tool_calls: List[ToolCallRequest] = []
                tool_calls_raw = None
                if hasattr(result, "choices") and result.choices:
                    msg = result.choices[0].message
                    if hasattr(msg, "tool_calls") and msg.tool_calls:
                        tool_calls_raw = msg.tool_calls
                if tool_calls_raw:
                    raw_tool_calls: List[Dict[str, Any]] = []
                    for tc in tool_calls_raw:
                        raw_tool_calls.append({
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        })
                    if isinstance(raw, dict):
                        raw["tool_calls"] = raw_tool_calls
                    tool_calls = [
                        tc.model_copy(
                            update={"tool_name": _map_tool_name_from_wire(tc.tool_name)}
                        )
                        for tc in ChatCompletionsToolCallAssembler().ingest_complete_calls(
                            tool_calls_raw
                        )
                    ]

                has_tool_calls = bool(tool_calls)
                stop_reason = _finish_reason_to_stop_reason(str(finish_reason or "stop"), has_tool_calls=has_tool_calls)

                usage = None
                if hasattr(result, "usage") and result.usage:
                    u = result.usage
                    # Moonshot may have cached_tokens at top level
                    cached_tokens = getattr(u, "cached_tokens", None)
                    prompt_details = getattr(u, "prompt_tokens_details", None)
                    if cached_tokens is None and prompt_details:
                        cached_tokens = getattr(prompt_details, "cached_tokens", None)
                    
                    completion_details = getattr(u, "completion_tokens_details", None)
                    reasoning_tokens = getattr(completion_details, "reasoning_tokens", None) if completion_details else None
                    
                    usage = LLMUsage(
                        prompt_tokens=getattr(u, "prompt_tokens", None),
                        output_tokens=getattr(u, "completion_tokens", None),
                        total_tokens=getattr(u, "total_tokens", None),
                        cache_read_tokens=cached_tokens,
                        reasoning_tokens=reasoning_tokens,
                        provider_metadata=(u.model_dump() if hasattr(u, "model_dump") else None),
                    )

                output_text = None
                if hasattr(result, "choices") and result.choices:
                    output_text = result.choices[0].message.content or ""

                output_items: List[Any] = []
                if output_text:
                    output_items.append(TextPart(text=output_text))
                output_items.extend(tool_calls)

                # Capture reasoning_content for persistence
                reasoning_content: Optional[str] = None
                if hasattr(result, "choices") and result.choices:
                    msg = result.choices[0].message
                    reasoning_content = getattr(msg, "reasoning_content", None)
                    if reasoning_content is not None and not isinstance(reasoning_content, str):
                        reasoning_content = None

                observe_model_latency("moonshot", model_name, time.perf_counter() - start)

                return LLMResponse(
                    output_text=output_text or None,
                    output_items=output_items,
                    stop_reason=stop_reason,
                    usage=usage,
                    raw_provider_metadata={"raw": raw},
                    reasoning_content=reasoning_content,
                )
            except Exception as exc:
                last_exc = exc
                error_type = type(exc).__name__
                error_msg = str(exc)
                try:
                    structlog.get_logger().warning(
                        "moonshot_complete_error",
                        model=model_name,
                        error_type=error_type,
                        error_message=error_msg[:200],
                        attempt=attempt + 1,
                        max_attempts=3,
                    )
                    increment_model_errors(self.provider, model_name, error_type)
                except Exception:
                    pass  # logging best-effort; must not crash retry loop

                if attempt == 2:
                    try:
                        structlog.get_logger().error(
                            "moonshot_complete_failed_after_retries",
                            model=model_name,
                            error_type=error_type,
                            error_message=error_msg[:200],
                            total_attempts=3,
                        )
                    except Exception:
                        pass  # logging best-effort; must not crash retry loop
                worker_sleep(0.5 * (attempt + 1))

        raise last_exc  # type: ignore[misc]

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]:
        """Execute streaming completion with Moonshot."""
        settings = request.model_settings or {}
        model_name = settings.get("model_name") or settings.get("model") or ""
        if not model_name:
            raise ValueError("LLMRequest.model_settings.model_name is required for Moonshot adapter")

        client = _build_moonshot_client(self.credentials)
        moonshot_tools = _canonical_tools_to_moonshot(request.tools)
        
        # Debug: Log input message types and structure BEFORE formatting
        try:
            for idx, m in enumerate(request.messages):
                tc_list = tool_calls_from_message(m)
                if tc_list:
                    tc_sample = tc_list[0]
                    role_dbg = (
                        m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
                    )
                    logger.debug(
                        "moonshot_input_message_debug",
                        index=idx,
                        msg_type=type(m).__name__,
                        role=role_dbg,
                        has_tool_calls=True,
                        tool_calls_count=len(tc_list),
                        tool_name=tc_sample.tool_name,
                    )
        except Exception as e:
            logger.warning("moonshot_input_debug_failed", error=str(e))
        
        safe_messages, sanitize_stats = sanitize_orphan_tool_call_messages(request.messages)
        if sanitize_stats["removed_assistant_calls"] > 0 or sanitize_stats["removed_tool_messages"] > 0:
            logger.warning(
                "provider_boundary_orphan_tool_calls_pruned",
                provider=self.provider,
                model=model_name,
                removed_assistant_calls=sanitize_stats["removed_assistant_calls"],
                removed_tool_messages=sanitize_stats["removed_tool_messages"],
            )
        messages = safe_messages
        
        # Debug: Log message types AFTER sanitization
        try:
            for idx, m in enumerate(messages):
                role = (
                    m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
                )
                if role == "assistant":
                    tc = tool_calls_from_message(m)
                    logger.debug(
                        "moonshot_sanitized_message_debug",
                        index=idx,
                        msg_type=type(m).__name__,
                        role=role,
                        has_tool_calls=bool(tc),
                        tool_name=tc[0].tool_name if tc else None,
                    )
        except Exception as e:
            logger.warning("moonshot_sanitized_debug_failed", error=str(e))
        
        formatted = _format_messages_for_moonshot(
            messages,
            model_name=model_name,
            request_context=request.request_context,
        )
        # Final guard - ensure reasoning_content exists
        _ensure_reasoning_content(formatted)
        
        # Debug logging for troubleshooting (only when explicitly needed)
        # Removed: structlog handles log level filtering internally

        assembler = ChatCompletionsToolCallAssembler()
        finish_reason = "stop"
        try:
            tracer = get_tracer("imf.model")
            with tracer.start_as_current_span(f"model_stream:moonshot:{model_name}"):
                stream_params: Dict[str, Any] = {
                    "model": model_name,
                    "messages": formatted,
                    "stream": True,
                }
                # ADR-0114: structured output via response_format when requested.
                response_format = _build_response_format(request)
                if response_format is not None:
                    stream_params["response_format"] = response_format
                    logger.info(
                        "moonshot_structured_output_constrained",
                        model=model_name,
                        mode=response_format.get("type"),
                    )
                # ADR-0064: K2.5 respects enable_thinking; K3 always surfaces reasoning.
                emit_thinking = _apply_moonshot_generation_params(
                    stream_params,
                    model_name=model_name,
                    settings=settings,
                    moonshot_tools=moonshot_tools,
                )
                # ADR-0124: prompt_cache_key when enabled + capable.
                apply_prompt_cache_key(stream_params, request, provider=self.provider)
                # Enable usage reporting in stream
                stream_params["stream_options"] = {"include_usage": True}

                # Critical debug: Log the exact messages being sent
                for idx, msg in enumerate(formatted):
                    if msg.get("role") == "assistant" and msg.get("tool_calls"):
                        logger.warning(
                            "moonshot_pre_api_check",
                            index=idx,
                            role=msg.get("role"),
                            has_reasoning_content="reasoning_content" in msg,
                            reasoning_content_value=repr(msg.get("reasoning_content"))[:50],
                            reasoning_content_type=type(msg.get("reasoning_content")).__name__,
                            tool_calls_format=repr(msg.get("tool_calls", [])[:1])[:200] if msg.get("tool_calls") else None,
                        )

                stream = client.chat.completions.create(**stream_params)

                final_usage: Optional[LLMUsage] = None
                reasoning_chunks: List[str] = []

                for event in stream:
                    choice = None
                    if getattr(event, "choices", None) and len(event.choices) > 0:
                        choice = event.choices[0]
                    try:
                        delta = choice.delta if choice is not None else None
                    except Exception:
                        delta = None

                    if delta and getattr(delta, "content", None):
                        yield TextDeltaEvent(text=delta.content)

                    # Moonshot may stream reasoning_content; emit when requested (K2.5) or always (K3)
                    if emit_thinking and delta and getattr(delta, "reasoning_content", None):
                        rct = delta.reasoning_content
                        if isinstance(rct, str):
                            reasoning_chunks.append(rct)
                            yield ThinkingEvent(text=rct, is_complete=False)

                    if delta and getattr(delta, "tool_calls", None):
                        for tool_call_delta in delta.tool_calls:
                            ev = assembler.apply_delta(tool_call_delta)
                            if ev is not None:
                                yield ev

                    if choice is not None and getattr(choice, "finish_reason", None):
                        finish_reason = choice.finish_reason

                    # Capture usage from stream (sent in final chunk)
                    if hasattr(event, "usage") and event.usage is not None:
                        usage_obj = event.usage
                        cached_tokens = getattr(usage_obj, "cached_tokens", None)
                        prompt_details = getattr(usage_obj, "prompt_tokens_details", None)
                        if cached_tokens is None and prompt_details:
                            cached_tokens = getattr(prompt_details, "cached_tokens", None)
                        
                        completion_details = getattr(usage_obj, "completion_tokens_details", None)
                        reasoning_tokens = getattr(completion_details, "reasoning_tokens", None) if completion_details else None

                        final_usage = LLMUsage(
                            prompt_tokens=getattr(usage_obj, "prompt_tokens", None),
                            output_tokens=getattr(usage_obj, "completion_tokens", None),
                            total_tokens=getattr(usage_obj, "total_tokens", None),
                            cache_read_tokens=cached_tokens,
                            reasoning_tokens=reasoning_tokens,
                        )

            tool_calls_list = []
            for complete_ev in assembler.complete():
                mapped = complete_ev.model_copy(
                    update={"tool_name": _map_tool_name_from_wire(complete_ev.tool_name)}
                )
                tool_calls_list.append(mapped)
                yield mapped

            # Signal end of reasoning when we surfaced thinking for this model
            if emit_thinking and reasoning_chunks:
                yield ThinkingEvent(text="", is_complete=True)

            # Emit usage before stop
            if final_usage is not None:
                yield UsageEvent(usage=final_usage)

            yield StopEvent(reason=_finish_reason_to_stop_reason(finish_reason, has_tool_calls=bool(tool_calls_list)))
            return
        except Exception as exc:
            error_type = type(exc).__name__
            error_msg = str(exc)
            try:
                structlog.get_logger().warning(
                    "moonshot_stream_error",
                    model=model_name,
                    error_type=error_type,
                    error_message=error_msg[:200],
                )
                increment_model_errors(self.provider, model_name, error_type)
            except Exception:
                pass  # logging best-effort; must not crash error propagation
            yield ErrorEvent(error_type=error_type, message=error_msg)
            yield StopEvent(reason=StopReason.ERROR)
            return
