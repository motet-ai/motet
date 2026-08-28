"""
Motet - Anthropic Messages Adapter

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Anthropic provider adapter that targets the Anthropic **Messages API** and translates
    between Anthropic wire formats and Motet canonical protocol types.
    Handles Claude Opus 5+ thinking-on-by-default by sending explicit ``thinking.type=disabled``
    when Motet ``enable_thinking`` is false.

    ``enable_prompt_caching`` is capability-gated (CAP_PROMPT_CACHING). When enabled,
    marks the stable system prompt and the last tool schema with ``cache_control`` breakpoints.
    Volatile per-turn system injections (pending action, memory recall, hook output — flagged
    ``cache_volatile`` in message metadata) are emitted as separate uncached blocks after the
    stable block so they cannot invalidate the cached prefix between turns.

    This adapter is translation-only:
    - Renders canonical `Message` history (including tool transcript messages) into Anthropic Messages input.
    - Converts Anthropic output blocks (`text`, `tool_use`, `server_tool_use`,
      `web_search_tool_result`) into canonical `LLMResponse` output items and
      `Citation` URLs for native web search.
    - Emits best-effort canonical streaming events via the Anthropic SDK stream helper.
    - Supports Anthropic's native web search via `server_tool_use` blocks.

Dependencies:
    - anthropic: Anthropic Python SDK (sync)
    - motet.core.types: canonical protocol models (LLMRequest/LLMResponse, tool calls, usage, stop reasons)
    - motet.core.models.specs: model capability constants + registry lookup

Usage:
    from motet.core.models.adapters.providers.anthropic_messages import AnthropicMessagesAdapter
    from motet.core.types import LLMRequest, Message

    adapter = AnthropicMessagesAdapter(provider="anthropic", adapter_name="messages", credentials={"anthropic_api_key": "..."})
    resp = adapter.complete(LLMRequest(messages=[Message(role="user", content="hi")], model_settings={"model_name": "claude-3-5-sonnet-latest"}))

Notes:
    - Canonical `role="tool"` messages are translated to Anthropic `tool_result` content blocks inside `role="user"`.
    - Assistant `Message.tool_calls` may be OpenAI-shaped or standardized (tool_name/parameters); we translate both into `tool_use` blocks.
    - Multimodal/image parts are rendered when RequestContext.enable_multimodal is true.
    - Web search: Maps `web_search` tool to Anthropic's `web_search_20250305` server tool.
      `server_tool_use` blocks are marked with `kind="provider"`. URLs come from
      text-block ``web_search_result_location`` citations and
      ``web_search_tool_result`` items so ``core.web_search`` can keep path=llm.
    - Thinking replay: thinking text is surfaced as `reasoning_content` and
      verbatim signed `thinking`/`redacted_thinking` blocks are captured into `reasoning_blocks`
      (complete + stream). When thinking is enabled on a request, persisted blocks are replayed
      ahead of the assistant turn's text/tool_use blocks for chain-of-thought continuity.
    - Opus/Sonnet 5+: thinking is on by default at the API, so when Motet `enable_thinking`
      is false the adapter sends `thinking.type=disabled`. Opus 5+ additionally rejects
      disabled thinking above `high` effort, so effort is clamped for that family only
      (Sonnet 5 accepts disabled at `max`). Fable/Mythos reject `disabled` entirely.
    - Adaptive-thinking Claude families default to `high` effort when the caller does not
      specify one, matching Anthropic's own default; other models keep `medium`.
    - Anthropic has no response_format/json_schema mode, so LLMRequest.output_contract
      (format="json" + json_schema) is emulated by forcing a single tool whose input_schema is the
      requested schema (tool_choice). The tool input is unwrapped back into plain JSON text. This is
      gated: it is skipped (degraded to unconstrained) when caller tools or extended thinking are set.
    - Trailing assistant turns are rejected before the API call via the shared
      `assert_trailing_user_turn` (message_history_sanitizer). Anthropic reads them as an
      assistant prefill, which models from Opus 4.5 onward refuse; the canonical protocol has
      no prefill concept, so this always means the turn was assembled without user input.
      Failing locally names that cause instead of an opaque provider 400.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple

import structlog

from ....types import (
    CanonicalToolSchema,
    Citation,
    CitationsEvent,
    ErrorEvent,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    LLMUsage,
    MediaPart,
    Message,
    OutputContract,
    ReasoningEffort,
    ThinkingEvent,
    ToolUseEvent,
    StopEvent,
    StopReason,
    TextDeltaEvent,
    TextPart,
    ToolCallCompleteEvent,
    ToolCallRequest,
    UsageEvent,
    normalize_reasoning_effort,
)
from ..base import CapabilityDescriptor
from ..prompt_caching import prompt_caching_enabled
from ...registry import get_model_spec
from ...specs import (
    CAP_JSON_MODE,
    CAP_STREAM,
    CAP_SYSTEM_PROMPT,
    CAP_TOOL_USE,
    CAP_VISION,
)
from .message_history_sanitizer import (
    assert_trailing_user_turn,
    sanitize_orphan_tool_call_messages,
)
from ..tool_call_codec import (
    inbound_tool_call_request,
    tool_call_requests_from_unknown,
    tool_call_requests_to_anthropic_blocks,
    tool_calls_from_message,
)

logger = structlog.get_logger(__name__)


def _as_dict(obj: Any) -> Dict[str, Any]:
    """Best-effort conversion of SDK objects to dict for parsing."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    try:
        md = getattr(obj, "model_dump", None)
        if callable(md):
            out = md()
            return out if isinstance(out, dict) else {}
    except Exception:
        pass  # model_dump fallback; continue to vars() below
    try:
        return vars(obj) if hasattr(obj, "__dict__") else {}
    except Exception:
        return {}


def _flatten_text_parts(m: Message) -> str:
    """Flatten text from content_parts when present; ignore non-text parts for now."""
    parts = getattr(m, "content_parts", None) or []
    if not parts:
        return m.content
    chunks: List[str] = []
    for part in parts:
        # Pydantic model path
        p_type = getattr(part, "type", None) if not isinstance(part, dict) else part.get("type")
        if p_type != "text":
            continue
        p_text = getattr(part, "text", None) if not isinstance(part, dict) else part.get("text")
        if isinstance(p_text, str) and p_text:
            chunks.append(p_text)
    return "\n\n".join(chunks) if chunks else m.content


def _image_parts_to_blocks(*, m: Message, request_context: Any) -> List[Dict[str, Any]]:
    """
    Render MediaPart(image) into Anthropic Messages image blocks.

    Prefers base64_data when present (ADR-0064); otherwise resolves artifact_id
    via artifact store (requires request_context with tenant/principal isolation, ADR-0062).
    """
    enable = bool(getattr(request_context, "enable_multimodal", False))
    if not enable:
        return []

    tenant_id = getattr(request_context, "tenant_id", None)
    principal_id = getattr(request_context, "principal_id", None)
    motet_id = getattr(request_context, "motet_id", None)
    if not tenant_id or not principal_id:
        return []

    import base64

    parts = getattr(m, "content_parts", None) or []
    out: List[Dict[str, Any]] = []
    for part in parts:
        p_type = getattr(part, "type", None) if not isinstance(part, dict) else part.get("type")
        if p_type != "media":
            continue
        media_type = getattr(part, "media_type", None) if not isinstance(part, dict) else part.get("media_type")
        if media_type != "image":
            continue
        # MediaPart field name is `mime_type`; accept dict variants too.
        content_type = getattr(part, "mime_type", None) if not isinstance(part, dict) else (part.get("mime_type") or part.get("content_type"))
        if not isinstance(content_type, str) or not content_type.startswith("image/"):
            continue

        # ADR-0064: prefer base64_data when present (e.g. after canonical renderer)
        b64_data = getattr(part, "base64_data", None) if not isinstance(part, dict) else part.get("base64_data")
        if isinstance(b64_data, str) and b64_data:
            out.append({"type": "image", "source": {"type": "base64", "media_type": content_type, "data": b64_data}})
            continue

        # Resolve artifact_id via store (tenant/principal scoped)
        artifact_id = getattr(part, "artifact_id", None) if not isinstance(part, dict) else part.get("artifact_id")
        if not artifact_id:
            continue
        from ....artifacts import get_artifact_store

        store = get_artifact_store()
        payload = store.get(str(artifact_id), tenant_id=str(tenant_id), principal_id=str(principal_id), motet_id=str(motet_id) if motet_id else None)
        if not isinstance(payload, (bytes, bytearray)):
            continue
        b64 = base64.b64encode(bytes(payload)).decode("utf-8")
        out.append({"type": "image", "source": {"type": "base64", "media_type": content_type, "data": b64}})

    return out


# Sources of per-turn system injections predating the explicit cache_volatile flag;
# kept for stored transcripts that carry only the source marker.
_VOLATILE_SYSTEM_SOURCES = frozenset({"pending_action", "memory_recall"})


def _is_volatile_system_message(m: Message) -> bool:
    """
    True for system messages whose content changes per turn (pending-action
    injections, memory recall, hook output). Fusing them into the cached system
    block would invalidate the prompt-cache prefix on every turn (ADR-0124).
    """
    metadata = getattr(m, "metadata", None)
    if not isinstance(metadata, dict):
        return False
    if metadata.get("cache_volatile"):
        return True
    return metadata.get("source") in _VOLATILE_SYSTEM_SOURCES


def _system_text_chunks(m: Message) -> List[str]:
    """Collect text content (content + text parts) from a system/developer message."""
    chunks: List[str] = []
    if m.content:
        chunks.append(m.content)
    parts = getattr(m, "content_parts", None) or []
    for part in parts:
        if getattr(part, "type", None) == "text":
            t = getattr(part, "text", None)
            if isinstance(t, str) and t:
                chunks.append(t)
        elif isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    return [c for c in chunks if c.strip()]


def _extract_system_and_messages(
    messages: List[Message],
    *,
    enable_prompt_caching: bool = False,
) -> Tuple[Any, List[Message]]:
    """
    Extract system/developer guidance into Anthropic `system` format and return remaining messages.

    Args:
        messages: Canonical messages
        enable_prompt_caching: If True, return system as content blocks. Stable
            chunks are fused into one block carrying the `cache_control`
            breakpoint; volatile per-turn chunks (pending action, memory recall,
            hook injections — see `_is_volatile_system_message`) follow as
            separate uncached blocks. Content after a breakpoint does not
            invalidate it, so the stable prefix survives across turns (ADR-0124).

    Returns:
        Tuple of (system_param, remaining_messages)
        - system_param is either a string or list of content blocks (for caching)

    NOTE: `Message.role` is a free string; treat "developer" as system-equivalent.
    """
    stable_chunks: List[str] = []
    volatile_chunks: List[str] = []
    remaining: List[Message] = []
    for m in messages:
        if m.role in {"system", "developer"}:
            target = volatile_chunks if _is_volatile_system_message(m) else stable_chunks
            target.extend(_system_text_chunks(m))
            continue
        remaining.append(m)

    if not stable_chunks and not volatile_chunks:
        return None, remaining

    if enable_prompt_caching:
        blocks: List[Dict[str, Any]] = []
        stable_text = "\n\n".join(stable_chunks)
        if stable_text:
            blocks.append(
                {
                    "type": "text",
                    "text": stable_text,
                    "cache_control": {"type": "ephemeral"},
                }
            )
        for chunk in volatile_chunks:
            blocks.append({"type": "text", "text": chunk})
        return (blocks or None), remaining

    system_text = "\n\n".join([*stable_chunks, *volatile_chunks])
    return (system_text or None), remaining


def _tool_calls_to_tool_use_blocks(tool_calls: List[Any]) -> List[Dict[str, Any]]:
    """Convert mixed assistant tool-call payloads into Anthropic tool_use blocks (ADR-0137)."""
    return tool_call_requests_to_anthropic_blocks(tool_call_requests_from_unknown(tool_calls))


def _is_replayable_thinking_block(block: Any) -> bool:
    """
    True when a block is an Anthropic thinking block that can be replayed verbatim:
    a ``thinking`` block with a ``signature`` (Anthropic verifies signatures on
    replay; unsigned blocks are rejected) or a ``redacted_thinking`` block with its
    opaque ``data``. Single source of truth for both capture and replay validation.
    """
    if not isinstance(block, dict):
        return False
    btype = block.get("type")
    if btype == "thinking":
        return isinstance(block.get("thinking"), str) and bool(block.get("signature"))
    if btype == "redacted_thinking":
        return bool(block.get("data"))
    return False


def _valid_anthropic_thinking_blocks(m: Message) -> List[Dict[str, Any]]:
    """
    Validate ``Message.reasoning_blocks`` as verbatim Anthropic thinking blocks.

    ``reasoning_blocks`` is provider-opaque and conversations can switch providers
    mid-stream, so only replay Anthropic-shaped blocks. Any foreign-shaped entry
    disqualifies the whole list (fail-soft: the turn is replayed without thinking
    blocks rather than partially).
    """
    raw_blocks = getattr(m, "reasoning_blocks", None)
    if not isinstance(raw_blocks, list) or not raw_blocks:
        return []
    if not all(_is_replayable_thinking_block(b) for b in raw_blocks):
        return []
    return list(raw_blocks)


def _format_messages_for_anthropic(
    *,
    messages: List[Message],
    request_context: Any,
    enable_prompt_caching: bool = False,
    include_thinking_blocks: bool = False,
) -> Tuple[Any, List[Dict[str, Any]]]:
    """
    Render canonical messages into Anthropic Messages `system` + `messages`.

    Args:
        messages: Canonical messages
        request_context: Request context with tenant/principal isolation
        enable_prompt_caching: If True, format system prompt for prompt caching
        include_thinking_blocks: If True (thinking enabled on this request), replay
            persisted thinking/redacted_thinking blocks (with signatures) ahead of
            the assistant turn's text/tool_use blocks so chain-of-thought carries
            across tool iterations. Anthropic rejects thinking blocks when thinking
            is disabled, so callers must gate this on the current request settings.

    Returns:
        Tuple of (system_param, formatted_messages)

    Tool results:
        Canonical `role="tool"` becomes `role="user"` with a `tool_result` block.
    """
    system, remaining = _extract_system_and_messages(messages, enable_prompt_caching=enable_prompt_caching)

    out: List[Dict[str, Any]] = []
    for m in remaining:
        if m.role == "tool":
            tool_use_id = str(getattr(m, "tool_call_id", None) or "")
            if not tool_use_id:
                # Fail-soft: cannot render a tool_result without correlation id.
                continue
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": _flatten_text_parts(m),
                        }
                    ],
                }
            )
            continue

        if m.role not in {"user", "assistant"}:
            # Fail-soft: ignore unknown roles.
            continue

        blocks: List[Dict[str, Any]] = []

        # ADR-0064 R10: Replay thinking blocks verbatim ahead of text/tool_use so the
        # model keeps chain-of-thought continuity across tool iterations.
        if m.role == "assistant" and include_thinking_blocks:
            blocks.extend(_valid_anthropic_thinking_blocks(m))

        text = _flatten_text_parts(m)
        if text:
            blocks.append({"type": "text", "text": text})

        # ADR-0062: multimodal parts (images) when enabled.
        blocks.extend(_image_parts_to_blocks(m=m, request_context=request_context))

        if m.role == "assistant":
            blocks.extend(tool_call_requests_to_anthropic_blocks(tool_calls_from_message(m)))

        out.append({"role": m.role, "content": blocks})

    return system, out


def _canonical_tools_to_anthropic(
    tools: Optional[List[CanonicalToolSchema]],
    *,
    enable_prompt_caching: bool = False,
) -> Optional[List[Dict[str, Any]]]:
    if not tools:
        return None
    out: List[Dict[str, Any]] = []
    for t in tools:
        # Map both canonical and namespaced web_search to Anthropic server tool
        if t.name in ("anthropic.web_search", "web_search"):
            # Anthropic built-in web search tool definition (ADR-0064).
            # Server tool is executed by Anthropic - results come back as web_search_tool_result.
            out.append(
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                }
            )
            continue
        out.append(
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.json_schema,
            }
        )
    # ADR-0124: mark the last tool as a cache breakpoint once schemas are stable
    # (agentic_loop sorts tools for prefix stability before calling the model).
    if enable_prompt_caching and out:
        out[-1] = {**out[-1], "cache_control": {"type": "ephemeral"}}
    return out


# ADR-0114: Anthropic has no response_format/json_schema mode. The supported way to
# guarantee schema-shaped output is to expose a single tool whose input_schema IS the
# requested JSON Schema and force the model to call it (tool_choice). The tool input is
# then unwrapped as the structured JSON text.
_STRUCTURED_TOOL_NAME = "emit_structured_output"


def _structured_output_tool(request: LLMRequest) -> Optional[Dict[str, Any]]:
    """Build a forced-tool definition from a JSON OutputContract, or None.

    Returns a tool whose ``input_schema`` is the contract's JSON Schema so that,
    when forced via ``tool_choice``, Anthropic constrains its output to that shape.
    """
    contract: Optional[OutputContract] = request.output_contract
    if not contract or contract.format != "json" or not contract.json_schema:
        return None
    return {
        "name": _STRUCTURED_TOOL_NAME,
        "description": "Return ONLY the final answer as a single object matching the required schema.",
        "input_schema": contract.json_schema,
    }


def _extract_forced_tool_json(tool_calls: List[ToolCallRequest]) -> Optional[str]:
    """Return the JSON text of the forced structured-output tool call, if present."""
    for tc in tool_calls:
        if tc.tool_name == _STRUCTURED_TOOL_NAME:
            return tc.arguments_json
    return None


def _stop_reason_to_canonical(stop_reason: Optional[str], *, has_tool_calls: bool) -> StopReason:
    if has_tool_calls:
        return StopReason.TOOL_CALLS
    mapped = {
        "end_turn": StopReason.NATURAL_STOP,
        "stop_sequence": StopReason.NATURAL_STOP,
        "max_tokens": StopReason.LENGTH_LIMIT,
        "tool_use": StopReason.TOOL_CALLS,
    }.get(str(stop_reason or ""), StopReason.NATURAL_STOP)
    return mapped


def _parse_usage(raw: Dict[str, Any]) -> Optional[LLMUsage]:
    """Parse Anthropic usage including prompt caching tokens."""
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        return None
    in_tok = usage.get("input_tokens")
    out_tok = usage.get("output_tokens")
    total = None
    try:
        if isinstance(in_tok, int) and isinstance(out_tok, int):
            total = in_tok + out_tok
    except Exception:
        total = None

    # Prompt caching tokens (Anthropic-specific)
    cache_read = usage.get("cache_read_input_tokens")
    cache_creation = usage.get("cache_creation_input_tokens")

    return LLMUsage(
        prompt_tokens=in_tok if isinstance(in_tok, int) else None,
        output_tokens=out_tok if isinstance(out_tok, int) else None,
        total_tokens=total,
        cache_read_tokens=cache_read if isinstance(cache_read, int) else None,
        cache_creation_tokens=cache_creation if isinstance(cache_creation, int) else None,
        provider_metadata=usage,
    )


def _parse_content_blocks(raw: Dict[str, Any]) -> Tuple[str, List[ToolCallRequest], List[ToolCallRequest], Optional[str]]:
    """
    Parse Anthropic content blocks into text, tool calls, and thinking.

    Handles both:
    - `tool_use` blocks: Regular tool calls that need local execution
    - `server_tool_use` blocks: Provider-executed builtins (e.g., web_search) - NOT included in tool_calls
      because Anthropic already executed them and included results in the response
    - `web_search_tool_result` blocks: Results from server-executed web search

    Returns:
        Tuple of (output_text, local_tool_calls, server_tool_calls, thinking_text)
        - local_tool_calls: tool_use blocks that need local execution
        - server_tool_calls: server_tool_use blocks (already executed by Anthropic, for observability only)
    """
    content = raw.get("content") or []
    text_chunks: List[str] = []
    thinking_chunks: List[str] = []
    tool_calls: List[ToolCallRequest] = []
    server_tool_calls: List[ToolCallRequest] = []

    # content may be list of dicts or list of SDK block objects
    if isinstance(content, list):
        for block in content:
            b = _as_dict(block)
            b_type = b.get("type")
            if b_type == "text" and isinstance(b.get("text"), str):
                text_chunks.append(b["text"])
            elif b_type == "thinking" and isinstance(b.get("thinking"), str):
                # Extended thinking block (Anthropic beta feature)
                thinking_chunks.append(b["thinking"])
            elif b_type == "tool_use":
                call_id = str(b.get("id") or "")
                name = str(b.get("name") or "")
                inp = b.get("input")
                inp_dict = inp if isinstance(inp, dict) else {}
                if call_id and name:
                    tool_calls.append(
                        inbound_tool_call_request(
                            call_id=call_id,
                            tool_name=name,
                            arguments_json=json.dumps(inp_dict),
                        )
                    )
            elif b_type == "server_tool_use":
                # ADR-0064: Server-executed tool (e.g., web_search) - already executed by Anthropic
                # These are NOT added to tool_calls because:
                # 1. Anthropic already executed them server-side
                # 2. Results are embedded in the response (web_search_tool_result blocks)
                # 3. No tool result message should be sent back
                # We track them separately for observability
                call_id = str(b.get("id") or "")
                name = str(b.get("name") or "")
                inp = b.get("input")
                inp_dict = inp if isinstance(inp, dict) else {}
                if call_id and name:
                    server_tool_calls.append(
                        inbound_tool_call_request(
                            call_id=call_id,
                            tool_name=name,
                            arguments_json=json.dumps(inp_dict),
                            kind="provider",
                        )
                    )

    thinking_text = "\n\n".join(thinking_chunks) if thinking_chunks else None
    return "".join(text_chunks), tool_calls, server_tool_calls, thinking_text


def _http_url(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    url = value.strip()
    return url if url.startswith("http") else None


def _append_web_citation(
    citations: List[Citation],
    seen: Set[str],
    *,
    url: Optional[str],
    title: Any,
    snippet: Any,
    metadata: Dict[str, Any],
) -> None:
    if not url or url in seen:
        return
    seen.add(url)
    title_s = title.strip() if isinstance(title, str) and title.strip() else None
    snippet_s = snippet.strip() if isinstance(snippet, str) and snippet.strip() else None
    citations.append(
        Citation(
            source_type="web",
            url=url,
            title=title_s,
            snippets=[snippet_s] if snippet_s else None,
            metadata=metadata,
        )
    )


def _extract_web_search_citations(raw: Dict[str, Any]) -> List[Citation]:
    """Collect URL citations from Anthropic web-search blocks.

    Prefer text-block ``web_search_result_location`` rows (they include
    ``cited_text``), then add leftover URLs from ``web_search_tool_result``.
    """
    content = raw.get("content") or []
    if not isinstance(content, list):
        return []

    citations: List[Citation] = []
    seen: Set[str] = set()

    for block in content:
        b = _as_dict(block)
        if b.get("type") != "text":
            continue
        for item in b.get("citations") or []:
            row = item if isinstance(item, dict) else _as_dict(item)
            if not row:
                continue
            _append_web_citation(
                citations,
                seen,
                url=_http_url(row.get("url")),
                title=row.get("title"),
                snippet=row.get("cited_text") or row.get("citedText"),
                metadata={"source": "web_search_result_location"},
            )

    for block in content:
        b = _as_dict(block)
        if b.get("type") != "web_search_tool_result":
            continue
        payload = b.get("content")
        if isinstance(payload, dict) and payload.get("type") == "web_search_tool_result_error":
            continue
        items: List[Any]
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            nested = payload.get("results") or payload.get("content")
            items = nested if isinstance(nested, list) else [payload]
        else:
            continue
        for item in items:
            row = item if isinstance(item, dict) else _as_dict(item)
            if not row or row.get("type") == "web_search_tool_result_error":
                continue
            _append_web_citation(
                citations,
                seen,
                url=_http_url(row.get("url")),
                title=row.get("title"),
                snippet=None,
                metadata={
                    "source": "web_search_tool_result",
                    "tool_use_id": b.get("tool_use_id"),
                },
            )

    return citations


def _extract_thinking_replay_blocks(raw: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """
    Capture verbatim thinking/redacted_thinking blocks for multi-turn replay (ADR-0064 R10).

    Anthropic returns ``thinking`` blocks with a cryptographic ``signature`` (and opaque
    ``redacted_thinking`` blocks). Replaying them unmodified ahead of the assistant
    turn's tool_use blocks preserves chain-of-thought across tool iterations. Unsigned
    thinking blocks are excluded (Anthropic verifies signatures on replay).
    """
    content = raw.get("content") or []
    if not isinstance(content, list):
        return None
    blocks = [b for b in content if _is_replayable_thinking_block(b)]
    return blocks or None


def _get_builtin_tool_names(request: LLMRequest) -> List[str]:
    return [str(t.name) for t in (request.tools or []) if getattr(t, "name", None)]


def _normalise_anthropic_thinking_mode(value: Any) -> Optional[str]:
    """Return the requested Anthropic thinking mode when explicitly configured."""
    if not isinstance(value, str):
        return None
    mode = value.strip().lower().replace("_", "-")
    if mode in {"adaptive"}:
        return "adaptive"
    if mode in {"enabled", "fixed", "budget", "budget-tokens"}:
        return "enabled"
    return None


def _normalise_anthropic_effort(
    value: Any,
    *,
    default: ReasoningEffort = "medium",
    supported: Optional[Iterable[str]] = None,
) -> ReasoningEffort:
    """Map Motet reasoning effort onto Anthropic output_config.effort."""
    return normalize_reasoning_effort(value, default=default, supported=supported)


def _normalise_anthropic_thinking_display(value: Any) -> str:
    """Return the Anthropic thinking display mode used for visible thinking summaries."""
    if not isinstance(value, str):
        return "summarized"
    display = value.strip().lower().replace("_", "-")
    if display in {"summarized", "omitted"}:
        return display
    return "summarized"


@dataclass(frozen=True)
class _ClaudeModel:
    """
    Family and version parsed from a Claude model id.

    Every behavioral predicate below keys off this, so model-id parsing lives in one
    place: adding a family or version means changing the parse once, and the policy
    functions stay readable as pure statements about Anthropic's API contract.
    """

    family: str  # opus | sonnet | haiku | fable | mythos | "" when unrecognized
    major: int
    minor: int

    @property
    def is_always_on_thinking_family(self) -> bool:
        """Fable/Mythos always think; they reject `thinking.type=disabled` outright."""
        return self.family in {"fable", "mythos"}


def _parse_claude_model(model_name: str) -> _ClaudeModel:
    """
    Parse a Claude model id into family/major/minor.

    Handles dot and underscore separators (``claude-opus-4.8``) and trailing date
    snapshots (``claude-haiku-4-5-20251001``): the version regex stops after the
    second number group, so the date is never read as a version component.
    """
    normalized = model_name.lower().replace(".", "-").replace("_", "-")
    for family in ("fable", "mythos"):
        if family in normalized:
            match = re.search(rf"{family}-(\d+)(?:-(\d+))?", normalized)
            major = int(match.group(1)) if match else 0
            minor = int(match.group(2)) if match and match.group(2) is not None else 0
            return _ClaudeModel(family=family, major=major, minor=minor)

    match = re.search(r"claude-(opus|sonnet|haiku)-(\d+)(?:-(\d+))?", normalized)
    if not match:
        return _ClaudeModel(family="", major=0, minor=0)
    return _ClaudeModel(
        family=match.group(1),
        major=int(match.group(2)),
        minor=int(match.group(3)) if match.group(3) is not None else 0,
    )


def _anthropic_model_prefers_adaptive_thinking(model_name: str) -> bool:
    """
    Return True for Anthropic models whose current API expects adaptive thinking.

    Newer Claude families reject the fixed-budget `thinking.type=enabled` shape with
    a 400 and require `thinking.type=adaptive` plus `output_config.effort`.

    Version-aware so new releases default correctly: opus/sonnet/haiku 4.6+ and all
    5-series models are adaptive, as are the Mythos/Fable families. Older versioned
    snapshots (e.g. claude-sonnet-4-5-20250929) keep the fixed-budget shape.
    """
    model = _parse_claude_model(model_name)
    if model.is_always_on_thinking_family:
        return True
    if not model.family:
        return False
    return model.major >= 5 or (model.major == 4 and model.minor >= 6)


def _default_anthropic_effort(model_name: str) -> ReasoningEffort:
    """
    Effort to use when the caller did not specify one.

    Anthropic tunes its adaptive-thinking families around a ``high`` default, so
    Motet's generic ``medium`` would silently run them below the level the provider
    recommends. Legacy fixed-budget models keep ``medium``.
    """
    return "high" if _anthropic_model_prefers_adaptive_thinking(model_name) else "medium"


def _anthropic_model_requires_explicit_thinking_disable(model_name: str) -> bool:
    """
    Return True when turning thinking off requires sending ``thinking.type=disabled``.

    Claude Opus 5 / Sonnet 5 think by default, so omitting the field leaves thinking
    on and makes Motet's toggle a no-op. Fable/Mythos are excluded because they reject
    ``disabled`` entirely ("Thinking defaults to adaptive mode when not specified").
    Pre-5 opus/sonnet treat an omitted ``thinking`` field as off.
    """
    model = _parse_claude_model(model_name)
    if model.is_always_on_thinking_family:
        return False
    return model.family in {"opus", "sonnet"} and model.major >= 5


def _anthropic_effort_ceiling_when_thinking_disabled(model_name: str) -> Optional[Iterable[str]]:
    """
    Supported effort rungs alongside ``thinking.type=disabled``, or None when unrestricted.

    Opus 5 returns a 400 ("output_config.effort 'max' is not supported when thinking is
    disabled on this model") above ``high``. The restriction is Opus-specific: Sonnet 5
    accepts disabled at ``max``, so clamping it there would needlessly lower the caller's
    requested effort.
    """
    model = _parse_claude_model(model_name)
    if model.family == "opus" and model.major >= 5:
        return ("low", "medium", "high")
    return None


def _anthropic_model_supports_temperature(model_name: str) -> bool:
    """
    Return False for Anthropic models that have deprecated the ``temperature`` parameter.

    The adaptive-thinking Claude families (opus/sonnet 4.6+, mythos) reject any
    request containing ``temperature`` with a 400
    ("`temperature` is deprecated for this model.") — even when thinking is
    disabled — so it must be omitted from the request entirely. Older models
    still accept it.
    """
    return not _anthropic_model_prefers_adaptive_thinking(model_name)


def _apply_anthropic_thinking_params(
    *,
    params: Dict[str, Any],
    settings: Dict[str, Any],
    model_name: str,
) -> bool:
    """
    Mutate Anthropic request params with the thinking shape supported by the model.

    Supports both Anthropic APIs:
    - Fixed budget: `thinking={"type": "enabled", "budget_tokens": ...}`
    - Adaptive: `thinking={"type": "adaptive"}` plus `output_config={"effort": ...}`
    - Thinking-on-by-default (Opus/Sonnet 5+): when Motet disables thinking, send
      ``thinking={"type": "disabled"}``, clamping effort to ``high`` for families that
      reject higher levels alongside disabled thinking (Opus 5+).

    Returns True when thinking was enabled.
    """
    if not bool(settings.get("enable_thinking", False)):
        if _anthropic_model_requires_explicit_thinking_disable(model_name):
            default_effort = _default_anthropic_effort(model_name)
            requested = _normalise_anthropic_effort(settings.get("reasoning_effort"), default=default_effort)
            effort = _normalise_anthropic_effort(
                requested,
                default=default_effort,
                supported=_anthropic_effort_ceiling_when_thinking_disabled(model_name),
            )
            params["thinking"] = {"type": "disabled"}
            params["output_config"] = {"effort": effort}
            params.pop("temperature", None)
            logger.info(
                "anthropic_thinking_explicitly_disabled",
                model=model_name,
                output_effort=effort,
                effort_clamped_from=requested if effort != requested else None,
                note="Model defaults thinking on; Motet enable_thinking=False requires explicit disabled.",
            )
        return False

    configured_mode = _normalise_anthropic_thinking_mode(
        settings.get("anthropic_thinking_type")
        or settings.get("anthropic_thinking_mode")
        or settings.get("thinking_type")
        or settings.get("thinking_mode")
    )
    mode = configured_mode or ("adaptive" if _anthropic_model_prefers_adaptive_thinking(model_name) else "enabled")

    if mode == "adaptive":
        params["thinking"] = {
            "type": "adaptive",
            "display": _normalise_anthropic_thinking_display(settings.get("anthropic_thinking_display")),
        }
        params["output_config"] = {
            "effort": _normalise_anthropic_effort(
                settings.get("reasoning_effort"), default=_default_anthropic_effort(model_name)
            )
        }
    else:
        thinking_budget = settings.get("thinking_budget_tokens")
        params["thinking"] = {
            "type": "enabled",
            "budget_tokens": thinking_budget if isinstance(thinking_budget, int) and thinking_budget > 0 else 10000,
        }

    # Anthropic thinking modes do not accept arbitrary temperature controls.
    params.pop("temperature", None)
    return True


def _anthropic_content_type(value: Any) -> Optional[str]:
    """Extract an Anthropic SDK object's type without logging provider text payloads."""
    if isinstance(value, dict):
        raw_type = value.get("type")
    else:
        raw_type = getattr(value, "type", None)
    return str(raw_type) if isinstance(raw_type, str) and raw_type else None


def _anthropic_text_len(value: Any, key: str) -> int:
    """Return the length of a provider text field without exposing the text itself."""
    if isinstance(value, dict):
        text = value.get(key)
    else:
        text = getattr(value, key, None)
    return len(text) if isinstance(text, str) else 0


def _anthropic_content_block_types(raw: Dict[str, Any]) -> List[str]:
    """Return final Anthropic content block types for observability."""
    content = raw.get("content") or []
    if not isinstance(content, list):
        return []
    block_types: List[str] = []
    for block in content:
        block_type = _anthropic_content_type(block)
        if block_type:
            block_types.append(block_type)
    return block_types


def _log_anthropic_thinking_request(
    *,
    operation: str,
    model_name: str,
    params: Dict[str, Any],
    beta_features: List[str],
) -> None:
    """Log metadata-only Anthropic thinking request configuration."""
    thinking = params.get("thinking") if isinstance(params.get("thinking"), dict) else {}
    output_config = params.get("output_config") if isinstance(params.get("output_config"), dict) else {}
    logger.debug(
        "anthropic_thinking_request_configured",
        operation=operation,
        model=model_name,
        thinking_type=thinking.get("type"),
        thinking_display=thinking.get("display"),
        thinking_budget_tokens=thinking.get("budget_tokens"),
        output_effort=output_config.get("effort"),
        beta_features=list(beta_features),
        temperature_present="temperature" in params,
    )


def _extract_provider_tool_use_events(
    raw: Dict[str, Any],
    builtin_tool_names: List[str],
) -> List[ToolUseEvent]:
    content = raw.get("content") or []
    if not isinstance(content, list) or not builtin_tool_names:
        return []

    provider_tool_map = {}
    if "anthropic.web_search" in builtin_tool_names:
        provider_tool_map["web_search"] = "anthropic.web_search"

    events: List[ToolUseEvent] = []
    for block in content:
        b = _as_dict(block)
        if b.get("type") != "tool_use":
            continue
        name = str(b.get("name") or "")
        canonical_name = provider_tool_map.get(name)
        if canonical_name:
            tool_call_id = b.get("id")
            events.append(
                ToolUseEvent(
                    kind="provider",
                    tool_name=canonical_name,
                    tool_call_id=str(tool_call_id) if tool_call_id else None,
                    status=None,
                    metadata=b,
                )
            )
    return events


@dataclass
class _PreparedCall:
    """Provider-ready request state shared by complete() and stream()."""

    client: Any
    params: Dict[str, Any]
    model_name: str
    enable_thinking: bool
    apply_structured: bool
    builtin_tool_names: List[str]


@dataclass
class AnthropicMessagesAdapter:
    provider: str
    adapter_name: str
    credentials: Optional[Dict[str, Any]] = None

    def capabilities(self, *, model: str) -> CapabilityDescriptor:
        spec = get_model_spec("anthropic", model)
        caps = set(spec.capabilities) if spec else set()
        return CapabilityDescriptor(
            provider=self.provider,
            model=model,
            supports_streaming=CAP_STREAM in caps,
            supports_tools=CAP_TOOL_USE in caps,
            supports_parallel_tool_calls=CAP_TOOL_USE in caps,
            supports_tool_call_id=True,  # tool_use.id exists
            supports_vision=CAP_VISION in caps,
            supports_json_mode=CAP_JSON_MODE in caps,
            # ADR-0114: emulated via forced tool-use (tool_choice) over the schema.
            supports_json_schema_strict=CAP_JSON_MODE in caps,
            supports_stateful_sessions=False,
            supports_builtin_tools=bool(getattr(spec, "supported_builtin_tools", None)),
            supports_system_prompt=CAP_SYSTEM_PROMPT in caps if spec else True,
            supports_reasoning=("reasoning" in caps),
            provider_metadata={"adapter": "anthropic_messages"},
        )

    def _prepare_call(self, request: LLMRequest, *, operation: str) -> _PreparedCall:
        """
        Shared request preparation for complete() and stream(): validate settings,
        sanitize + render history, translate tools, gate structured output (ADR-0114),
        build provider params/beta headers, and construct the SDK client.
        """
        settings = request.model_settings or {}
        model_name = settings.get("model_name") or settings.get("model") or ""
        if not model_name:
            raise ValueError("LLMRequest.model_settings.model_name is required for Anthropic messages adapter")

        api_key = (self.credentials or {}).get("anthropic_api_key") or (self.credentials or {}).get("api_key")
        if not api_key:
            raise ValueError("Anthropic API key missing (expected credentials.anthropic_api_key or credentials.api_key)")

        # ADR-0124: prompt caching is capability-gated; flag alone is a no-op without CAP.
        enable_prompt_caching = prompt_caching_enabled(request, provider=self.provider)

        safe_messages, sanitize_stats = sanitize_orphan_tool_call_messages(request.messages)
        if sanitize_stats["removed_assistant_calls"] > 0 or sanitize_stats["removed_tool_messages"] > 0:
            logger.warning(
                "provider_boundary_orphan_tool_calls_pruned",
                provider=self.provider,
                model=model_name,
                removed_assistant_calls=sanitize_stats["removed_assistant_calls"],
                removed_tool_messages=sanitize_stats["removed_tool_messages"],
            )

        system, msgs = _format_messages_for_anthropic(
            messages=safe_messages,
            request_context=request.request_context,
            enable_prompt_caching=enable_prompt_caching,
            include_thinking_blocks=bool(settings.get("enable_thinking", False)),
        )
        assert_trailing_user_turn(msgs, provider=self.provider, model=str(model_name))
        anthropic_tools = _canonical_tools_to_anthropic(
            request.tools,
            enable_prompt_caching=enable_prompt_caching,
        )
        builtin_tool_names = _get_builtin_tool_names(request)

        # ADR-0114: structured output is emulated by forcing a single schema tool.
        # It cannot be combined with caller tools or extended thinking, so degrade
        # (unconstrained) in those cases rather than send a contradictory request.
        structured_tool = _structured_output_tool(request)
        wants_thinking = bool(settings.get("enable_thinking", False))
        apply_structured = bool(structured_tool) and not anthropic_tools and not wants_thinking
        if structured_tool and not apply_structured:
            logger.warning(
                "anthropic_structured_output_degraded",
                model=model_name,
                reason="tools_present" if anthropic_tools else "thinking_enabled",
            )

        from ...output_limits import resolve_max_output_tokens

        # Anthropic Messages requires max_tokens; do not invent a magic default —
        # resolve from request or ModelSpec, else fail loudly.
        max_tokens = resolve_max_output_tokens(
            settings,
            provider=self.provider,
            model_name=model_name,
            fallback=None,
        )
        if max_tokens is None:
            raise ValueError(
                "Anthropic Messages requires max_tokens; set model_settings.max_tokens "
                f"or register ModelSpec.max_output_tokens for {self.provider}/{model_name}"
            )

        # IMPORTANT: temperature=0.0 is a valid setting; do not treat falsy as "unset".
        temperature_raw = settings.get("temperature", 0.2)
        temperature = float(0.2 if temperature_raw is None else temperature_raw)

        try:
            from anthropic import Anthropic
        except Exception as exc:
            raise RuntimeError("anthropic package not available") from exc

        # Build extra headers for beta features
        beta_features: List[str] = []
        if enable_prompt_caching:
            beta_features.append("prompt-caching-2024-07-31")

        extra_headers: Optional[Dict[str, str]] = None
        if beta_features:
            extra_headers = {"anthropic-beta": ",".join(beta_features)}

        client = Anthropic(api_key=api_key, default_headers=extra_headers)
        params: Dict[str, Any] = {
            "model": model_name,
            "max_tokens": max_tokens,
            "messages": msgs,
        }
        if _anthropic_model_supports_temperature(str(model_name)):
            params["temperature"] = temperature
        elif settings.get("temperature") is not None:
            logger.debug(
                "anthropic_temperature_dropped_deprecated",
                model=model_name,
                requested_temperature=settings.get("temperature"),
                note="This model family rejects `temperature` with a 400; omitting it.",
            )
        if system:
            params["system"] = system
        if anthropic_tools:
            params["tools"] = anthropic_tools
        if apply_structured:
            params["tools"] = [structured_tool]
            params["tool_choice"] = {"type": "tool", "name": _STRUCTURED_TOOL_NAME}
            logger.info("anthropic_structured_output_constrained", model=model_name, operation=operation)

        # Extended/adaptive thinking support. Enable via model_settings.enable_thinking = True.
        enable_thinking = _apply_anthropic_thinking_params(params=params, settings=settings, model_name=str(model_name))
        if enable_thinking:
            # Add beta header for extended thinking
            beta_features.append("interleaved-thinking-2025-05-14")
            extra_headers = {"anthropic-beta": ",".join(beta_features)}
            client = Anthropic(api_key=api_key, default_headers=extra_headers)
            _log_anthropic_thinking_request(
                operation=operation,
                model_name=str(model_name),
                params=params,
                beta_features=beta_features,
            )

        return _PreparedCall(
            client=client,
            params=params,
            model_name=str(model_name),
            enable_thinking=enable_thinking,
            apply_structured=apply_structured,
            builtin_tool_names=builtin_tool_names,
        )

    def complete(self, request: LLMRequest) -> LLMResponse:
        prepared = self._prepare_call(request, operation="complete")
        model_name = prepared.model_name
        enable_thinking = prepared.enable_thinking
        apply_structured = prepared.apply_structured

        result = prepared.client.messages.create(**prepared.params)
        raw = _as_dict(result)

        output_text, tool_calls, server_tool_calls, thinking_text = _parse_content_blocks(raw)
        if enable_thinking:
            logger.debug(
                "anthropic_thinking_response_metadata",
                operation="complete",
                model=model_name,
                final_content_block_types=_anthropic_content_block_types(raw),
                has_thinking_text=bool(thinking_text),
                thinking_text_len=len(thinking_text) if isinstance(thinking_text, str) else 0,
            )
        usage = _parse_usage(raw)

        # Include thinking and server tool calls in raw metadata for observability
        raw_metadata: Dict[str, Any] = {"raw": raw}
        if thinking_text:
            raw_metadata["thinking_text"] = thinking_text
        if server_tool_calls:
            # Track server-executed tools for observability (these were already executed by Anthropic)
            raw_metadata["server_tool_calls"] = [tc.model_dump() for tc in server_tool_calls]

        # ADR-0114: unwrap the forced structured-output tool call into plain JSON text
        # so downstream consumers see a normal (schema-conformant) text response.
        if apply_structured:
            structured_text = _extract_forced_tool_json(tool_calls)
            structured_items: List[Any] = [TextPart(text=structured_text)] if structured_text else []
            return LLMResponse(
                output_text=structured_text or None,
                output_items=structured_items,
                stop_reason=StopReason.NATURAL_STOP,
                usage=usage,
                raw_provider_metadata=raw_metadata,
            )

        # Only consider local tool_use blocks for stop_reason (not server_tool_use)
        stop_reason = _stop_reason_to_canonical(raw.get("stop_reason"), has_tool_calls=bool(tool_calls))

        output_items: List[Any] = []
        if output_text:
            output_items.append(TextPart(text=output_text))
        output_items.extend(tool_calls)
        citations = _extract_web_search_citations(raw)

        return LLMResponse(
            output_text=output_text or None,
            output_items=output_items,
            stop_reason=stop_reason,
            usage=usage,
            raw_provider_metadata=raw_metadata,
            citations=citations or None,
            # ADR-0064 R10: Reasoning for persistence + verbatim signed thinking blocks
            # for multi-turn replay (thinking blocks only appear when thinking is enabled).
            reasoning_content=thinking_text,
            reasoning_blocks=_extract_thinking_replay_blocks(raw),
        )

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]:
        prepared = self._prepare_call(request, operation="stream")
        client = prepared.client
        params = prepared.params
        enable_thinking = prepared.enable_thinking
        apply_structured = prepared.apply_structured
        builtin_tool_names = prepared.builtin_tool_names

        # Best-effort streaming:
        # - emit thinking deltas when extended thinking is enabled
        # - emit text deltas via text_stream when available
        # - emit tool calls on completion by parsing the final message
        try:
            with client.messages.stream(**params) as stream:
                # ADR-0114: forced structured-output tool. Anthropic streams the tool
                # input as `input_json_delta` chunks; surface them as text deltas so the
                # consumer receives the (schema-shaped) JSON incrementally.
                if apply_structured:
                    emitted_any = False
                    for event in stream:
                        event_dict = _as_dict(event)
                        if event_dict.get("type") != "content_block_delta":
                            continue
                        delta = event_dict.get("delta", {})
                        if _anthropic_content_type(delta) == "input_json_delta":
                            partial = delta.get("partial_json", "") if isinstance(delta, dict) else ""
                            if partial:
                                emitted_any = True
                                yield TextDeltaEvent(text=str(partial))

                    final_msg = None
                    getter = getattr(stream, "get_final_message", None)
                    if callable(getter):
                        final_msg = getter()
                    raw_final = _as_dict(final_msg) if final_msg is not None else {}
                    if not emitted_any:
                        # Fallback: no deltas captured; emit the final tool input once.
                        _, final_tool_calls, _, _ = _parse_content_blocks(raw_final)
                        structured_text = _extract_forced_tool_json(final_tool_calls)
                        if structured_text:
                            yield TextDeltaEvent(text=structured_text)

                    usage = _parse_usage(raw_final)
                    if usage is not None:
                        yield UsageEvent(usage=usage)
                    yield StopEvent(reason=StopReason.NATURAL_STOP)
                    return

                # Track if we're in a thinking block for streaming
                thinking_accumulated: List[str] = []

                # Use event-based iteration for thinking support
                if enable_thinking:
                    for event in stream:
                        event_dict = _as_dict(event)
                        event_type = event_dict.get("type")
                        block = event_dict.get("content_block", {})
                        delta = event_dict.get("delta", {})
                        block_type = _anthropic_content_type(block)
                        delta_type = _anthropic_content_type(delta)
                        logger.debug(
                            "anthropic_stream_event_metadata",
                            event_type=event_type,
                            content_block_type=block_type,
                            delta_type=delta_type,
                            thinking_delta_len=_anthropic_text_len(delta, "thinking"),
                            text_delta_len=_anthropic_text_len(delta, "text"),
                        )

                        if event_type == "content_block_start":
                            if isinstance(block, dict) and block.get("type") == "thinking":
                                logger.debug("anthropic_thinking_block_started", block_type=block_type)
                        elif event_type == "content_block_delta":
                            if isinstance(delta, dict):
                                if delta_type == "thinking_delta":
                                    thinking_text = delta.get("thinking", "")
                                    if thinking_text:
                                        thinking_accumulated.append(thinking_text)
                                        logger.debug(
                                            "anthropic_thinking_delta_received",
                                            delta_len=len(thinking_text),
                                            accumulated_len=sum(len(chunk) for chunk in thinking_accumulated),
                                        )
                                        yield ThinkingEvent(text=thinking_text, is_complete=False)
                                elif delta_type == "text_delta":
                                    text = delta.get("text", "")
                                    if text:
                                        yield TextDeltaEvent(text=text)
                        elif event_type == "content_block_stop":
                            # Thinking block complete - emit final event if we had thinking
                            if thinking_accumulated:
                                logger.debug(
                                    "anthropic_thinking_block_completed",
                                    accumulated_len=sum(len(chunk) for chunk in thinking_accumulated),
                                )
                                yield ThinkingEvent(text="", is_complete=True)
                else:
                    # Standard text streaming without thinking
                    text_stream = getattr(stream, "text_stream", None)
                    if text_stream is not None:
                        for chunk in text_stream:
                            if chunk:
                                yield TextDeltaEvent(text=str(chunk))

                final_msg = None
                getter = getattr(stream, "get_final_message", None)
                if callable(getter):
                    final_msg = getter()

                raw_final = _as_dict(final_msg) if final_msg is not None else {}
                output_text, tool_calls, server_tool_calls, thinking_text = _parse_content_blocks(raw_final)
                if enable_thinking:
                    logger.debug(
                        "anthropic_stream_final_message_metadata",
                        final_content_block_types=_anthropic_content_block_types(raw_final),
                        thinking_delta_count=len(thinking_accumulated),
                        streamed_thinking_len=sum(len(chunk) for chunk in thinking_accumulated),
                        has_final_thinking_text=bool(thinking_text),
                        final_thinking_text_len=len(thinking_text) if isinstance(thinking_text, str) else 0,
                    )

                # ADR-0064 R10: Deliver verbatim signed thinking blocks on a final
                # ThinkingEvent so orchestration persists them for multi-turn replay.
                if enable_thinking:
                    replay_blocks = _extract_thinking_replay_blocks(raw_final)
                    if replay_blocks:
                        yield ThinkingEvent(text="", is_complete=True, blocks=replay_blocks)

                # Emit ToolUseEvent for server-executed tools (observability only)
                provider_tool_events = _extract_provider_tool_use_events(raw_final, builtin_tool_names)
                for event in provider_tool_events:
                    yield event

                citations = _extract_web_search_citations(raw_final)
                if citations:
                    yield CitationsEvent(citations=citations)

                # Emit ToolCallCompleteEvent for server-executed tools (observability)
                # These have kind="provider" to indicate they were already executed by Anthropic
                for tc in server_tool_calls:
                    yield ToolCallCompleteEvent(
                        call_id=tc.call_id,
                        tool_name=tc.tool_name,
                        arguments_json=tc.arguments_json,
                        kind="provider",  # Server-executed, don't execute locally
                    )

                # Emit ToolCallCompleteEvent for local tool calls (need local execution)
                for tc in tool_calls:
                    yield ToolCallCompleteEvent(
                        call_id=tc.call_id,
                        tool_name=tc.tool_name,
                        arguments_json=tc.arguments_json,
                        kind=None,  # Local tool, needs execution
                    )

                usage = _parse_usage(raw_final)
                if usage is not None:
                    yield UsageEvent(usage=usage)

                # Only consider local tool_use blocks for stop_reason (not server_tool_use)
                stop_reason = _stop_reason_to_canonical(raw_final.get("stop_reason"), has_tool_calls=bool(tool_calls))
                yield StopEvent(reason=stop_reason)
        except Exception as exc:
            logger.error("anthropic_stream_failed", error=str(exc), error_type=type(exc).__name__, exc_info=True)
            yield ErrorEvent(error_type=type(exc).__name__, message=str(exc))
            yield StopEvent(reason=StopReason.ERROR)


__all__ = ["AnthropicMessagesAdapter", "_format_messages_for_anthropic"]

