"""
Motet - OpenAI Chat Completions Adapter

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    OpenAI provider adapter that targets the OpenAI **Chat Completions API** (`/v1/chat/completions`)
    and translates into Motet canonical protocol types.

    Handles retries, circuit breaker, multimodal rendering, and Chat Completions
    streaming semantics while translating into canonical outputs/events.

    NOTE: For Moonshot/Kimi models, use MoonshotChatCompletionsAdapter instead - it handles
    Moonshot-specific wire format requirements (reasoning_content, $web_search, etc.).
    For DeepSeek V4, use DeepSeekChatCompletionsAdapter (thinking toggle + reasoning_content).

    when ``enable_prompt_caching`` is set and the model has CAP_PROMPT_CACHING,
    sets ``prompt_cache_key`` from ``conversation_id`` (complete + stream).

Dependencies:
    - openai: OpenAI Python SDK (sync)
    - motet.core.resilience: circuit breaker integration
    - motet.core.observability: tracing + metrics
    - motet.core.types: canonical protocol models (LLMRequest/LLMResponse, stream events, stop reasons)
    - motet.core.models.registry/specs: model spec lookup for capabilities

Usage:
    adapter = OpenAIChatCompletionsAdapter(provider="openai", adapter_name="chat_completions", credentials={...})
    resp = adapter.complete(LLMRequest(messages=[...], tools=[...]))

Notes:
    - Tool calls are emitted as `ToolCallCompleteEvent` on stream completion.
    - This adapter is provider-agnostic for OpenAI-compatible APIs (standard format only).
    - LLMRequest.output_contract maps to `response_format` — strict
      `json_schema` when a schema is present, `json_object` (JSON mode) for bare
      format="json" — in both complete() and stream().
"""

from __future__ import annotations

import json
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
    normalize_reasoning_effort,
)
from ....observability.metrics import observe_model_latency, increment_model_errors
from ....observability.tracing import get_tracer
from ....config import Config
from ....resilience import get_breaker_configured
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
from ....workers.concurrency_primitives import worker_sleep
from .message_history_sanitizer import sanitize_orphan_tool_call_messages
from .chat_completions_deltas import ChatCompletionsToolCallAssembler
from ..tool_call_codec import (
    tool_call_requests_to_openai_chat,
    tool_calls_from_message,
)

logger = structlog.get_logger(__name__)


def _openai_chat_tool_calls_wire(m: Any) -> Optional[List[Dict[str, Any]]]:
    """Render Message tool calls as Chat Completions wire dicts (ADR-0137)."""
    calls = tool_calls_from_message(m)
    if not calls:
        return None
    return tool_call_requests_to_openai_chat(calls)


def _canonical_tools_to_openai(
    tools: Optional[List[CanonicalToolSchema]],
) -> Optional[List[Dict[str, Any]]]:
    """Map canonical tool schemas to OpenAI wire format."""
    if not tools:
        return None
    out: List[Dict[str, Any]] = []
    for t in tools:
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


def _finish_reason_to_stop_reason(finish_reason: str, *, has_tool_calls: bool) -> StopReason:
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


def _supports_temperature(model_name: str) -> bool:
    """Return False for OpenAI models that reject temperature."""
    model = (model_name or "").lower()
    return not (model.startswith("o1") or model.startswith("o3") or model.startswith("gpt-5"))


def _build_response_format(request: LLMRequest) -> Optional[Dict[str, Any]]:
    """Map a canonical OutputContract to a Chat Completions ``response_format`` (ADR-0114).

    With a schema we request strict ``json_schema`` enforcement; with bare
    ``format="json"`` we fall back to ``json_object`` (JSON mode: valid JSON,
    no schema adherence). Validation and OutputContract.fallback_policy are
    handled outside the adapter (ADR-0064 R2); the adapter only attaches the
    provider config. Same shape as the Moonshot (OpenAI-compatible) mapping.
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


def _temperature_for_request(model_name: str, requested: Optional[float]) -> Optional[float]:
    """
    Return the temperature value to send to the API for model_name.
    OpenAI gpt-5 only allows temperature=1; o1/o3 reject it entirely.
    """
    mn = (model_name or "").strip().lower()
    if "gpt-5" in mn:
        return 1.0
    if not _supports_temperature(model_name or ""):
        return None
    return requested if requested is not None else 0.2


def _format_messages_for_openai(
    messages: List[Message],
    *,
    model_name: str,
    request_context: Optional[RequestContext],
    provider: str = "openai",
) -> List[Dict[str, Any]]:
    """
    Format canonical messages into OpenAI Chat Completions wire schema.
    Mirrors the legacy OpenAIChatModel formatting behavior (ADR-0062).
    """
    enable_multimodal = bool(getattr(request_context, "enable_multimodal", False))
    has_parts = any(bool(getattr(m, "content_parts", None)) for m in messages)

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

        # If any image parts exist and multimodal is enabled, use the renderer (OpenAI content array).
        if has_image_parts and enable_multimodal:
            if not request_context or not request_context.tenant_id or not request_context.principal_id:
                raise ValueError(
                    "Multimodal rendering requires RequestContext with tenant_id and principal_id."
                )

            renderer = get_renderer("canonical")
            ctx = RenderingContext(
                provider=provider,
                model_name=model_name,
                tenant_id=str(request_context.tenant_id),
                principal_id=str(request_context.principal_id),
                motet_id=str(request_context.motet_id) if request_context.motet_id is not None else None,
                artifact_store=get_artifact_store(),
                max_images=int(getattr(request_context, "max_images", 8)),
                max_image_bytes=int(getattr(request_context, "max_image_bytes", 20 * 1024 * 1024)),
            )
            rendered_messages = renderer.render(messages, context=ctx)

            formatted: List[Dict[str, Any]] = []
            for m in rendered_messages:
                msg: Dict[str, Any] = {"role": m.role}
                if hasattr(m, "name") and m.name:
                    msg["name"] = m.name
                wire_calls = _openai_chat_tool_calls_wire(m)
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
                        raise ValueError(f"Unsupported content part for OpenAI chat multimodal: {type(part).__name__}")
                    msg["content"] = content_blocks
                else:
                    msg["content"] = m.content

                formatted.append(msg)

            return formatted

        # Otherwise (text-only parts, or images present but multimodal disabled),
        # flatten text parts into the standard Chat Completions string `content`.
        formatted = []
        for m in messages:
            parts = getattr(m, "content_parts", None) or []
            text_chunks: List[str] = []
            for part in parts:
                if isinstance(part, TextPart):
                    text_chunks.append(part.text)
                elif isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                    text_chunks.append(part["text"])
                else:
                    continue

            msg_dict: Dict[str, Any] = {"role": m.role, "content": ("\n\n".join(text_chunks) if text_chunks else m.content)}
            if hasattr(m, "name") and m.name:
                msg_dict["name"] = m.name
            wire_calls = _openai_chat_tool_calls_wire(m)
            if wire_calls:
                msg_dict["tool_calls"] = wire_calls
            if hasattr(m, "tool_call_id") and m.tool_call_id:
                msg_dict["tool_call_id"] = m.tool_call_id
            formatted.append(msg_dict)
        return formatted

    formatted = []
    for m in messages:
        # Support both Message and dict-like for role/content (worker may pass dicts after deserialization)
        role = m.role if hasattr(m, "role") else (m.get("role") if isinstance(m, dict) else "user")
        content = m.content if hasattr(m, "content") else (m.get("content", "") if isinstance(m, dict) else "")
        msg_dict: Dict[str, Any] = {"role": role, "content": content}
        name_val = m.name if hasattr(m, "name") and m.name else (m.get("name") if isinstance(m, dict) else None)
        if name_val:
            msg_dict["name"] = name_val
        wire_calls = _openai_chat_tool_calls_wire(m)
        if wire_calls:
            msg_dict["tool_calls"] = wire_calls
        if hasattr(m, "tool_call_id") and m.tool_call_id:
            msg_dict["tool_call_id"] = m.tool_call_id
        if isinstance(m, dict) and m.get("tool_call_id"):
            msg_dict["tool_call_id"] = m["tool_call_id"]
        formatted.append(msg_dict)
    return formatted


def _build_openai_client(credentials: Optional[Dict[str, Any]]) -> Any:
    try:
        from openai import OpenAI
    except Exception as exc:
        raise RuntimeError("openai package not available") from exc

    creds = credentials or {}
    api_key = creds.get("openai_api_key") or creds.get("api_key")
    base_url = creds.get("base_url")
    if base_url:
        return OpenAI(api_key=api_key, base_url=base_url)
    return OpenAI(api_key=api_key)


@dataclass
class OpenAIChatCompletionsAdapter:
    provider: str
    adapter_name: str
    credentials: Optional[Dict[str, Any]] = None

    def capabilities(self, *, model: str) -> CapabilityDescriptor:
        spec = get_model_spec(self.provider, model)
        caps = set(spec.capabilities) if spec else set()
        return CapabilityDescriptor(
            provider=self.provider,
            model=model,
            supports_streaming=CAP_STREAM in caps,
            supports_tools=CAP_TOOL_USE in caps,
            supports_parallel_tool_calls=CAP_TOOL_USE in caps,
            supports_tool_call_id=True,  # Chat Completions provides tool_call.id
            supports_vision=CAP_VISION in caps,
            supports_json_mode=CAP_JSON_MODE in caps,
            # ADR-0114: strict json_schema via response_format (gpt-4o+ snapshots).
            supports_json_schema_strict=CAP_JSON_MODE in caps,
            supports_stateful_sessions=False,
            supports_builtin_tools=bool(getattr(spec, "supported_builtin_tools", None)),
            supports_system_prompt=CAP_SYSTEM_PROMPT in caps if spec else True,
            supports_reasoning=("reasoning" in caps),
            provider_metadata={"adapter": "openai_chat_completions"},
        )

    def complete(self, request: LLMRequest) -> LLMResponse:
        settings = request.model_settings or {}
        model_name = settings.get("model_name") or settings.get("model") or ""
        if not model_name:
            raise ValueError("LLMRequest.model_settings.model_name is required for OpenAI chat_completions adapter")

        client = _build_openai_client(self.credentials)
        openai_tools = _canonical_tools_to_openai(request.tools)
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
        formatted = _format_messages_for_openai(
            messages,
            model_name=model_name,
            request_context=request.request_context,
            provider=self.provider,
        )

        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                start = time.perf_counter()
                tracer = get_tracer("imf.model")
                with tracer.start_as_current_span(f"model:{self.provider}:{model_name}"):
                    cfg = Config()
                    br = get_breaker_configured(
                        f"model:{self.provider}:{model_name}",
                        default_failure_threshold=int(getattr(cfg, "breaker_model_failure_threshold", 5) or 5),
                        default_reset_timeout_seconds=float(getattr(cfg, "breaker_model_reset_timeout_seconds", 60.0) or 60.0),
                    )

                    def _call() -> Any:
                        params: Dict[str, Any] = {
                            "model": model_name,
                            "messages": formatted,
                        }
                        temperature = _temperature_for_request(model_name, settings.get("temperature"))
                        from ...output_limits import resolve_max_output_tokens

                        max_tokens = resolve_max_output_tokens(
                            settings,
                            provider=self.provider,
                            model_name=model_name,
                            fallback=None,
                        )
                        if max_tokens is not None:
                            if model_name and ("gpt-4.1" in model_name or "gpt-5" in model_name):
                                params["max_completion_tokens"] = max_tokens
                            else:
                                params["max_tokens"] = max_tokens
                        if temperature is not None:
                            params["temperature"] = temperature
                        if openai_tools:
                            params["tools"] = openai_tools
                        # ADR-0114: structured output via response_format when requested.
                        response_format = _build_response_format(request)
                        if response_format is not None:
                            params["response_format"] = response_format
                            logger.info(
                                "openai_chat_completions_structured_output_constrained",
                                model=model_name,
                                mode=response_format.get("type"),
                            )
                        # ADR-0064: Enable reasoning for gpt-5/o-series models.
                        # Chat Completions rejects Motet ``max`` even on gpt-5.6
                        # (Responses-only); always clamp to ``xhigh``.
                        if settings.get("enable_thinking"):
                            params["reasoning_effort"] = normalize_reasoning_effort(
                                settings.get("reasoning_effort"),
                                default="medium",
                                supported=("low", "medium", "high", "xhigh"),
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
                        raw_tool_calls.append(
                            {
                                "id": tc.id,
                                "type": tc.type,
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments,
                                },
                            }
                        )
                    if isinstance(raw, dict):
                        raw["tool_calls"] = raw_tool_calls
                    tool_calls = ChatCompletionsToolCallAssembler().ingest_complete_calls(
                        tool_calls_raw
                    )

                has_tool_calls = bool(tool_calls)
                stop_reason = _finish_reason_to_stop_reason(str(finish_reason or "stop"), has_tool_calls=has_tool_calls)

                usage = None
                if hasattr(result, "usage") and result.usage:
                    u = result.usage
                    # OpenAI: cached_tokens under prompt_tokens_details
                    prompt_details = getattr(u, "prompt_tokens_details", None)
                    cached_tokens = getattr(prompt_details, "cached_tokens", None) if prompt_details else None
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

                # Capture reasoning_content if present (some OpenAI models may return it)
                reasoning_content: Optional[str] = None
                if hasattr(result, "choices") and result.choices:
                    msg = result.choices[0].message
                    reasoning_content = getattr(msg, "reasoning_content", None)
                    if reasoning_content is not None and not isinstance(reasoning_content, str):
                        reasoning_content = None

                observe_model_latency("openai", model_name, time.perf_counter() - start)

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
                        "openai_chat_completions_complete_error",
                        model=model_name,
                        error_type=error_type,
                        error_message=error_msg[:200],
                        attempt=attempt + 1,
                        max_attempts=3,
                    )
                    increment_model_errors(self.provider, model_name, error_type)
                except Exception:
                    pass  # logging fallback; must not crash retry loop

                if attempt == 2:
                    try:
                        structlog.get_logger().error(
                            "openai_chat_completions_complete_failed_after_retries",
                            model=model_name,
                            error_type=error_type,
                            error_message=error_msg[:200],
                            total_attempts=3,
                        )
                    except Exception:
                        pass  # logging fallback; must not crash retry loop
                worker_sleep(0.5 * (attempt + 1))

        raise last_exc  # type: ignore[misc]

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]:
        settings = request.model_settings or {}
        model_name = settings.get("model_name") or settings.get("model") or ""
        if not model_name:
            raise ValueError("LLMRequest.model_settings.model_name is required for OpenAI chat_completions adapter")

        client = _build_openai_client(self.credentials)
        openai_tools = _canonical_tools_to_openai(request.tools)
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
        formatted = _format_messages_for_openai(
            messages,
            model_name=model_name,
            request_context=request.request_context,
            provider=self.provider,
        )

        assembler = ChatCompletionsToolCallAssembler()
        finish_reason = "stop"
        try:
            tracer = get_tracer("imf.model")
            with tracer.start_as_current_span(f"model_stream:{self.provider}:{model_name}"):
                stream_params: Dict[str, Any] = {
                    "model": model_name,
                    "messages": formatted,
                    "stream": True,
                }
                temperature = _temperature_for_request(model_name, settings.get("temperature"))
                from ...output_limits import resolve_max_output_tokens

                max_tokens = resolve_max_output_tokens(
                    settings,
                    provider=self.provider,
                    model_name=model_name,
                    fallback=None,
                )
                if max_tokens is not None:
                    if model_name and ("gpt-4.1" in model_name or "gpt-5" in model_name):
                        stream_params["max_completion_tokens"] = max_tokens
                    else:
                        stream_params["max_tokens"] = max_tokens
                if temperature is not None:
                    stream_params["temperature"] = temperature
                if openai_tools:
                    stream_params["tools"] = openai_tools

                # ADR-0114: structured output via response_format when requested.
                response_format = _build_response_format(request)
                if response_format is not None:
                    stream_params["response_format"] = response_format
                    logger.info(
                        "openai_chat_completions_structured_output_constrained",
                        model=model_name,
                        mode=response_format.get("type"),
                    )

                # ADR-0064: Enable reasoning for gpt-5/o-series models (Chat Completions uses reasoning_effort).
                # Chat Completions rejects Motet ``max`` even on gpt-5.6
                # (Responses-only); always clamp to ``xhigh``.
                if settings.get("enable_thinking"):
                    stream_params["reasoning_effort"] = normalize_reasoning_effort(
                        settings.get("reasoning_effort"),
                        default="medium",
                        supported=("low", "medium", "high", "xhigh"),
                    )

                # ADR-0124: prompt_cache_key when enabled + capable.
                apply_prompt_cache_key(stream_params, request, provider=self.provider)

                # ADR-0064 R9: Enable usage reporting in stream
                stream_params["stream_options"] = {"include_usage": True}

                stream = client.chat.completions.create(**stream_params)

                # ADR-0064 R9: Track usage from streaming chunks
                final_usage: Optional[LLMUsage] = None
                # ADR-0064 R10: Accumulate reasoning (if model returns it)
                reasoning_chunks: List[str] = []

                for event in stream:
                    # With stream_options.include_usage=True, final chunk may have usage but empty choices
                    choice = None
                    if getattr(event, "choices", None) and len(event.choices) > 0:
                        choice = event.choices[0]
                    try:
                        delta = choice.delta if choice is not None else None
                    except Exception:
                        delta = None  # best-effort delta extraction

                    if delta and getattr(delta, "content", None):
                        yield TextDeltaEvent(text=delta.content)

                    # Some models may stream reasoning_content
                    if delta and getattr(delta, "reasoning_content", None):
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

                    # ADR-0064 R9: Capture usage from stream (sent in final chunk)
                    if hasattr(event, "usage") and event.usage is not None:
                        usage_obj = event.usage
                        prompt_details = getattr(usage_obj, "prompt_tokens_details", None)
                        cached_tokens = getattr(prompt_details, "cached_tokens", None) if prompt_details else None
                        completion_details = getattr(usage_obj, "completion_tokens_details", None)
                        reasoning_tokens = getattr(completion_details, "reasoning_tokens", None) if completion_details else None

                        final_usage = LLMUsage(
                            prompt_tokens=getattr(usage_obj, "prompt_tokens", None),
                            output_tokens=getattr(usage_obj, "completion_tokens", None),
                            total_tokens=getattr(usage_obj, "total_tokens", None),
                            cache_read_tokens=cached_tokens,
                            reasoning_tokens=reasoning_tokens,
                        )

            # Emit tool calls as canonical complete events
            tool_calls_list = list(assembler.complete())
            for complete_ev in tool_calls_list:
                yield complete_ev

            # ADR-0064 R10: Signal end of reasoning if we emitted any chunks
            if reasoning_chunks:
                yield ThinkingEvent(text="", is_complete=True)

            # ADR-0064 R9: Emit usage before stop
            if final_usage is not None:
                yield UsageEvent(usage=final_usage)

            yield StopEvent(reason=_finish_reason_to_stop_reason(finish_reason, has_tool_calls=bool(tool_calls_list)))
            return
        except Exception as exc:
            error_type = type(exc).__name__
            error_msg = str(exc)
            try:
                structlog.get_logger().warning(
                    "openai_chat_completions_stream_error",
                    model=model_name,
                    error_type=error_type,
                    error_message=error_msg[:200],
                    attempt=1,
                    max_attempts=1,
                )
                increment_model_errors(self.provider, model_name, error_type)
            except Exception:
                pass  # logging fallback; must not crash stream error path

            yield ErrorEvent(error_type=error_type, message=error_msg)
            yield StopEvent(reason=StopReason.ERROR)
            return
