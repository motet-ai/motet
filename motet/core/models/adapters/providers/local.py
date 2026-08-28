"""
Motet - Local Adapter

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Local adapter that uses LocalInferenceClient via Redis Streams.
    Implements the canonical adapter interface without provider objects.

Dependencies:
    - motet.core.models.local: LocalInferenceClient
    - motet.core.distributed.redis_manager: UnifiedRedisManager
    - motet.core.models.local.reasoning: pure parsing helpers (think split, usage /
      finish-reason mapping, tool-call parsing)
    - motet.core.types: Canonical protocol models

Usage:
adapter = LocalAdapter(provider="local", adapter_name="local")
resp = adapter.complete(LLMRequest(messages=[...]))

Notes:
    - parity: this adapter separates ``<think>`` reasoning from user-facing
      content (non-stream + a stateful stream router), maps llama.cpp usage /
      finish_reason onto canonical ``LLMUsage`` / ``StopReason``, threads sampling
      overrides (top_p/top_k/repeat_penalty/seed/stop), and emits ``UsageEvent``.
    - (Path B): native tool calling is capability-gated on CAP_TOOL_USE.
      When the model advertises it, canonical ``LLMRequest.tools`` are mapped to the
      OpenAI-style tool schema and forwarded; returned tool calls are surfaced as
      canonical ``ToolCallRequest`` output items / ``ToolCall*`` stream events. Models
      without CAP_TOOL_USE degrade to no-tools generation.
    - when LLMRequest.output_contract requests JSON with a json_schema,
      the schema is forwarded to the manager, which compiles it to a GBNF grammar
      for constrained decoding (guaranteed-parseable output). Capability-gated on
      CAP_STRUCTURED_OUTPUT; models without it degrade to unconstrained generation.
    - (tool-result fidelity): local GGUF chat templates render only
      ``role``/``content`` and ignore structured ``tool_calls``, so an assistant turn
      that only carried tool calls would render empty and orphan the following tool
      result. ``_message_to_text`` echoes such tool calls as the assistant turn's text
      (the JSON the model itself emits), restoring the ``assistant: <call>`` ->
      ``tool: <result>`` shape these models were trained on.
    - (tool-result ownership): when the conversation ends on a tool result,
      a final user turn is appended attributing the result to the model's own tool
      call. Small local models otherwise misattribute the trailing tool turn to the
      user and refuse to use it ("I can't browse the web").
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, cast

import structlog

from ....types import (
    ErrorEvent,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    OutputItem,
    StopEvent,
    StopReason,
    TextDeltaEvent,
    TextPart,
    ThinkingEvent,
    ToolCallCompleteEvent,
    ToolCallDeltaEvent,
    UsageEvent,
)
from ....observability.metrics import observe_model_latency, increment_model_errors
from ....observability.tracing import get_tracer
from ....distributed.redis_manager import UnifiedRedisManager
from ...local.reasoning import (
    ThinkStreamRouter,
    map_finish_reason,
    map_usage,
    parse_tool_calls,
)
from ..base import CapabilityDescriptor
from ...registry import get_model_spec
from ...specs import (
    CAP_STREAM,
    CAP_SYSTEM_PROMPT,
    CAP_TOOL_USE,
    CAP_JSON_MODE,
    CAP_VISION,
    CAP_REASONING,
    CAP_STRUCTURED_OUTPUT,
)

# Request controls forwarded from request.model_settings to the local manager
# (ADR-0064). ``enable_thinking`` is intentionally included here even though it
# is not a sampler: family profiles use it to apply model-specific thinking
# controls before llama.cpp renders the chat template.
_LOCAL_REQUEST_KEYS = ("top_p", "top_k", "repeat_penalty", "seed", "stop", "enable_thinking", "timeout")

# Local GGUF models are much slower than hosted APIs, so an unbounded Chat Explorer
# default can turn a missed stop sequence into a 300s model_stream timeout.
_DEFAULT_LOCAL_MAX_TOKENS = 1024

# ADR-0115 (tool-result ownership): appended as a final user turn when the
# conversation ends on a tool result. Small local models (phi-4-mini validated
# empirically) otherwise misattribute the trailing tool turn to the user and
# refuse to use it ("I can't browse the web"). Explicitly attributing the result
# to the model's own tool call flips that refusal into normal summarization.
_TOOL_RESULT_INSTRUCTION = (
    "The tool result above is the output of your own {tool} call. The data has "
    "already been retrieved for you, so never claim you cannot browse, fetch, or "
    "access it. If the result satisfies the user's request, answer now using only "
    "that result. If it reports an error or you need more data, make another tool call."
)


logger = structlog.get_logger(__name__)


def _get_client() -> Any:
    try:
        from ...local import LocalInferenceClient
    except Exception as exc:
        raise RuntimeError("LocalInferenceClient not available") from exc

    redis_manager = UnifiedRedisManager()
    redis_client = redis_manager.get_sync_client()
    return LocalInferenceClient(redis_client)


def _tool_call_name_and_args(tool_call: Any) -> Optional[Dict[str, Any]]:
    """Extract ``{"name", "arguments"}`` from a canonical-or-legacy tool-call dict.

    Tolerates the three shapes tool calls arrive in (ADR-0064 canonical
    ``tool_name``/``arguments``/``arguments_json``; ChatCompletions
    ``function.name``/``function.arguments``; Responses ``name``/``arguments``)
    so the synthesized assistant turn matches what the model emitted regardless
    of provenance.
    """
    if not isinstance(tool_call, dict):
        return None
    name = tool_call.get("tool_name") or tool_call.get("name")
    args: Any = tool_call.get("arguments")
    fn = tool_call.get("function")
    if isinstance(fn, dict):
        name = name or fn.get("name")
        if args is None:
            args = fn.get("arguments")
    if args is None:
        raw = tool_call.get("arguments_json")
        if isinstance(raw, str) and raw.strip():
            try:
                args = json.loads(raw)
            except (TypeError, ValueError):
                args = raw
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            pass
    if not name:
        return None
    return {"name": str(name), "arguments": args if args is not None else {}}


def _assistant_tool_call_text(message: Any) -> str:
    """Render an assistant turn's tool calls as the JSON the model itself emits.

    Local GGUF chat templates (phi-4, gemma, etc.) render only ``role``/``content``
    and ignore structured tool-call fields. An assistant turn that *only*
    carried tool calls therefore renders empty, leaving the following tool result
    as an orphan turn the model tends to disown ("that's not the full content...").
    Echoing the tool call as the assistant's text content restores the
    ``assistant: <call>`` -> ``tool: <result>`` shape these models were trained on
    (validated empirically against phi-4-mini's embedded template).
    """
    from motet.core.models.adapters.tool_call_codec import tool_calls_from_message

    rendered: List[Dict[str, Any]] = []
    for tc in tool_calls_from_message(message):
        args: Any = tc.arguments
        if args is None and tc.arguments_json:
            try:
                parsed = json.loads(tc.arguments_json)
                args = parsed if isinstance(parsed, dict) else parsed
            except (TypeError, ValueError, json.JSONDecodeError):
                args = tc.arguments_json
        rendered.append({"name": tc.tool_name, "arguments": args if args is not None else {}})
    if not rendered:
        return ""
    try:
        return json.dumps(rendered)
    except (TypeError, ValueError):
        return ""


def _message_to_text(message: Any) -> str:
    content = str(getattr(message, "content", "") or "")
    parts = getattr(message, "content_parts", None)
    if not parts:
        if not content and getattr(message, "role", None) == "assistant":
            return _assistant_tool_call_text(message)
        return content

    text_parts: List[str] = []
    has_media = False
    for part in parts:
        part_type = getattr(part, "type", None)
        if part_type == "text":
            text_parts.append(str(getattr(part, "text", "") or ""))
        elif part_type == "media":
            has_media = True

    if text_parts:
        return "\n".join([t for t in text_parts if t])
    if has_media:
        return "[media]"
    return content


def _message_to_content(message: Any) -> Any:
    """Build vision-capable content from a canonical message (ADR-0064 Phase 3).

    Returns a plain string when the message is text-only (the common path), or an
    OpenAI-style content-block list ``[{type: text|image_url, ...}]`` when image
    media is present. Only used when the selected model advertises CAP_VISION;
    image bytes must be inline (``url`` / ``base64_data``) since artifact-backed
    media cannot be resolved at this adapter boundary. Non-image media and
    artifact-only parts are skipped.
    """
    parts = getattr(message, "content_parts", None)
    if not parts:
        content = str(getattr(message, "content", "") or "")
        if not content and getattr(message, "role", None) == "assistant":
            return _assistant_tool_call_text(message)
        return content

    blocks: List[Dict[str, Any]] = []
    for part in parts:
        part_type = getattr(part, "type", None)
        if part_type == "text":
            text = str(getattr(part, "text", "") or "")
            if text:
                blocks.append({"type": "text", "text": text})
        elif part_type == "media":
            if getattr(part, "media_type", None) != "image":
                continue
            url = getattr(part, "url", None)
            b64 = getattr(part, "base64_data", None)
            mime = getattr(part, "mime_type", None) or "image/png"
            if url:
                image_url = url
            elif b64:
                image_url = f"data:{mime};base64,{b64}"
            else:
                continue
            blocks.append({"type": "image_url", "image_url": {"url": image_url}})

    if not blocks:
        return str(getattr(message, "content", "") or "")
    if all(b["type"] == "text" for b in blocks):
        return "\n".join(b["text"] for b in blocks)
    return blocks


def _append_tool_result_instruction(
    formatted: List[Dict[str, Any]], messages: List[Any]
) -> List[Dict[str, Any]]:
    """Append the tool-result ownership instruction when a turn ends on a tool result.

    Probe-validated against phi-4-mini's embedded template (ADR-0115): a bare
    trailing ``tool`` turn is disowned by small models, while the same result
    followed by a user turn that attributes it to the model's own call is
    summarized correctly. Only fires when the *last* message is a tool result
    (the model is being asked to respond to it now), so intermediate tool
    results in multi-step chains are left untouched. The instruction stays
    neutral about next steps so error results can still trigger a corrected
    retry call instead of a forced answer.
    """
    if not formatted or formatted[-1].get("role") != "tool":
        return formatted

    tool_names: List[str] = []
    for message in reversed(messages):
        if getattr(message, "role", None) != "tool":
            break
        name = getattr(message, "name", None)
        if name and name not in tool_names:
            tool_names.append(str(name))
    tool_label = ", ".join(reversed(tool_names)) if tool_names else "tool"

    return formatted + [
        {"role": "user", "content": _TOOL_RESULT_INSTRUCTION.format(tool=tool_label)}
    ]


@dataclass
class LocalAdapter:
    provider: str
    adapter_name: str
    credentials: Optional[Dict[str, Any]] = None

    def capabilities(self, *, model: str) -> CapabilityDescriptor:
        spec = get_model_spec("local", model)
        caps = set(spec.capabilities) if spec else set()
        return CapabilityDescriptor(
            provider=self.provider,
            model=model,
            supports_streaming=CAP_STREAM in caps,
            supports_tools=CAP_TOOL_USE in caps,
            supports_parallel_tool_calls=False,
            supports_tool_call_id=False,
            supports_vision=CAP_VISION in caps,
            supports_json_mode=CAP_JSON_MODE in caps,
            # ADR-0114: grammar-constrained (GBNF) decoding guarantees parseable output.
            supports_json_schema_strict=CAP_STRUCTURED_OUTPUT in caps,
            supports_stateful_sessions=False,
            supports_builtin_tools=False,
            supports_system_prompt=CAP_SYSTEM_PROMPT in caps,
            supports_reasoning=CAP_REASONING in caps,
            provider_metadata={"adapter": "local"},
        )

    def _structured_output_kwargs(self, request: LLMRequest, model_name: str) -> Dict[str, Any]:
        """Map a canonical OutputContract to LocalInferenceClient request kwargs (ADR-0114).

        When the request asks for JSON output and carries a JSON Schema, we
        forward it as ``json_schema`` so the manager compiles a GBNF grammar
        and constrains decoding (guaranteed-parseable output).

        Capability-gated: if the selected model does not advertise
        CAP_STRUCTURED_OUTPUT we log and degrade to unconstrained generation
        rather than risk forcing a grammar on an unsupported model.
        """
        contract = getattr(request, "output_contract", None)
        if not contract or getattr(contract, "format", "text") != "json":
            return {}
        schema = getattr(contract, "json_schema", None)
        if not schema:
            return {}
        if not self.capabilities(model=model_name).supports_json_schema_strict:
            logger.warning(
                "local_adapter_structured_output_degraded",
                model=model_name,
                note="model lacks CAP_STRUCTURED_OUTPUT; proceeding unconstrained (ADR-0114).",
            )
            return {}
        logger.info("local_adapter_structured_output_constrained", model=model_name)
        return {"json_schema": schema}

    def _local_request_kwargs(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        """Pass through local-manager controls present in ``model_settings``.

        Only forwards keys that are explicitly set so the manager keeps its
        defaults otherwise (e.g. ``top_p=0.95``). ``stop`` is merged with the
        family stop sequences manager-side; ``enable_thinking`` is applied by the
        selected local model profile before llama.cpp renders the chat template.
        """
        return {key: settings[key] for key in _LOCAL_REQUEST_KEYS if settings.get(key) is not None}

    def _tools_kwargs(self, request: LLMRequest, model_name: str) -> Dict[str, Any]:
        """Map canonical ``LLMRequest.tools`` to llama.cpp tool kwargs (ADR-0115).

        Capability-gated on CAP_TOOL_USE: models that do not advertise tool use
        degrade to no-tools generation (the schemas are dropped with a warning)
        rather than risk forcing an unsupported format on the model.
        """
        tools = getattr(request, "tools", None)
        if not tools:
            return {}
        if not self.capabilities(model=model_name).supports_tools:
            logger.warning(
                "local_adapter_tools_degraded",
                model=model_name,
                note="model lacks CAP_TOOL_USE; proceeding without tools (ADR-0115).",
            )
            return {}
        mapped = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.json_schema or {"type": "object", "properties": {}},
                },
            }
            for tool in tools
        ]
        logger.info("local_adapter_tools_forwarded", model=model_name, tool_count=len(mapped))
        return {"tools": mapped}

    def _format_messages(self, request: LLMRequest, model_name: str) -> List[Dict[str, Any]]:
        """Format canonical messages for the local inference client.

        Vision-capable models (CAP_VISION) get OpenAI-style content blocks so image
        media is preserved (ADR-0064 Phase 3); all other models flatten content to
        text, summarizing any media (the prior behavior). The CAP_VISION path is
        inert until a vision GGUF (with an ``mmproj`` projector) is added to the
        tier, but is wired and gated here so no media silently breaks a text model.
        """
        vision = self.capabilities(model=model_name).supports_vision
        has_media = any(getattr(m, "content_parts", None) for m in request.messages)
        if vision:
            formatted = [{"role": m.role, "content": _message_to_content(m)} for m in request.messages]
        else:
            formatted = [{"role": m.role, "content": _message_to_text(m)} for m in request.messages]
            if has_media:
                logger.info(
                    "local_adapter_content_parts_summarized",
                    model=model_name,
                    note="Local adapter flattens content_parts to text; media is summarized.",
                )
        reframed = _append_tool_result_instruction(formatted, list(request.messages))
        if len(reframed) != len(formatted):
            logger.info("local_adapter_tool_result_instruction_appended", model=model_name)
        return reframed

    def complete(self, request: LLMRequest) -> LLMResponse:
        settings = request.model_settings or {}
        model_name = settings.get("model_name") or settings.get("model") or ""
        if not model_name:
            raise ValueError("LLMRequest.model_settings.model_name is required for local adapter")

        temperature = settings.get("temperature", 0.7)
        max_tokens = settings.get("max_tokens", _DEFAULT_LOCAL_MAX_TOKENS)

        try:
            client = _get_client()
            formatted_messages = self._format_messages(request, model_name)
            structured_kwargs = self._structured_output_kwargs(request, model_name)
            local_request_kwargs = self._local_request_kwargs(settings)
            tools_kwargs = self._tools_kwargs(request, model_name)
            tracer = get_tracer("imf.model")
            with tracer.start_as_current_span(f"model:local:{model_name}"):
                result = client.infer_sync(
                    model_id=model_name,
                    messages=formatted_messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **structured_kwargs,
                    **local_request_kwargs,
                    **tools_kwargs,
                )

            if not result.get("success"):
                error_msg = result.get("error", "Unknown error")
                raise RuntimeError(f"Local inference failed: {error_msg}")

            # Manager exposes text/reasoning/usage/tool_calls at the top level;
            # tolerate the nested {'result': {...}} shape as well for robustness.
            nested = result.get("result") if isinstance(result.get("result"), dict) else {}

            def _pick(key: str) -> Any:
                value = result.get(key)
                if value is None and nested:
                    value = nested.get(key)
                return value

            output_text = _pick("text") or ""
            reasoning_text = _pick("reasoning") or _pick("thinking") or ""
            raw_usage = _pick("usage")
            raw_tool_calls = _pick("tool_calls")
            raw_finish = _pick("finish_reason")

            tool_calls = parse_tool_calls(raw_tool_calls)
            usage = map_usage(raw_usage)
            stop_reason = map_finish_reason(raw_finish, has_tool_calls=bool(tool_calls))

            observe_model_latency("local", model_name, float(result.get("elapsed_seconds", 0.0)))
            output_items: List[Any] = []
            if output_text:
                output_items.append(TextPart(text=output_text))
            output_items.extend(tool_calls)

            raw_metadata: Dict[str, Any] = {"raw": result}
            if reasoning_text:
                raw_metadata["thinking_text"] = reasoning_text

            return LLMResponse(
                output_text=output_text or None,
                output_items=cast(List[OutputItem], output_items),
                stop_reason=stop_reason,
                usage=usage,
                reasoning_content=reasoning_text or None,
                raw_provider_metadata=raw_metadata,
            )
        except Exception as exc:
            increment_model_errors("local", model_name, type(exc).__name__)
            logger.error("local_adapter_complete_failed", model=model_name, error=str(exc), exc_info=True)
            raise

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]:
        settings = request.model_settings or {}
        model_name = settings.get("model_name") or settings.get("model") or ""
        if not model_name:
            raise ValueError("LLMRequest.model_settings.model_name is required for local adapter")

        temperature = settings.get("temperature", 0.7)
        max_tokens = settings.get("max_tokens", _DEFAULT_LOCAL_MAX_TOKENS)

        try:
            client = _get_client()
            formatted_messages = self._format_messages(request, model_name)
            structured_kwargs = self._structured_output_kwargs(request, model_name)
            local_request_kwargs = self._local_request_kwargs(settings)
            tools_kwargs = self._tools_kwargs(request, model_name)
            logger.info("local_adapter_stream_start", model=model_name, message_count=len(formatted_messages))

            # Stateful router splits raw content tokens into thinking vs text,
            # tolerating <think>/</think> tags that span chunk boundaries.
            router = ThinkStreamRouter()
            usage = None
            finish_reason: Optional[str] = None
            had_tool_calls = False
            # Whether thinking output was emitted without a terminal
            # is_complete=True yet. UIs key "done thinking" off that flag
            # (ADR-0064), so it must be emitted when the reasoning run closes —
            # on transition to text/tool output, or at end of stream.
            thinking_open = False

            def _close_thinking() -> Iterator[LLMStreamEvent]:
                nonlocal thinking_open
                if thinking_open:
                    thinking_open = False
                    yield ThinkingEvent(text="", is_complete=True)

            def _route(text: str) -> Iterator[LLMStreamEvent]:
                nonlocal thinking_open
                for channel, chunk in router.feed(text):
                    if not chunk:
                        continue
                    if channel == "thinking":
                        thinking_open = True
                        yield ThinkingEvent(text=chunk, is_complete=False)
                    else:
                        yield from _close_thinking()
                        yield TextDeltaEvent(text=chunk)

            for event in client.infer_stream(
                model=model_name,
                messages=formatted_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **structured_kwargs,
                **local_request_kwargs,
                **tools_kwargs,
            ):
                if isinstance(event, dict):
                    event_type = event.get("type")
                    if event_type == "thinking":
                        # Pre-split thinking (manager-classified) passthrough.
                        text = event.get("text", "")
                        if text:
                            thinking_open = True
                            yield ThinkingEvent(text=str(text), is_complete=False)
                    elif event_type == "thinking_complete":
                        thinking_open = False
                        yield ThinkingEvent(text="", is_complete=True)
                    elif event_type in ("text", "token"):
                        text = event.get("text", "") or event.get("token", "")
                        if text:
                            yield from _route(str(text))
                    elif event_type == "tool_call_delta":
                        had_tool_calls = True
                        yield from _close_thinking()
                        yield ToolCallDeltaEvent(
                            call_id=str(event.get("call_id") or ""),
                            tool_name=event.get("tool_name"),
                            arguments_delta=event.get("arguments_delta"),
                        )
                    elif event_type == "tool_call_complete":
                        had_tool_calls = True
                        yield from _close_thinking()
                        yield ToolCallCompleteEvent(
                            call_id=str(event.get("call_id") or ""),
                            tool_name=str(event.get("tool_name") or ""),
                            arguments_json=str(event.get("arguments_json") or "{}"),
                        )
                    elif event_type == "final":
                        finish_reason = event.get("finish_reason")
                        usage = map_usage(event.get("usage"))
                elif event:
                    # Legacy simple string token.
                    yield from _route(str(event))

            # Drain any buffered partial-tag tail.
            for channel, chunk in router.flush():
                if not chunk:
                    continue
                if channel == "thinking":
                    thinking_open = True
                    yield ThinkingEvent(text=chunk, is_complete=False)
                else:
                    yield from _close_thinking()
                    yield TextDeltaEvent(text=chunk)

            # Close a still-open thinking run (thinking-only output, or a
            # reasoning trace truncated at end of stream).
            yield from _close_thinking()

            # ADR-0064 R9: emit usage before stop.
            if usage is not None:
                yield UsageEvent(usage=usage)
            yield StopEvent(reason=map_finish_reason(finish_reason, has_tool_calls=had_tool_calls))
        except Exception as exc:
            increment_model_errors("local", model_name, type(exc).__name__)
            yield ErrorEvent(error_type=type(exc).__name__, message=str(exc))
            yield StopEvent(reason=StopReason.ERROR)


__all__ = ["LocalAdapter"]
