"""
Motet - OpenAI Responses Adapter

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    OpenAI provider adapter that targets the OpenAI **Responses API** (`/v1/responses`) and
    translates between OpenAI wire formats and Motet canonical protocol types.

    This adapter is translation-only:
    - It renders canonical `Message` + multimodal `content_parts` into OpenAI-compatible input payloads.
    - It parses OpenAI Responses outputs/events into canonical `LLMResponse` / `LLMStreamEvent`.
    - It does NOT implement orchestration policy (tool discovery, retries, schema repair).
    - when ``enable_prompt_caching`` is set and the model has
    CAP_PROMPT_CACHING, sets ``prompt_cache_key`` from ``conversation_id``.

Dependencies:
    - openai: OpenAI Python SDK (sync)
    - motet.core.types: canonical protocol models (LLMRequest/LLMResponse, parts, tool calls, usage, stop reasons)
    - motet.core.models.rendering: OpenAI multimodal renderer
    - motet.core.models.specs: model capability constants + registry lookup

Usage:
    from motet.core.models.adapters.providers.openai_responses import OpenAIResponsesAdapter
    from motet.core.types import LLMRequest, Message, RequestContext

    adapter = OpenAIResponsesAdapter(
        provider="openai",
        adapter_name="responses",
        credentials={"openai_api_key": "..."},
    )
    resp = adapter.complete(LLMRequest(messages=[Message(role="user", content="Hello")]))

Notes:
    - System/developer messages are extracted into `instructions` to avoid double-injection.
    - Multimodal rendering is fail-closed and requires RequestContext isolation.
    - Streaming support is best-effort because SDK event objects can vary by version; we parse by dict keys.
    - LLMRequest.output_contract maps to the Responses `text.format`
      param, whose shape is *flattened* (`{type, name, strict, schema}`; `name`
      is required by the API) — not Chat Completions' nested `response_format`.
    - Tool-call round-trip: prefer verbatim ``arguments_json`` (never ``str(dict)``) and
      unmodified function_call items on follow-up turns. Names on replayed calls are
      already wire format; this adapter does not remap ``mcp.`` ↔ ``mcp__``.
    - Tool-call streaming: ``response.function_call_arguments.*`` events identify their
      call only by ``item_id`` (no ``call_id``, ``name`` is null), so the ``output_item``
      events are tracked to recover both. Without that map every argument fragment is
      unattributable and the completed call surfaces only at ``response.completed``.
    - Native web search is sent as ``{"type": "web_search"}``. Canonical names
      are ``web_search`` and ``{provider}.web_search`` (OpenAI-compatible
      subclasses inherit this; they only override Responses params).
    - Stateless reasoning replay: every call sends ``store=false`` (no
      server-side retention, ZDR-compatible); when reasoning is enabled we request
      ``include=["reasoning.encrypted_content"]``, capture the turn's output items
      verbatim into ``reasoning_blocks``, and replay them on subsequent iterations so
      chain-of-thought carries across tool calls. xAI overrides
      ``_finalize_responses_params`` and does not inherit this behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple, cast

import json
import time
import structlog

from ....types import (
    CanonicalToolSchema,
    Citation,
    CitationSpan,
    CitationsEvent,
    ErrorEvent,
    GeneratedImage,
    ImageGenerationRequest,
    ImageGenerationResponse,
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
    ToolUseEvent,
    UsageEvent,
    normalize_reasoning_effort,
)
from ....observability.metrics import observe_model_latency, increment_model_errors
from ....observability.tracing import get_tracer
from ....config import Config
from ....resilience import get_breaker_configured
from ....workers.concurrency_primitives import worker_sleep
from ..base import CapabilityDescriptor
from ..prompt_caching import apply_prompt_cache_key
from ..tool_call_codec import (
    inbound_tool_call_request,
    tool_call_requests_from_unknown,
    tool_call_requests_to_responses_items,
    tool_calls_from_message,
)
from ...rendering import get_renderer
from ...rendering.base import RenderingContext
from ...specs import (
    CAP_IMAGE_GENERATION,
    CAP_JSON_MODE,
    CAP_STREAM,
    CAP_SYSTEM_PROMPT,
    CAP_TOOL_USE,
    CAP_VISION,
)
from ...registry import get_model_spec
from .message_history_sanitizer import sanitize_orphan_tool_call_messages

logger = structlog.get_logger(__name__)


def _tool_calls_to_function_call_items(tool_calls: List[Any]) -> List[Dict[str, Any]]:
    """Convert mixed assistant tool-call payloads into Responses function_call items (ADR-0137).

    Names are passed through; ``model.py`` already applied wire format.
    """
    return tool_call_requests_to_responses_items(tool_call_requests_from_unknown(tool_calls))


def _extract_instructions_and_messages(messages: List[Message]) -> Tuple[Optional[str], List[Message]]:
    """
    Extract system/developer guidance into `instructions` and return remaining messages.

    NOTE: `Message.role` is a free string in our codebase; treat "developer" as system-equivalent.
    """

    sys_chunks: List[str] = []
    remaining: List[Message] = []
    for m in messages:
        if m.role in {"system", "developer"}:
            if m.content:
                sys_chunks.append(m.content)
            # If content_parts exists, include text parts only; other parts are ignored for instructions.
            parts = getattr(m, "content_parts", None) or []
            for part in parts:
                if isinstance(part, TextPart):
                    sys_chunks.append(part.text)
            continue
        remaining.append(m)

    instructions = "\n\n".join([c for c in sys_chunks if c.strip()]) if sys_chunks else None
    return instructions, remaining


def _format_messages_for_openai(
    *,
    messages: List[Message],
    model_name: str,
    request_context: Optional[RequestContext],
) -> List[Dict[str, Any]]:
    """
    Render canonical messages into OpenAI Responses API-compatible `input` items.

    IMPORTANT:
        The Responses API `input` is an *item list*. It can contain normal message objects
        (e.g. {"role": "user", "content": "..."}) *and* non-message items such as tool outputs
        (e.g. {"type": "function_call_output", ...}).

        Our in-memory transcript currently uses legacy ChatCompletions-style fields on `Message`:
        - assistant messages may include `tool_calls` (list of dicts)
        - tool messages include `tool_call_id`

        The Responses API does NOT accept `tool_calls` as a field on an input message object,
        so we translate these into Responses-native input items:
        - assistant.tool_calls -> {"type": "function_call", "call_id": ..., "name": ..., "arguments": ...}
        - tool(role="tool")    -> {"type": "function_call_output", "call_id": ..., "output": ...}

    Notes:
        This function intentionally does NOT emit ChatCompletions-only keys like `tool_calls`
        or `tool_call_id` on message objects.
    """

    def _flatten_text_parts(m: Message) -> str:
        parts = getattr(m, "content_parts", None) or []
        if not parts:
            return m.content
        text_chunks: List[str] = []
        for part in parts:
            if isinstance(part, TextPart):
                text_chunks.append(part.text)
            elif isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                text_chunks.append(part["text"])
        return "\n\n".join([c for c in text_chunks if c]) if text_chunks else m.content

    enable_multimodal = bool(getattr(request_context, "enable_multimodal", False))
    has_parts = any(bool(getattr(m, "content_parts", None)) for m in messages)

    # Detect image-ish parts anywhere (used to decide whether to invoke the renderer).
    has_image_parts = False
    if has_parts:
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

    out_items: List[Dict[str, Any]] = []

    # Canonical multimodal rendering: materialize MediaPart(image) to base64_data once for the full message list.
    renderer = None
    ctx = None
    rendered_messages: Optional[List[Message]] = None
    if has_image_parts and enable_multimodal:
        if not request_context or not request_context.tenant_id or not request_context.principal_id:
            raise ValueError("Multimodal rendering requires RequestContext with tenant_id and principal_id.")
        from ....artifacts import get_artifact_store

        renderer = get_renderer("canonical")
        ctx = RenderingContext(
            provider="openai",
            model_name=model_name,
            tenant_id=str(request_context.tenant_id),
            principal_id=str(request_context.principal_id),
            motet_id=str(request_context.motet_id) if request_context.motet_id is not None else None,
            artifact_store=get_artifact_store(),
            max_images=int(getattr(request_context, "max_images", 8)),
            max_image_bytes=int(getattr(request_context, "max_image_bytes", 20 * 1024 * 1024)),
        )
        rendered_messages = renderer.render(messages, context=ctx)

    iter_messages = rendered_messages if rendered_messages is not None else messages
    for m in iter_messages:
        # Tool output messages: translate to Responses-native item (NOT a role="tool" message).
        if m.role == "tool":
            call_id = str(getattr(m, "tool_call_id", None) or "")
            if not call_id:
                # Best-effort: if we somehow lack a call_id, skip (fail-soft rather than emitting invalid input).
                continue
            out_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _flatten_text_parts(m),
                }
            )
            continue

        # Normal message object (user/assistant/other).
        # ADR-0064 R10: For assistant messages, prefer verbatim replay of the turn's
        # captured output items (reasoning items with encrypted_content + function_call
        # + message, ids intact). This preserves chain-of-thought across tool-call
        # iterations with store=false. Fall back to a summary-derived reasoning item.
        if m.role == "assistant":
            replay_items = _valid_replay_items_for_message(m)
            if replay_items is not None:
                # The verbatim items already cover the message text and function calls;
                # skip the reconstruction paths below for this message.
                out_items.extend(replay_items)
                continue
            rc = getattr(m, "reasoning_content", None)
            if rc is not None and isinstance(rc, str) and rc.strip():
                # OpenAI Responses API expects "reasoning" items (not "thinking").
                out_items.append({"type": "reasoning", "summary": [{"type": "summary_text", "text": rc}]})

        parts = getattr(m, "content_parts", None) or []
        if parts and enable_multimodal:
            content_blocks: List[Dict[str, Any]] = []
            for part in parts:
                if isinstance(part, TextPart):
                    if part.text:
                        content_blocks.append({"type": "input_text", "text": part.text})
                    continue
                if isinstance(part, MediaPart) and part.media_type == "image":
                    if not isinstance(part.mime_type, str) or not part.mime_type.startswith("image/"):
                        raise ValueError(f"Invalid image mime_type for MediaPart: {part.mime_type!r}")
                    if not isinstance(part.base64_data, str) or not part.base64_data:
                        raise ValueError("Expected MediaPart.base64_data after canonical rendering")
                    data_url = f"data:{part.mime_type};base64,{part.base64_data}"
                    block: Dict[str, Any] = {"type": "input_image", "image_url": data_url}
                    if (part.detail or "") in {"low", "high", "auto"}:
                        block["detail"] = part.detail
                    content_blocks.append(block)
                    continue
                raise ValueError(f"Unsupported content part for OpenAI Responses multimodal: {type(part).__name__}")

            msg_dict: Dict[str, Any] = {"role": m.role, "content": content_blocks}
            if getattr(m, "name", None):
                msg_dict["name"] = m.name
            out_items.append(msg_dict)
        else:
            msg_dict = {"role": m.role, "content": _flatten_text_parts(m)}
            if getattr(m, "name", None):
                msg_dict["name"] = m.name
            out_items.append(msg_dict)

        # If this message contained legacy tool_calls, translate into Responses-native function_call items.
        calls = tool_calls_from_message(m)
        if calls:
            out_items.extend(tool_call_requests_to_responses_items(calls))

    return out_items


def _build_text_format(request: LLMRequest) -> Optional[Dict[str, Any]]:
    """Map a canonical OutputContract to the Responses API ``text`` param (ADR-0114).

    The Responses API moved structured output from Chat Completions'
    ``response_format`` to ``text.format``, with a *flattened* shape:
    ``{"type": "json_schema", "name": ..., "strict": ..., "schema": ...}``.
    ``name`` is required — the API rejects the request without it. With a
    schema we request strict ``json_schema`` enforcement; with bare
    ``format="json"`` we fall back to ``json_object`` (JSON mode: valid JSON,
    no schema adherence). Validation/fallback policy lives outside the adapter
    (ADR-0064 R2); the adapter only attaches the provider config.
    """
    contract: Optional[OutputContract] = request.output_contract
    if not contract or contract.format != "json":
        return None
    if contract.json_schema:
        return {
            "format": {
                "type": "json_schema",
                "name": "structured_output",
                "strict": bool(contract.strict),
                "schema": contract.json_schema,
            }
        }
    return {"format": {"type": "json_object"}}


_DEFAULT_WEB_SEARCH_NAMES = frozenset({"web_search", "openai.web_search"})


def _canonical_tools_to_openai(
    tools: Optional[List[CanonicalToolSchema]],
    *,
    web_search_names: Optional[Set[str]] = None,
    web_search_wire_type: str = "web_search",
) -> Optional[List[Dict[str, Any]]]:
    if not tools:
        return None
    names = web_search_names if web_search_names is not None else _DEFAULT_WEB_SEARCH_NAMES
    out: List[Dict[str, Any]] = []
    for t in tools:
        # Provider-native built-ins: unified "web_search" or a namespaced provider name.
        # model.py replaces provider-specific names with "web_search" before the adapter.
        # These are NOT registry tools; the host executes them on the Responses request.
        if t.name in names:
            out.append({"type": web_search_wire_type})
            continue

        # OpenAI Responses expects function tool fields at the top-level (name/description/parameters),
        # unlike Chat Completions which nests these under {"function": {...}}.
        out.append(
            {
                "type": "function",
                "name": t.name,
                "description": t.description or "",
                "parameters": t.json_schema,
            }
        )
    return out


def _parse_usage(raw: Dict[str, Any]) -> Optional[LLMUsage]:
    usage = raw.get("usage") or raw.get("usage_metadata") or None
    if not isinstance(usage, dict):
        return None
    
    # ADR-0064 R9: Extract cache tokens
    # Responses API uses input_tokens_details, Chat Completions uses prompt_tokens_details
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached_tokens = input_details.get("cached_tokens") if isinstance(input_details, dict) else None
    
    # Extract reasoning tokens from output_tokens_details / completion_tokens_details (OpenAI o1/o3 format)
    output_details = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
    reasoning_tokens = (
        output_details.get("reasoning_tokens") 
        if isinstance(output_details, dict) 
        else usage.get("reasoning_tokens")
    )
    
    # Best-effort normalization across SDK variants
    return LLMUsage(
        prompt_tokens=usage.get("input_tokens") or usage.get("prompt_tokens"),
        output_tokens=usage.get("output_tokens") or usage.get("completion_tokens"),
        total_tokens=usage.get("total_tokens"),
        cache_read_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
        provider_metadata=usage,
    )


def _parse_response_to_canonical(
    raw: Dict[str, Any],
) -> Tuple[str, List[ToolCallRequest], List[Citation], StopReason, Optional[LLMUsage], Optional[str]]:
    """
    Parse a Responses API response dict into canonical primitives.
    Returns (output_text, tool_calls, citations, stop_reason, usage, reasoning_content).
    """

    # Some SDK versions include a top-level `output_text`; others only include text inside
    # `output[*].content[*]` blocks (typically type="output_text" with a `text` field).
    output_text = raw.get("output_text") or ""
    tool_calls: List[ToolCallRequest] = []
    citations: List[Citation] = []
    reasoning_chunks: List[str] = []

    # Responses output is item-based; extract function/tool call items if present.
    output_items = raw.get("output") or raw.get("outputs") or []
    if isinstance(output_items, list):
        text_chunks: List[str] = []
        for item in output_items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            # ADR-0064 R10: Capture reasoning from output items.
            # - "thinking" items: o1/o3-style raw reasoning text
            # - "reasoning" items: gpt-5/o-series reasoning with optional summaries
            if item_type == "thinking":
                t = item.get("thinking") or item.get("text")
                if isinstance(t, str) and t.strip():
                    reasoning_chunks.append(t)
            if item_type == "reasoning":
                # gpt-5/o-series return reasoning items with summaries (not raw reasoning text).
                summaries = item.get("summary")
                if isinstance(summaries, list):
                    for s in summaries:
                        if isinstance(s, dict):
                            st = s.get("text")
                            if isinstance(st, str) and st.strip():
                                reasoning_chunks.append(st)
            if (
                isinstance(item_type, str)
                and "web_search" in item_type
                and item_type not in {"function_call", "tool_call"}
            ):
                for url in _urls_from_web_search_item(item):
                    citations.append(
                        Citation(
                            source_type="web",
                            url=url,
                            title=None,
                            metadata={"source": "web_search_call", "item_id": item.get("id")},
                        )
                    )
            if item_type in {"function_call", "tool_call"}:
                call_id = str(item.get("call_id") or item.get("id") or "")
                name = str(item.get("name") or item.get("tool_name") or "")
                args = item.get("arguments")
                # arguments may be JSON string or object depending on SDK/version; normalize to string
                if isinstance(args, (dict, list)):
                    args_json = json.dumps(args)
                else:
                    args_json = str(args or "")
                if call_id and name:
                    tool_calls.append(
                        inbound_tool_call_request(
                            call_id=call_id,
                            tool_name=name,
                            arguments_json=args_json,
                        )
                    )
            # Text output: OpenAI Responses typically returns assistant messages as:
            # {"type":"message","role":"assistant","content":[{"type":"output_text","text":"..."}]}
            # Newer/other variants may also use type="output_text" blocks.
            content_blocks = item.get("content")
            if isinstance(content_blocks, list):
                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype in {"output_text", "text"}:
                        t = block.get("text")
                        if isinstance(t, str) and t:
                            text_chunks.append(t)
            # Best-effort: capture citations/annotations from any output message content blocks
            # (OpenAI Responses may attach URL/file citations as annotations on output text blocks).
            if isinstance(content_blocks, list):
                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue
                    ann = block.get("annotations")
                    if not isinstance(ann, list):
                        continue
                    for a in ann:
                        if not isinstance(a, dict):
                            continue
                        a_type = str(a.get("type") or "")
                        url = a.get("url") if isinstance(a.get("url"), str) else None
                        title = a.get("title") if isinstance(a.get("title"), str) else None
                        source_id = None
                        source_type: str = "other"
                        if "url_citation" in a_type or url:
                            source_type = "web"
                        if "file_citation" in a_type:
                            source_type = "document"
                            source_id = (
                                a.get("file_id")
                                if isinstance(a.get("file_id"), str)
                                else (a.get("document_id") if isinstance(a.get("document_id"), str) else None)
                            )
                        start = a.get("start_index") if isinstance(a.get("start_index"), int) else None
                        end = a.get("end_index") if isinstance(a.get("end_index"), int) else None
                        spans = [CitationSpan(start=start, end=end)] if (start is not None and end is not None) else None
                        citations.append(
                            Citation(
                                source_type=source_type,  # type: ignore[arg-type]
                                title=title,
                                url=url,
                                source_id=source_id,
                                snippets=None,
                                spans=spans,
                                metadata=a,
                            )
                        )
        if not output_text and text_chunks:
            output_text = "\n".join(text_chunks)

    _merge_top_level_url_citations(raw, citations)

    reasoning_content = ("\n".join(reasoning_chunks).strip() or None) if reasoning_chunks else None

    # Stop reason: best-effort
    stop = StopReason.NATURAL_STOP
    # Prefer explicit stop fields if present
    incomplete_details = raw.get("incomplete_details") or {}
    if not isinstance(incomplete_details, dict):
        incomplete_details = {}
    stop_raw = raw.get("stop_reason") or raw.get("finish_reason") or incomplete_details.get("reason")
    if isinstance(stop_raw, str):
        mapped = {
            "stop": StopReason.NATURAL_STOP,
            "length": StopReason.LENGTH_LIMIT,
            "max_output_tokens": StopReason.LENGTH_LIMIT,
            "tool_calls": StopReason.TOOL_CALLS,
            "function_call": StopReason.TOOL_CALLS,
            "content_filter": StopReason.SAFETY_FILTER,
        }.get(stop_raw)
        if mapped is not None:
            stop = mapped

    if tool_calls:
        stop = StopReason.TOOL_CALLS

    usage = _parse_usage(raw)
    return output_text, tool_calls, citations, stop, usage, reasoning_content


def _strip_none_values(obj: Any) -> Any:
    """Recursively drop None values from dicts (SDK model_dump includes nulls the API rejects on input)."""
    if isinstance(obj, dict):
        return {k: _strip_none_values(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_none_values(v) for v in obj]
    return obj


# Output item types that can be replayed verbatim as Responses input items.
# Provider-executed items (e.g. web_search_call) are excluded: they are not
# valid input items and their work is already reflected in the message text.
_REPLAYABLE_OUTPUT_ITEM_TYPES = frozenset({"reasoning", "message", "function_call"})


def _extract_reasoning_replay_items(raw: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """
    Capture verbatim Responses output items for stateless reasoning replay (ADR-0064 R10).

    With ``store=false`` + ``include=["reasoning.encrypted_content"]``, reasoning models
    return reasoning items carrying ``encrypted_content``. Replaying the turn's output
    items verbatim (reasoning + function_call + message, ids intact) on the next
    iteration preserves chain-of-thought across tool calls without server-side
    storage (ZDR-compatible).

    Returns the sanitized item list when at least one reasoning item has
    encrypted_content; otherwise None (summary-based replay is used instead).
    """
    output_items = raw.get("output") or raw.get("outputs") or []
    if not isinstance(output_items, list):
        return None
    has_encrypted = any(
        isinstance(it, dict) and it.get("type") == "reasoning" and it.get("encrypted_content")
        for it in output_items
    )
    if not has_encrypted:
        return None
    replay: List[Dict[str, Any]] = []
    for it in output_items:
        if isinstance(it, dict) and it.get("type") in _REPLAYABLE_OUTPUT_ITEM_TYPES:
            replay.append(_strip_none_values(it))
    return replay or None


def _valid_replay_items_for_message(m: Message) -> Optional[List[Dict[str, Any]]]:
    """
    Validate ``Message.reasoning_blocks`` as verbatim OpenAI Responses output items.

    ``reasoning_blocks`` is provider-opaque and conversations can switch providers
    mid-stream, so only replay blocks that look like Responses items (replayable
    types with an encrypted reasoning item) and that are consistent with the
    canonical message: function_call call_ids must match ``Message.tool_calls_canonical``
    and non-empty content requires a message item. On any mismatch return None
    and let the summary-based fallback handle the turn.
    """
    blocks = getattr(m, "reasoning_blocks", None)
    if not isinstance(blocks, list) or not blocks:
        return None
    items: List[Dict[str, Any]] = []
    for it in blocks:
        if not isinstance(it, dict) or it.get("type") not in _REPLAYABLE_OUTPUT_ITEM_TYPES:
            return None
        items.append(it)
    if not any(it.get("type") == "reasoning" and it.get("encrypted_content") for it in items):
        return None

    block_call_ids = {
        str(it.get("call_id") or "") for it in items if it.get("type") == "function_call"
    }
    message_call_ids: Set[str] = set()
    for tc in tool_calls_from_message(m):
        if tc.kind == "provider":
            continue
        if tc.call_id:
            message_call_ids.add(str(tc.call_id))
    if block_call_ids != message_call_ids:
        return None

    content_text = (getattr(m, "content", None) or "").strip()
    if content_text and not any(it.get("type") == "message" for it in items):
        return None
    return items


def _supports_temperature(model_name: str) -> bool:
    """Return False for OpenAI models that reject temperature."""
    model = (model_name or "").lower()
    return not (model.startswith("o1") or model.startswith("o3") or model.startswith("gpt-5"))


# OpenAI Responses ``reasoning.effort`` vocabulary. ``none``/``minimal`` are unused by Motet.
# Most models top out at ``xhigh``; gpt-5.6 (Responses only) also accepts ``max``.
_VALID_OPENAI_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
_VALID_OPENAI_REASONING_EFFORTS_WITH_MAX = frozenset({"low", "medium", "high", "xhigh", "max"})


def _openai_model_supports_max_reasoning_effort(model_name: str) -> bool:
    """
    True when OpenAI Responses accepts ``reasoning.effort=max`` for this model.

    Verified against OpenAI/Azure docs (2026-07): ``max`` is gpt-5.6-only on the
    Responses API (Chat Completions rejects it). Model ids may be aliases
    (``gpt-5.6``) or tiered (``gpt-5.6-sol`` / ``terra`` / ``luna``).
    """
    model = (model_name or "").lower().strip()
    return model == "gpt-5.6" or model.startswith("gpt-5.6-")


def _resolve_openai_reasoning_effort(settings: Dict[str, Any]) -> str:
    """
    Map canonical reasoning effort onto OpenAI's ``reasoning.effort``.

    Supported rungs are model-dependent. gpt-5.6 Responses accepts ``max``;
    earlier models (and Chat Completions) 400 on it, so Motet ``max`` clamps
    to ``xhigh`` there. Values still reach us from unvalidated overrides, so
    normalize rather than pass through raw.
    """
    model_name = str(settings.get("model_name") or settings.get("model") or "")
    supported = (
        _VALID_OPENAI_REASONING_EFFORTS_WITH_MAX
        if _openai_model_supports_max_reasoning_effort(model_name)
        else _VALID_OPENAI_REASONING_EFFORTS
    )
    return normalize_reasoning_effort(
        settings.get("reasoning_effort"),
        default="medium",
        supported=supported,
    )


def _clean_web_search_url(url: str) -> str:
    """Drop DeepSeek ``#ws_call_id=`` tracking fragments from open_page URLs."""
    cleaned = url.strip()
    marker = "#ws_call_id="
    if marker in cleaned:
        cleaned = cleaned.split(marker, 1)[0]
    return cleaned


def _urls_from_web_search_item(item: Dict[str, Any]) -> List[str]:
    """Collect URLs from a Responses ``web_search_call`` item (DeepSeek open_page)."""
    action = item.get("action")
    if not isinstance(action, dict):
        return []
    urls: List[str] = []
    raw_url = action.get("url")
    if isinstance(raw_url, str) and raw_url.startswith("http"):
        urls.append(_clean_web_search_url(raw_url))
    sources = action.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, str) and source.startswith("http"):
                urls.append(_clean_web_search_url(source))
            elif isinstance(source, dict):
                source_url = source.get("url")
                if isinstance(source_url, str) and source_url.startswith("http"):
                    urls.append(_clean_web_search_url(source_url))
    # Dedup while preserving order.
    seen: Set[str] = set()
    out: List[str] = []
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _merge_top_level_url_citations(raw: Dict[str, Any], citations: List[Citation]) -> None:
    """Add URL-only citations from a top-level ``citations`` list (xAI All Citations)."""
    extra = raw.get("citations")
    if not isinstance(extra, list):
        return
    seen = {c.url for c in citations if c.url}
    for item in extra:
        url: Optional[str] = None
        if isinstance(item, str):
            url = item.strip() or None
        elif isinstance(item, dict):
            raw_url = item.get("url")
            url = raw_url.strip() if isinstance(raw_url, str) else None
        if not url or url in seen:
            continue
        citations.append(
            Citation(
                source_type="web",
                url=url,
                title=None,
                metadata={"source": "top_level_citations"},
            )
        )
        seen.add(url)


def _extract_provider_tool_use_events(
    raw: Dict[str, Any],
    *,
    tool_name: str = "openai.web_search",
) -> List[ToolUseEvent]:
    output_items = raw.get("output") or raw.get("outputs") or []
    if not isinstance(output_items, list):
        return []

    events: List[ToolUseEvent] = []
    for item in output_items:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if not item_type:
            continue
        # Responses built-in tools (e.g., web_search_call) are emitted as output items.
        if "web_search" in item_type and item_type not in {"function_call", "tool_call"}:
            status = item.get("status") if isinstance(item.get("status"), str) else None
            events.append(
                ToolUseEvent(
                    kind="provider",
                    tool_name=tool_name,
                    status=status,
                    metadata=item,
                )
            )
    return events


def _event_to_dict(ev: Any) -> Dict[str, Any]:
    """
    Best-effort conversion of OpenAI SDK streaming event objects into a dict.

    The OpenAI Python SDK has changed event object types across versions. We support:
    - pydantic-like objects with `.model_dump()`
    - dict-like events
    - objects with attribute access (fall back to vars()).
    """

    if isinstance(ev, dict):
        return ev
    if hasattr(ev, "model_dump"):
        try:
            d = ev.model_dump()
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    if hasattr(ev, "__dict__"):
        try:
            d = vars(ev)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {}


def _get_event_type(ev: Any, ev_raw: Dict[str, Any]) -> Optional[str]:
    t = getattr(ev, "type", None)
    if isinstance(t, str):
        return t
    t2 = ev_raw.get("type")
    return t2 if isinstance(t2, str) else None


def _resolve_tool_call_identity(
    ev: Any,
    ev_raw: Dict[str, Any],
    call_by_item: Dict[str, Tuple[str, str]],
) -> Tuple[str, str]:
    """Recover (call_id, tool_name) for a function-call argument event.

    Argument events carry only `item_id` and the argument text: `call_id` is
    absent and `name` is explicitly null. Both live on the `output_item` events
    that bracket them, so *call_by_item* supplies what the event omits. The
    fields are still read off the event first, since SDK versions have differed
    on which of them are populated.
    """
    item_id = str(getattr(ev, "item_id", None) or ev_raw.get("item_id") or "")
    known_call, known_name = call_by_item.get(item_id, ("", ""))
    call_id = str(
        getattr(ev, "call_id", None) or ev_raw.get("call_id") or known_call or item_id
    )
    name = getattr(ev, "name", None) or ev_raw.get("name") or known_name
    return call_id, str(name or "")


@dataclass
class OpenAIResponsesAdapter:
    provider: str
    adapter_name: str
    credentials: Optional[Dict[str, Any]] = None

    def _client(self):
        try:
            from openai import OpenAI
        except Exception as exc:
            raise RuntimeError("openai package not available") from exc

        creds = self.credentials or {}
        api_key = creds.get("openai_api_key") or creds.get("api_key")
        base_url = creds.get("base_url")
        if base_url:
            return OpenAI(api_key=api_key, base_url=base_url)
        return OpenAI(api_key=api_key)

    def _web_search_canonical_names(self) -> Set[str]:
        """Unified ``web_search`` plus ``{provider}.web_search`` for this host."""
        names = {"web_search"}
        provider = (self.provider or "").strip().lower()
        if provider:
            names.add(f"{provider}.web_search")
        return names

    def _web_search_wire_type(self) -> str:
        return "web_search"

    def _web_search_tool_use_name(self) -> str:
        provider = (self.provider or "").strip().lower() or "openai"
        return f"{provider}.web_search"

    def _responses_tools(
        self, tools: Optional[List[CanonicalToolSchema]]
    ) -> Optional[List[Dict[str, Any]]]:
        return _canonical_tools_to_openai(
            tools,
            web_search_names=self._web_search_canonical_names(),
            web_search_wire_type=self._web_search_wire_type(),
        )

    def _finalize_responses_params(
        self,
        params: Dict[str, Any],
        request: LLMRequest,
    ) -> Dict[str, Any]:
        """Hook for provider subclasses to adjust Responses API params before the call.

        OpenAI policy (ADR-0064 R10): disable server-side retention (store=false; no
        30-day org retention, ZDR-compatible) and, when reasoning is enabled, request
        encrypted reasoning items so multi-turn tool loops can replay chain-of-thought
        statelessly. OpenAI-compatible hosts (e.g. xAI) override this method entirely
        to apply their own policy (always-on reasoning, cache keys) and do NOT inherit
        the store/include behavior.

        ADR-0124: when prompt caching is enabled for a capable model, set
        ``prompt_cache_key`` from ``request_context.conversation_id``.
        """
        params["store"] = False
        if params.get("reasoning"):
            params["include"] = ["reasoning.encrypted_content"]
        apply_prompt_cache_key(params, request, provider=self.provider)
        return params

    def capabilities(self, *, model: str):
        # Best-effort: consult our model spec registry if available.
        # Use self.provider so OpenAI-compatible subclasses (xAI) resolve correctly.
        spec = get_model_spec(self.provider, model)
        caps = set(spec.capabilities) if spec else set()
        return CapabilityDescriptor(
            provider=self.provider,
            model=model,
            supports_streaming=CAP_STREAM in caps,
            supports_tools=CAP_TOOL_USE in caps,
            supports_parallel_tool_calls=CAP_TOOL_USE in caps,
            supports_tool_call_id=True,  # Responses provides call_id
            supports_vision=CAP_VISION in caps,
            supports_audio=False,
            supports_video=False,
            supports_image_generation=CAP_IMAGE_GENERATION in caps,  # ADR-0113
            supports_json_mode=CAP_JSON_MODE in caps,
            # text.format is adapter-level; only advertise when the model has JSON mode.
            supports_json_schema_strict=CAP_JSON_MODE in caps,
            supports_stateful_sessions=True,  # previous_response_id optional
            supports_builtin_tools=bool(getattr(spec, "supported_builtin_tools", None)),
            supports_system_prompt=CAP_SYSTEM_PROMPT in caps if spec else True,
            supports_reasoning=("reasoning" in caps),
            provider_metadata={"adapter": "openai_responses"},
        )

    def generate_images(self, request: ImageGenerationRequest) -> ImageGenerationResponse:
        """
        ADR-0113: generate images via the OpenAI Images API (`client.images.generate`).

        GPT Image models (gpt-image-1/1.5/2) return base64 by default; DALL·E supports
        response_format. We pass through size/quality/n/background when set and normalize
        the result into canonical GeneratedImage items.
        """
        client = self._client()
        settings = request.model_settings or {}
        model_name = settings.get("model_name") or settings.get("model") or "gpt-image-1"

        kwargs: Dict[str, Any] = {
            "model": model_name,
            "prompt": request.prompt,
            "n": int(request.n or 1),
        }
        if request.size:
            kwargs["size"] = request.size
        if request.quality:
            kwargs["quality"] = request.quality
        if request.background:
            kwargs["background"] = request.background
        # Only pass response_format for models that accept it (DALL·E); GPT Image rejects it.
        response_format = settings.get("response_format")
        if response_format and str(model_name).startswith("dall-e"):
            kwargs["response_format"] = response_format

        try:
            result = client.images.generate(**kwargs)
        except Exception as exc:
            increment_model_errors(provider=self.provider, model=str(model_name))
            logger.error(
                "openai_image_generation_failed",
                provider=self.provider,
                model=str(model_name),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

        data = getattr(result, "data", None) or []
        images: List[GeneratedImage] = []
        for item in data:
            b64 = getattr(item, "b64_json", None)
            url = getattr(item, "url", None)
            revised = getattr(item, "revised_prompt", None)
            if not b64 and not url:
                continue
            images.append(
                GeneratedImage(
                    mime_type="image/png",
                    base64_data=b64,
                    url=url,
                    revised_prompt=revised,
                )
            )

        usage_obj = getattr(result, "usage", None)
        usage = None
        if usage_obj is not None:
            usage = LLMUsage(
                prompt_tokens=getattr(usage_obj, "input_tokens", None),
                output_tokens=getattr(usage_obj, "output_tokens", None),
                total_tokens=getattr(usage_obj, "total_tokens", None),
            )

        return ImageGenerationResponse(
            images=images,
            model=str(model_name),
            usage=usage,
            raw_provider_metadata={"adapter": "openai_responses"},
        )

    def complete(self, request: LLMRequest) -> LLMResponse:
        client = self._client()

        # Provider-boundary guard against orphan assistant tool-call history blocks.
        safe_messages, sanitize_stats = sanitize_orphan_tool_call_messages(request.messages)
        if sanitize_stats["removed_assistant_calls"] > 0 or sanitize_stats["removed_tool_messages"] > 0:
            logger.warning(
                "provider_boundary_orphan_tool_calls_pruned",
                provider=self.provider,
                model=(request.model_settings or {}).get("model_name") or (request.model_settings or {}).get("model") or "",
                removed_assistant_calls=sanitize_stats["removed_assistant_calls"],
                removed_tool_messages=sanitize_stats["removed_tool_messages"],
            )

        # Extract instructions and ensure we do not double-inject system guidance.
        instructions, remaining = _extract_instructions_and_messages(safe_messages)
        formatted = _format_messages_for_openai(
            messages=remaining,
            model_name=(request.model_settings or {}).get("model_name") or (request.model_settings or {}).get("model") or "",
            request_context=request.request_context,
        )

        settings = request.model_settings or {}
        model_name = settings.get("model_name") or settings.get("model") or ""
        temperature = settings.get("temperature")
        from ...output_limits import resolve_max_output_tokens

        # Request / ModelSpec when known; omit wire field (provider default) when neither.
        max_output_tokens = resolve_max_output_tokens(
            settings,
            provider=self.provider,
            model_name=model_name,
            fallback=None,
        )

        params: Dict[str, Any] = {
            "model": model_name,
            "input": formatted,
            # Note: usage is included by default in Responses API responses
        }
        if instructions:
            params["instructions"] = instructions
        if temperature is not None and _supports_temperature(model_name):
            params["temperature"] = temperature
        if max_output_tokens is not None:
            params["max_output_tokens"] = int(max_output_tokens)

        # ADR-0064: Enable reasoning with summaries for gpt-5/o-series models when enable_thinking is set.
        # This activates the reasoning engine and requests human-readable reasoning summaries
        # so ThinkingEvent can surface them to the UI.
        if settings.get("enable_thinking"):
            params["reasoning"] = {"effort": _resolve_openai_reasoning_effort(settings), "summary": "auto"}

        openai_tools = self._responses_tools(request.tools)
        if openai_tools:
            params["tools"] = openai_tools
            params["parallel_tool_calls"] = True

        # ADR-0114: structured output via Responses text.format when requested.
        text_format = _build_text_format(request)
        if text_format is not None:
            params["text"] = text_format
            logger.info(
                "openai_responses_structured_output_constrained",
                model=model_name,
                mode=text_format["format"]["type"],
            )

        params = self._finalize_responses_params(params, request)

        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                logger.info(
                    "openai_responses_complete_call",
                    model=model_name,
                    has_tools=bool(openai_tools),
                    has_instructions=bool(instructions),
                    attempt=attempt + 1,
                    max_attempts=3,
                )
                start = time.perf_counter()
                tracer = get_tracer("imf.model")
                with tracer.start_as_current_span(f"model:openai:{model_name}"):
                    cfg = Config()
                    br = get_breaker_configured(
                        f"model:openai:{model_name}",
                        default_failure_threshold=int(getattr(cfg, "breaker_model_failure_threshold", 5) or 5),
                        default_reset_timeout_seconds=float(getattr(cfg, "breaker_model_reset_timeout_seconds", 60.0) or 60.0),
                    )

                    def _call() -> Any:
                        return cast(Any, client).responses.create(**params)

                    resp = br.call(_call)

                raw = resp.model_dump() if hasattr(resp, "model_dump") else (resp if isinstance(resp, dict) else {})

                output_text, tool_calls, citations, stop_reason, usage, reasoning_content = _parse_response_to_canonical(
                    raw if isinstance(raw, dict) else {}
                )

                output_items_out: List[Any] = []
                if output_text:
                    output_items_out.append(TextPart(text=output_text))
                output_items_out.extend(tool_calls)

                observe_model_latency("openai", model_name, time.perf_counter() - start)

                return LLMResponse(
                    output_text=output_text or None,
                    output_items=output_items_out,
                    citations=(citations or None),
                    stop_reason=stop_reason,
                    usage=usage,
                    raw_provider_metadata={"raw": raw},
                    reasoning_content=reasoning_content,
                    # ADR-0064 R10: verbatim output items (with encrypted reasoning) for stateless
                    # replay. Only captured when thinking was requested: with store=false the API
                    # returns encrypted reasoning even for default (unrequested) reasoning, and
                    # surfacing it would violate the thinking-disabled contract downstream.
                    reasoning_blocks=(
                        _extract_reasoning_replay_items(raw if isinstance(raw, dict) else {})
                        if settings.get("enable_thinking")
                        else None
                    ),
                )
            except Exception as exc:
                last_exc = exc
                error_type = type(exc).__name__
                error_msg = str(exc)
                try:
                    structlog.get_logger().warning(
                        "openai_responses_complete_error",
                        model=model_name,
                        error_type=error_type,
                        error_message=error_msg[:200],
                        attempt=attempt + 1,
                        max_attempts=3,
                    )
                    increment_model_errors("openai", model_name, error_type)
                except Exception:
                    pass  # metrics best-effort; must not break error handling

                if attempt == 2:
                    try:
                        structlog.get_logger().error(
                            "openai_responses_complete_failed_after_retries",
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
        client = self._client()

        safe_messages, sanitize_stats = sanitize_orphan_tool_call_messages(request.messages)
        if sanitize_stats["removed_assistant_calls"] > 0 or sanitize_stats["removed_tool_messages"] > 0:
            logger.warning(
                "provider_boundary_orphan_tool_calls_pruned",
                provider=self.provider,
                model=(request.model_settings or {}).get("model_name") or (request.model_settings or {}).get("model") or "",
                removed_assistant_calls=sanitize_stats["removed_assistant_calls"],
                removed_tool_messages=sanitize_stats["removed_tool_messages"],
            )
        instructions, remaining = _extract_instructions_and_messages(safe_messages)
        formatted = _format_messages_for_openai(
            messages=remaining,
            model_name=(request.model_settings or {}).get("model_name") or (request.model_settings or {}).get("model") or "",
            request_context=request.request_context,
        )

        settings = request.model_settings or {}
        model_name = settings.get("model_name") or settings.get("model") or ""
        temperature = settings.get("temperature")
        from ...output_limits import resolve_max_output_tokens

        max_output_tokens = resolve_max_output_tokens(
            settings,
            provider=self.provider,
            model_name=model_name,
            fallback=None,
        )

        params: Dict[str, Any] = {
            "model": model_name,
            "input": formatted,
            "stream": True,
            # Note: usage is included by default in Responses API streaming responses
        }
        if instructions:
            params["instructions"] = instructions
        if temperature is not None and _supports_temperature(model_name):
            params["temperature"] = temperature
        if max_output_tokens is not None:
            params["max_output_tokens"] = int(max_output_tokens)

        # ADR-0064: Enable reasoning with summaries for gpt-5/o-series models when enable_thinking is set.
        if settings.get("enable_thinking"):
            params["reasoning"] = {"effort": _resolve_openai_reasoning_effort(settings), "summary": "auto"}

        openai_tools = self._responses_tools(request.tools)
        if openai_tools:
            params["tools"] = openai_tools
            # Request parallel tool calls when supported (API may still return one per turn in practice)
            params["parallel_tool_calls"] = True

        # ADR-0114: structured output via Responses text.format when requested.
        text_format = _build_text_format(request)
        if text_format is not None:
            params["text"] = text_format
            logger.info(
                "openai_responses_structured_output_constrained",
                model=model_name,
                mode=text_format["format"]["type"],
            )

        params = self._finalize_responses_params(params, request)

        emitted_text = False
        emitted_citations = False
        streamed_reasoning_summary = False
        completed_reasoning_summary = False

        # Buffers for tool arguments by call_id.
        tool_arg_buffers: Dict[str, str] = {}
        tool_name_by_call: Dict[str, str] = {}
        # Argument events identify their call only by the *item* id they belong to
        # (`fc_...`); the call id and tool name arrive once, on the output item.
        # Without this map neither can be recovered, and the fragments are unusable.
        call_by_item: Dict[str, Tuple[str, str]] = {}
        # Track call_ids we already emitted via function_call_arguments.done to avoid duplicates at response.completed
        emitted_tool_call_ids: Set[str] = set()
        start = time.perf_counter()
        latency_recorded = False
        try:
            logger.info(
                "openai_responses_stream_call",
                model=model_name,
                has_tools=bool(openai_tools),
                has_instructions=bool(instructions),
                attempt=1,
                max_attempts=1,
            )
            tracer = get_tracer("imf.model")
            with tracer.start_as_current_span(f"model_stream:openai:{model_name}"):
                cfg = Config()
                br = get_breaker_configured(
                    f"model:openai:{model_name}",
                    default_failure_threshold=int(getattr(cfg, "breaker_model_failure_threshold", 5) or 5),
                    default_reset_timeout_seconds=float(getattr(cfg, "breaker_model_reset_timeout_seconds", 60.0) or 60.0),
                )

                def _call() -> Any:
                    return cast(Any, client).responses.create(**params)

                stream = br.call(_call)

            for ev in stream:
                ev_raw = _event_to_dict(ev)
                t = _get_event_type(ev, ev_raw)
                if not t:
                    # Unknown event object shape; ignore.
                    continue

                if t == "response.output_text.delta":
                    delta = getattr(ev, "delta", None)
                    if delta is None:
                        delta = ev_raw.get("delta")
                    delta = delta or ""
                    if delta:
                        yield TextDeltaEvent(text=str(delta))
                        emitted_text = True
                    continue

                if t.startswith("response.output_text.") and t != "response.output_text.delta":
                    text = getattr(ev, "text", None)
                    if text is None:
                        text = ev_raw.get("text") or ev_raw.get("output_text")
                    text = text or ""
                    if text and not emitted_text:
                        yield TextDeltaEvent(text=str(text))
                        emitted_text = True
                    continue

                if t == "response.reasoning_summary_text.delta":
                    delta = getattr(ev, "delta", None)
                    if delta is None:
                        delta = ev_raw.get("delta")
                    delta = delta or ""
                    if delta:
                        streamed_reasoning_summary = True
                        yield ThinkingEvent(text=str(delta), is_complete=False)
                    continue

                if t in {
                    "response.reasoning_summary_text.done",
                    "response.reasoning_summary_part.done",
                    "response.reasoning_summary.done",
                }:
                    if streamed_reasoning_summary:
                        completed_reasoning_summary = True
                        yield ThinkingEvent(text="", is_complete=True)
                    continue

                if t in {"response.output_item.added", "response.output_item.done"}:
                    item = ev_raw.get("item")
                    if isinstance(item, dict) and item.get("type") == "function_call":
                        item_id = str(item.get("id") or "")
                        if item_id:
                            resolved_call = str(item.get("call_id") or item_id)
                            call_by_item[item_id] = (
                                resolved_call,
                                str(item.get("name") or ""),
                            )
                            if item.get("name"):
                                tool_name_by_call[resolved_call] = str(item["name"])
                    continue

                if t == "response.function_call_arguments.delta":
                    call_id, name = _resolve_tool_call_identity(ev, ev_raw, call_by_item)
                    delta = getattr(ev, "delta", None)
                    if delta is None:
                        delta = ev_raw.get("delta")
                    delta = delta or ""
                    if call_id:
                        if isinstance(name, str) and name:
                            tool_name_by_call[call_id] = name
                        tool_arg_buffers[call_id] = tool_arg_buffers.get(call_id, "") + str(delta)
                        raw_name = name or tool_name_by_call.get(call_id) or ""
                        canonical_name = (
                            inbound_tool_call_request(
                                call_id=call_id,
                                tool_name=raw_name,
                                arguments_json="{}",
                            ).tool_name
                            if raw_name
                            else None
                        )
                        yield ToolCallDeltaEvent(
                            call_id=call_id,
                            tool_name=canonical_name,
                            arguments_delta=str(delta) if delta else None,
                        )
                    continue

                if t == "response.function_call_arguments.done":
                    call_id, name = _resolve_tool_call_identity(ev, ev_raw, call_by_item)
                    name = name or tool_name_by_call.get(call_id) or ""
                    if call_id and name:
                        emitted_tool_call_ids.add(call_id)
                        args_json = getattr(ev, "arguments", None) or ev_raw.get("arguments") or tool_arg_buffers.get(call_id, "")
                        req = inbound_tool_call_request(
                            call_id=call_id,
                            tool_name=str(name),
                            arguments_json=str(args_json or ""),
                        )
                        yield ToolCallCompleteEvent(
                            call_id=req.call_id,
                            tool_name=req.tool_name,
                            arguments_json=req.arguments_json,
                        )
                    continue

                if t == "response.error":
                    err = ev_raw.get("error") or {}
                    message = ""
                    error_type = "openai_error"
                    if isinstance(err, dict):
                        message = str(err.get("message") or "")
                        error_type = str(err.get("type") or error_type)
                    try:
                        increment_model_errors("openai", model_name, error_type)
                    except Exception:
                        pass  # metrics best-effort; must not break stream
                    yield ErrorEvent(error_type=error_type, message=message)
                    continue

                if t in {"response.usage", "response.usage.done"}:
                    usage = _parse_usage(ev_raw if isinstance(ev_raw, dict) else {})
                    if usage is None:
                        usage_obj = getattr(ev, "usage", None)
                        if usage_obj is not None:
                            usage = _parse_usage(_event_to_dict(usage_obj))
                    if usage:
                        yield UsageEvent(usage=usage)
                    continue

                if t == "response.completed":
                    # Some SDK versions only provide the final response object here; parse it fully to
                    # ensure we emit tool call completes and/or final text when deltas were not streamed.
                    response_obj = getattr(ev, "response", None)
                    if response_obj is None:
                        response_obj = ev_raw.get("response")

                    response_dict = _event_to_dict(response_obj)
                    # Fallback: some event types put fields at top-level
                    if not response_dict and isinstance(ev_raw, dict):
                        response_dict = ev_raw
                    # Usage is sometimes attached to the event wrapper, not the response payload.
                    if isinstance(ev_raw, dict) and "usage" in ev_raw and "usage" not in response_dict:
                        response_dict["usage"] = ev_raw.get("usage")
                    if isinstance(ev_raw, dict) and "usage_metadata" in ev_raw and "usage_metadata" not in response_dict:
                        response_dict["usage_metadata"] = ev_raw.get("usage_metadata")

                    output_text, tool_calls, citations, stop_reason, usage, reasoning_content = _parse_response_to_canonical(response_dict)
                    if usage is None and isinstance(ev_raw, dict):
                        usage = _parse_usage(ev_raw)
                    if usage is None:
                        usage_obj = getattr(ev, "usage", None)
                        if usage_obj is not None:
                            usage = _parse_usage(_event_to_dict(usage_obj))
                    provider_tool_events = _extract_provider_tool_use_events(
                        response_dict,
                        tool_name=self._web_search_tool_use_name(),
                    )

                    # If we never saw text deltas, emit final text as a single delta for compatibility.
                    if output_text and not emitted_text:
                        yield TextDeltaEvent(text=output_text)
                        emitted_text = True

                    # Emit tool calls not already emitted via function_call_arguments.done (avoid duplicates)
                    for tc in tool_calls:
                        if tc.call_id not in emitted_tool_call_ids:
                            emitted_tool_call_ids.add(tc.call_id)
                            yield ToolCallCompleteEvent(call_id=tc.call_id, tool_name=tc.tool_name, arguments_json=tc.arguments_json)

                    for event in provider_tool_events:
                        yield event

                    if citations and not emitted_citations:
                        yield CitationsEvent(citations=citations)
                        emitted_citations = True

                    # ADR-0064 R10: Emit reasoning so model_stream can accumulate into reasoning_content.
                    # The final event also carries the verbatim encrypted reasoning items (blocks)
                    # so orchestration can persist them for stateless multi-turn replay. Only when
                    # thinking was requested: with store=false the API returns encrypted reasoning
                    # even for default (unrequested) reasoning, and a ThinkingEvent on a
                    # thinking-disabled call would violate the canonical stream contract.
                    reasoning_blocks = (
                        _extract_reasoning_replay_items(response_dict)
                        if settings.get("enable_thinking")
                        else None
                    )
                    if (
                        reasoning_content
                        and isinstance(reasoning_content, str)
                        and reasoning_content.strip()
                        and not streamed_reasoning_summary
                    ):
                        yield ThinkingEvent(text=reasoning_content, is_complete=True, blocks=reasoning_blocks)
                    elif streamed_reasoning_summary and not completed_reasoning_summary:
                        completed_reasoning_summary = True
                        yield ThinkingEvent(text="", is_complete=True, blocks=reasoning_blocks)
                    elif reasoning_blocks:
                        # Summary already completed earlier in the stream; still deliver the
                        # encrypted reasoning items for replay persistence.
                        yield ThinkingEvent(text="", is_complete=True, blocks=reasoning_blocks)

                    if usage:
                        yield UsageEvent(usage=usage)

                    # If tool calls occurred, stop_reason must be tool_calls
                    if tool_calls:
                        stop_reason = StopReason.TOOL_CALLS

                    if not latency_recorded:
                        observe_model_latency("openai", model_name, time.perf_counter() - start)
                        latency_recorded = True

                    yield StopEvent(reason=stop_reason)
                    return

                # Ignore other event types; they may be present depending on SDK version.
                continue
        except Exception as exc:
            error_type = type(exc).__name__
            error_msg = str(exc)
            try:
                structlog.get_logger().warning(
                    "openai_responses_stream_error",
                    model=model_name,
                    error_type=error_type,
                    error_message=error_msg[:200],
                    attempt=1,
                    max_attempts=1,
                )
                increment_model_errors("openai", model_name, error_type)
            except Exception:
                pass  # logging best-effort; must not crash error propagation
            yield ErrorEvent(error_type=error_type, message=error_msg)
            yield StopEvent(reason=StopReason.ERROR)
            return

