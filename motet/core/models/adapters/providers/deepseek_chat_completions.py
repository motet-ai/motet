"""
Motet - DeepSeek Chat Completions Adapter

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    DeepSeek V4 provider adapter that extends OpenAI Chat Completions wire format
    with DeepSeek-specific requirements:

    - reasoning_content: Required on assistant tool-call messages when thinking is
    enabled (omit → HTTP 400). Replay full assistant message including reasoning.
    - thinking toggle: ``extra_body={"thinking": {"type": "enabled"|"disabled"}}``
    (DeepSeek defaults thinking ON; Motet must send explicit disabled when off).
    - reasoning_effort: ``high`` | ``max`` (Motet low/medium map to high).
    - max_tokens (not max_completion_tokens); sampling params ignored while thinking.
    - developer role rejected → mapped to system.

    Chat Completions at ``https://api.deepseek.com`` is the fallback V4 path.
    Builtin ``web_search`` lives on the Responses adapter. The Anthropic-
    compatible DeepSeek route is unused.

Dependencies:
    - openai: OpenAI Python SDK (sync) pointed at DeepSeek base URL
    - motet.core.models.adapters.providers.openai_chat_completions: shared helpers

Usage:
    adapter = DeepSeekChatCompletionsAdapter(
        provider="deepseek",
        adapter_name="chat_completions",
        credentials={"deepseek_api_key": "...", "base_url": "https://api.deepseek.com"},
    )
    resp = adapter.complete(LLMRequest(messages=[...], tools=[...]))

Notes:
    - Env override: DEEPSEEK_API_BASE (wired in model.py); config key deepseek_api_key /
      MOTET_DEEPSEEK_API_KEY / DEEPSEEK_API_KEY; vault key "deepseek".
    - Prefer ModelSpec.base_url when set via _normalize_adapter_credentials.
    - Pattern mirrors MoonshotChatCompletionsAdapter (reasoning_content replay).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

import structlog

from ....types import (
    ErrorEvent,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    LLMUsage,
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
from ..base import CapabilityDescriptor
from ...registry import get_model_spec
from ...specs import (
    CAP_JSON_MODE,
    CAP_STREAM,
    CAP_SYSTEM_PROMPT,
    CAP_TOOL_USE,
    CAP_VISION,
)
from ....workers.concurrency_primitives import worker_sleep
from .message_history_sanitizer import sanitize_orphan_tool_call_messages
from .openai_chat_completions import (
    _build_response_format,
    _canonical_tools_to_openai,
    _finish_reason_to_stop_reason,
    _format_messages_for_openai,
)
from .chat_completions_deltas import ChatCompletionsToolCallAssembler

logger = structlog.get_logger(__name__)

DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# DeepSeek only documents high/max; low/medium are mapped to high for compatibility.
_VALID_DEEPSEEK_EFFORTS = frozenset({"high", "max"})


def _normalize_deepseek_credentials(credentials: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Ensure the OpenAI client builder sees api_key + base_url for DeepSeek."""
    creds = dict(credentials or {})
    api_key = creds.get("api_key") or creds.get("deepseek_api_key")
    if api_key:
        creds["api_key"] = api_key
        creds.setdefault("deepseek_api_key", api_key)
    if not creds.get("base_url"):
        creds["base_url"] = DEFAULT_DEEPSEEK_BASE_URL
    return creds


def _build_deepseek_client(credentials: Optional[Dict[str, Any]]) -> Any:
    """Build OpenAI client configured for DeepSeek Chat Completions."""
    try:
        from openai import OpenAI
    except Exception as exc:
        raise RuntimeError("openai package not available") from exc

    creds = _normalize_deepseek_credentials(credentials)
    return OpenAI(api_key=creds.get("api_key"), base_url=creds["base_url"])


def _get_reasoning_content(m: Any) -> Optional[str]:
    """Extract reasoning_content from a message (Message or dict)."""
    rc = getattr(m, "reasoning_content", None)
    if rc is None and isinstance(m, dict):
        rc = m.get("reasoning_content")
    return rc if (rc is not None and isinstance(rc, str)) else None


def _resolve_reasoning_effort(settings: Dict[str, Any]) -> str:
    """
    Map Motet reasoning_effort onto DeepSeek's high|max vocabulary.

    Deliberately not the shared ``normalize_reasoning_effort`` clamp, which would round
    ``xhigh`` down to ``high``. With only two rungs, "above high" is better served by
    ``max``: DeepSeek's ``high`` already sits at the top of most callers' intent, so a
    request for more should get more.
    """
    raw = settings.get("reasoning_effort")
    if isinstance(raw, str):
        effort = raw.strip().lower()
        if effort in _VALID_DEEPSEEK_EFFORTS:
            return effort
        if effort in {"low", "medium"}:
            return "high"
        if effort in {"xhigh"}:
            return "max"
    return "high"


def _apply_deepseek_generation_params(
    params: Dict[str, Any],
    *,
    settings: Dict[str, Any],
    openai_tools: Optional[List[Dict[str, Any]]],
) -> bool:
    """
    Attach tools and DeepSeek thinking / token settings.

    Returns:
        True when ThinkingEvent / reasoning_content should be surfaced to callers.
    """
    enable_thinking = bool(settings.get("enable_thinking", False))

    if openai_tools:
        params["tools"] = openai_tools

    from ...output_limits import resolve_max_output_tokens

    max_tokens = resolve_max_output_tokens(
        settings,
        provider="deepseek",
        model_name=params.get("model") or settings.get("model_name"),
        fallback=None,
    )
    if max_tokens is not None:
        params["max_tokens"] = max_tokens

    # DeepSeek defaults thinking ON — always send an explicit toggle from Motet.
    params["extra_body"] = {
        "thinking": {"type": "enabled" if enable_thinking else "disabled"},
    }

    if enable_thinking:
        params["reasoning_effort"] = _resolve_reasoning_effort(settings)
        # Sampling params are no-ops in thinking mode; omit to avoid confusion.
        params.pop("temperature", None)
        params.pop("top_p", None)
        params.pop("presence_penalty", None)
        params.pop("frequency_penalty", None)
    else:
        temp = settings.get("temperature")
        if temp is not None:
            params["temperature"] = temp

    return enable_thinking


def _format_messages_for_deepseek(
    messages: List[Any],
    *,
    model_name: str,
    request_context: Any,
) -> List[Dict[str, Any]]:
    """
    Format canonical messages for DeepSeek Chat Completions.

    Starts from the OpenAI formatter, then:
    - maps ``developer`` → ``system`` (DeepSeek rejects developer)
    - injects ``reasoning_content`` on assistant turns (required with tool_calls)
    - ensures assistant ``content`` is a string when tool_calls are present
    """
    # Preserve original messages for reasoning_content lookup by index.
    originals = list(messages)
    formatted = _format_messages_for_openai(
        messages,
        model_name=model_name,
        request_context=request_context,
        provider="deepseek",
    )

    for idx, msg in enumerate(formatted):
        if msg.get("role") == "developer":
            msg["role"] = "system"

        if msg.get("role") != "assistant":
            continue

        original = originals[idx] if idx < len(originals) else None
        rc = _get_reasoning_content(original) if original is not None else None
        if rc is None:
            rc = msg.get("reasoning_content") if isinstance(msg.get("reasoning_content"), str) else None
        # Always attach for assistant messages so tool-loop replay cannot omit the field.
        msg["reasoning_content"] = rc if isinstance(rc, str) else ""

        if msg.get("tool_calls") and msg.get("content") is None:
            msg["content"] = ""

    return formatted


def _ensure_reasoning_content(messages: List[Dict[str, Any]]) -> None:
    """Final guard: reasoning_content must exist on assistant+tool_calls messages."""
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            rc = msg.get("reasoning_content")
            if not isinstance(rc, str):
                msg["reasoning_content"] = ""
            if msg.get("content") is None:
                msg["content"] = ""


@dataclass
class DeepSeekChatCompletionsAdapter:
    """
    DeepSeek V4 adapter for Chat Completions API.

    Handles DeepSeek-specific thinking toggle, reasoning_effort, and
    reasoning_content replay for agentic tool loops.
    """

    provider: str
    adapter_name: str
    credentials: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        self.credentials = _normalize_deepseek_credentials(self.credentials)

    def capabilities(self, *, model: str) -> CapabilityDescriptor:
        """Return capability descriptor for DeepSeek models."""
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
            supports_json_schema_strict=CAP_JSON_MODE in caps,
            supports_stateful_sessions=False,
            supports_builtin_tools=bool(getattr(spec, "supported_builtin_tools", None)),
            supports_system_prompt=CAP_SYSTEM_PROMPT in caps if spec else True,
            supports_reasoning=("reasoning" in caps),
            provider_metadata={"adapter": "deepseek_chat_completions"},
        )

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Execute non-streaming completion with DeepSeek."""
        settings = request.model_settings or {}
        model_name = settings.get("model_name") or settings.get("model") or ""
        if not model_name:
            raise ValueError(
                "LLMRequest.model_settings.model_name is required for DeepSeek adapter"
            )

        client = _build_deepseek_client(self.credentials)
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
        formatted = _format_messages_for_deepseek(
            messages,
            model_name=model_name,
            request_context=request.request_context,
        )
        _ensure_reasoning_content(formatted)

        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                start = time.perf_counter()
                tracer = get_tracer("imf.model")
                with tracer.start_as_current_span(f"model:deepseek:{model_name}"):
                    cfg = Config()
                    br = get_breaker_configured(
                        f"model:deepseek:{model_name}",
                        default_failure_threshold=int(
                            getattr(cfg, "breaker_model_failure_threshold", 5) or 5
                        ),
                        default_reset_timeout_seconds=float(
                            getattr(cfg, "breaker_model_reset_timeout_seconds", 60.0) or 60.0
                        ),
                    )

                    def _call() -> Any:
                        params: Dict[str, Any] = {
                            "model": model_name,
                            "messages": formatted,
                        }
                        response_format = _build_response_format(request)
                        if response_format is not None:
                            params["response_format"] = response_format
                            logger.info(
                                "deepseek_structured_output_constrained",
                                model=model_name,
                                mode=response_format.get("type"),
                            )
                        _apply_deepseek_generation_params(
                            params,
                            settings=settings,
                            openai_tools=openai_tools,
                        )
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
                stop_reason = _finish_reason_to_stop_reason(
                    str(finish_reason or "stop"), has_tool_calls=has_tool_calls
                )

                usage = None
                if hasattr(result, "usage") and result.usage:
                    u = result.usage
                    prompt_details = getattr(u, "prompt_tokens_details", None)
                    cached_tokens = (
                        getattr(prompt_details, "cached_tokens", None) if prompt_details else None
                    )
                    completion_details = getattr(u, "completion_tokens_details", None)
                    reasoning_tokens = (
                        getattr(completion_details, "reasoning_tokens", None)
                        if completion_details
                        else None
                    )
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

                reasoning_content: Optional[str] = None
                if hasattr(result, "choices") and result.choices:
                    msg = result.choices[0].message
                    reasoning_content = getattr(msg, "reasoning_content", None)
                    if reasoning_content is not None and not isinstance(reasoning_content, str):
                        reasoning_content = None

                observe_model_latency("deepseek", model_name, time.perf_counter() - start)

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
                    logger.warning(
                        "deepseek_complete_error",
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
                        logger.error(
                            "deepseek_complete_failed_after_retries",
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
        """Execute streaming completion with DeepSeek."""
        settings = request.model_settings or {}
        model_name = settings.get("model_name") or settings.get("model") or ""
        if not model_name:
            raise ValueError(
                "LLMRequest.model_settings.model_name is required for DeepSeek adapter"
            )

        client = _build_deepseek_client(self.credentials)
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
        formatted = _format_messages_for_deepseek(
            messages,
            model_name=model_name,
            request_context=request.request_context,
        )
        _ensure_reasoning_content(formatted)

        assembler = ChatCompletionsToolCallAssembler()
        finish_reason = "stop"
        try:
            tracer = get_tracer("imf.model")
            with tracer.start_as_current_span(f"model_stream:deepseek:{model_name}"):
                stream_params: Dict[str, Any] = {
                    "model": model_name,
                    "messages": formatted,
                    "stream": True,
                }
                response_format = _build_response_format(request)
                if response_format is not None:
                    stream_params["response_format"] = response_format
                    logger.info(
                        "deepseek_structured_output_constrained",
                        model=model_name,
                        mode=response_format.get("type"),
                    )
                emit_thinking = _apply_deepseek_generation_params(
                    stream_params,
                    settings=settings,
                    openai_tools=openai_tools,
                )
                stream_params["stream_options"] = {"include_usage": True}

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

                    if hasattr(event, "usage") and event.usage is not None:
                        usage_obj = event.usage
                        prompt_details = getattr(usage_obj, "prompt_tokens_details", None)
                        cached_tokens = (
                            getattr(prompt_details, "cached_tokens", None)
                            if prompt_details
                            else None
                        )
                        completion_details = getattr(usage_obj, "completion_tokens_details", None)
                        reasoning_tokens = (
                            getattr(completion_details, "reasoning_tokens", None)
                            if completion_details
                            else None
                        )
                        final_usage = LLMUsage(
                            prompt_tokens=getattr(usage_obj, "prompt_tokens", None),
                            output_tokens=getattr(usage_obj, "completion_tokens", None),
                            total_tokens=getattr(usage_obj, "total_tokens", None),
                            cache_read_tokens=cached_tokens,
                            reasoning_tokens=reasoning_tokens,
                        )

            tool_calls_list = list(assembler.complete())
            for complete_ev in tool_calls_list:
                yield complete_ev

            if emit_thinking and reasoning_chunks:
                yield ThinkingEvent(text="", is_complete=True)

            if final_usage is not None:
                yield UsageEvent(usage=final_usage)

            yield StopEvent(
                reason=_finish_reason_to_stop_reason(
                    finish_reason, has_tool_calls=bool(tool_calls_list)
                )
            )
            return
        except Exception as exc:
            error_type = type(exc).__name__
            error_msg = str(exc)
            try:
                logger.warning(
                    "deepseek_stream_error",
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
