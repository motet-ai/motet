"""
Motet - OpenAI Compatible Translation

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Translation between the OpenAI wire format and Motet canonical types for the
    compatibility facade.

    This module is the whole reason the facade preserves OpenAI shapes
    are converted to canonical Message / CanonicalToolSchema / OutputContract on
    the way in, and canonical inference results are rendered back to OpenAI
    shapes on the way out. Nothing downstream of this module sees OpenAI wire
    types, and nothing upstream sees canonical types.

    Tool names cross the boundary in wire form (``mcp__server__tool``) and are
    converted to canonical form (``mcp.server.tool``) on receipt, matching the
    convention Motet already applies at the outbound provider boundary.

    Thinking maps to Motet ``enable_thinking`` when the model has
    ``CAP_REASONING`` and either the client opts in (``reasoning_effort`` /
    ``reasoning`` / ``motet_enable_thinking``) or facade policy sets
    ``force_thinking``. Outbound thinking is rendered as Chat Completions
    ``reasoning_content`` and Responses ``reasoning`` output items.

Dependencies:
    - motet.core.types: canonical Message, ContentPart, OutputContract, tool schemas
    - motet.core.models.registry: model id resolution against the registry
    - motet.core.models.adapters.provider_builtin_tools: tool name wire mapping

Usage:
    from motet.interfaces.api.openai_compat import translation

    provider, key, spec = translation.resolve_model(req.model, policy)
    messages = translation.messages_to_canonical(req)
    payload = translation.completion_payload(result, model_id="openai/gpt-4o-mini")

Notes:
    - Unsupported parameters raise rather than being silently dropped
    - Model resolution enforces the credential allowlist
    - finish_reason mapping follows canonical StopReason semantics
    - Streaming tool calls render per endpoint: ``tool_call_delta_to_openai`` for
      Chat Completions increments, ``function_call_item`` for Responses items
      (shared with the final snapshot so item ids match)
"""

from __future__ import annotations

import json
import mimetypes
from typing import Any, Dict, List, Optional, Tuple

import structlog

from ....core.models.adapters.provider_builtin_tools import (
    tool_canonical_to_wire,
    tool_wire_to_canonical,
)
from ....core.models.registry import list_models_with_keys
from ....core.security.facade_policy import FacadePolicy
from ....core.types import (
    CanonicalToolSchema,
    ContentPart,
    MediaPart,
    Message,
    OutputContract,
    TextPart,
    ToolCallRequest,
    normalize_reasoning_effort,
)
from .errors import FacadeError
from .wire import (
    ChatCompletionRequest,
    new_call_id,
    new_completion_id,
    new_message_id,
    new_response_id,
    now_ts,
)

logger = structlog.get_logger(__name__)

# Parameters Motet cannot honor in a way the client would recognize. Accepting
# them silently would make the facade lie about what it did (ADR-0125 §5f, §8).
_REJECTED_PARAMS = ("logprobs", "top_logprobs")

# Parameters forwarded into model_settings. Adapters honor a subset today; the
# rest ride along so adapter support does not require a facade change.
_FORWARDED_PARAMS = ("top_p", "seed", "stop", "presence_penalty", "frequency_penalty")


# ---------------------------------------------------------------------------
# Model identity
# ---------------------------------------------------------------------------


def facade_model_id(provider: str, registry_key: str) -> str:
    """Render the facade-visible model id for a registry entry."""
    return f"{provider}/{registry_key}"


def resolve_model(model: str, policy: FacadePolicy) -> Tuple[str, str, Any]:
    """Resolve a client model string to a registry entry, enforcing the allowlist.

    Accepts ``provider/registry_key`` and, when unambiguous, a bare
    ``registry_key``. Denied models return 404 rather than 403 so a client's
    model picker degrades the same way it would against OpenAI for an unknown
    model, without disclosing which models exist behind the allowlist.
    """
    if not model or not str(model).strip():
        raise FacadeError(400, "model is required", code="missing_model", param="model")

    requested = str(model).strip()
    provider: Optional[str] = None
    key = requested
    if "/" in requested:
        provider, key = requested.split("/", 1)
        provider = provider.strip().lower()
        key = key.strip()

    matches = [
        (prov, registry_key, spec)
        for prov, registry_key, spec in list_models_with_keys()
        if registry_key == key and (provider is None or prov == provider)
    ]

    if not matches:
        raise FacadeError(
            404,
            f"model '{requested}' not found",
            error_type="not_found_error",
            code="model_not_found",
            param="model",
        )
    if len(matches) > 1:
        options = ", ".join(sorted(facade_model_id(p, k) for p, k, _ in matches))
        raise FacadeError(
            400,
            f"model '{requested}' is ambiguous; qualify it as provider/model ({options})",
            code="ambiguous_model",
            param="model",
        )

    prov, registry_key, spec = matches[0]
    if not policy.allows_model(prov, registry_key):
        # Deliberately indistinguishable from "unknown model" to the client.
        logger.warning(
            "openai_compat_model_denied",
            model=requested,
            provider=prov,
            registry_key=registry_key,
            allowlist_source=policy.allowlist_source,
        )
        raise FacadeError(
            404,
            f"model '{requested}' not found",
            error_type="not_found_error",
            code="model_not_found",
            param="model",
        )
    return prov, registry_key, spec


def allowed_models(policy: FacadePolicy) -> List[Tuple[str, str, Any]]:
    """List registry entries this credential may use."""
    return [
        (provider, key, spec)
        for provider, key, spec in list_models_with_keys()
        if policy.allows_model(provider, key)
    ]


# ---------------------------------------------------------------------------
# Inbound: OpenAI wire -> canonical
# ---------------------------------------------------------------------------


def validate_supported(req: ChatCompletionRequest) -> None:
    """Reject parameters whose absence the client would not detect."""
    if req.n is not None and req.n > 1:
        raise FacadeError(
            400,
            "n > 1 is not supported; request one choice per call",
            code="unsupported_parameter",
            param="n",
        )
    for name in _REJECTED_PARAMS:
        if getattr(req, name, None):
            raise FacadeError(
                400,
                f"{name} is not supported by this endpoint",
                code="unsupported_parameter",
                param=name,
            )


def _media_part_from_url(url: str, detail: Optional[str]) -> MediaPart:
    """Build a canonical MediaPart from a data URL or remote image URL."""
    if url.startswith("data:"):
        header, _, payload = url.partition(",")
        mime_type = header[5:].split(";")[0] or "image/png"
        return MediaPart(
            media_type="image",
            mime_type=mime_type,
            base64_data=payload,
            detail=detail,  # type: ignore[arg-type]
        )
    guessed, _ = mimetypes.guess_type(url)
    return MediaPart(
        media_type="image",
        mime_type=guessed or "image/png",
        url=url,
        detail=detail,  # type: ignore[arg-type]
    )


def _content_to_parts(content: Any) -> Tuple[str, Optional[List[ContentPart]]]:
    """Convert OpenAI message content to (flat text, canonical parts)."""
    if content is None:
        return "", None
    if isinstance(content, str):
        return content, None

    if not isinstance(content, list):
        return str(content), None

    parts: List[ContentPart] = []
    texts: List[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(TextPart(text=item))
            texts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in ("text", "input_text", "output_text"):
            text = str(item.get("text") or "")
            parts.append(TextPart(text=text))
            texts.append(text)
        elif item_type in ("image_url", "input_image"):
            raw = item.get("image_url") or item.get("image")
            detail = item.get("detail")
            if isinstance(raw, dict):
                url = raw.get("url")
                detail = raw.get("detail", detail)
            else:
                url = raw
            if url:
                parts.append(_media_part_from_url(str(url), detail))
        elif item_type == "input_file" and item.get("file_url"):
            parts.append(
                MediaPart(
                    media_type="file",
                    mime_type="application/octet-stream",
                    url=str(item.get("file_url")),
                )
            )

    flat_text = "\n".join(t for t in texts if t)
    has_media = any(isinstance(p, MediaPart) for p in parts)
    return flat_text, parts if has_media else None


def _canonicalize_tool_calls(tool_calls: Any) -> Optional[List[ToolCallRequest]]:
    """Convert OpenAI assistant tool_calls to canonical ToolCallRequest (ADR-0137)."""
    from motet.core.models.adapters.tool_call_codec import inbound_tool_call_request

    if not isinstance(tool_calls, list) or not tool_calls:
        return None
    out: List[ToolCallRequest] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = function.get("name") or call.get("name") or call.get("tool_name")
        if not name:
            continue
        args = function.get("arguments") if function else call.get("arguments")
        if args is None:
            args = call.get("arguments_json")
        if not isinstance(args, str):
            args = json.dumps(args or {})
        out.append(
            inbound_tool_call_request(
                call_id=str(call.get("id") or call.get("call_id") or new_call_id()),
                tool_name=str(name),
                arguments_json=args,
            )
        )
    return out or None


def _message_from_openai(raw: Dict[str, Any]) -> Optional[Message]:
    """Convert one OpenAI chat message to a canonical Message."""
    role = str(raw.get("role") or "user")
    text, parts = _content_to_parts(raw.get("content"))
    tool_calls = _canonicalize_tool_calls(raw.get("tool_calls"))

    if role == "developer":
        role = "system"
    if not text and not parts and not tool_calls and role != "assistant":
        return None

    return Message(
        role=role,
        content=text,
        content_parts=parts,
        name=raw.get("name"),
        tool_calls_canonical=tool_calls,
        tool_call_id=raw.get("tool_call_id"),
    )


def _messages_from_responses_input(raw_input: Any) -> List[Message]:
    """Convert a Responses API ``input`` value to canonical messages."""
    if isinstance(raw_input, str):
        return [Message(role="user", content=raw_input)]
    if not isinstance(raw_input, list):
        return []

    messages: List[Message] = []
    for item in raw_input:
        if isinstance(item, str):
            messages.append(Message(role="user", content=item))
            continue
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")
        if item_type == "function_call":
            from motet.core.models.adapters.tool_call_codec import inbound_tool_call_request

            args = item.get("arguments")
            if not isinstance(args, str):
                args = json.dumps(args or {})
            messages.append(
                Message(
                    role="assistant",
                    content="",
                    tool_calls_canonical=[
                        inbound_tool_call_request(
                            call_id=str(item.get("call_id") or item.get("id") or new_call_id()),
                            tool_name=str(item.get("name") or ""),
                            arguments_json=args,
                        )
                    ],
                )
            )
            continue
        if item_type == "function_call_output":
            output = item.get("output")
            messages.append(
                Message(
                    role="tool",
                    content=output if isinstance(output, str) else json.dumps(output),
                    tool_call_id=item.get("call_id"),
                )
            )
            continue

        message = _message_from_openai(item)
        if message is not None:
            messages.append(message)

    return messages


def messages_to_canonical(req: ChatCompletionRequest) -> List[Message]:
    """Build canonical messages from either request shape."""
    messages: List[Message] = []

    if req.instructions:
        messages.append(Message(role="system", content=req.instructions))

    if req.messages:
        for raw in req.messages:
            if not isinstance(raw, dict):
                continue
            message = _message_from_openai(raw)
            if message is not None:
                messages.append(message)
    elif req.input is not None:
        messages.extend(_messages_from_responses_input(req.input))

    if not messages:
        raise FacadeError(
            400,
            "at least one message is required",
            code="missing_messages",
            param="messages",
        )
    return messages


def tools_to_canonical(req: ChatCompletionRequest) -> Optional[List[CanonicalToolSchema]]:
    """Convert client-declared OpenAI tools to canonical tool schemas."""
    if req.tool_choice == "none" or not req.tools:
        return None

    schemas: List[CanonicalToolSchema] = []
    for tool in req.tools:
        if not isinstance(tool, dict):
            continue
        # Chat Completions nests under "function"; Responses inlines the fields.
        function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = function.get("name")
        if not name:
            continue
        parameters = function.get("parameters") or {"type": "object", "properties": {}}
        schemas.append(
            CanonicalToolSchema(
                name=tool_wire_to_canonical(str(name)),
                description=str(function.get("description") or ""),
                json_schema=parameters,
                strict=bool(function.get("strict") or False),
            )
        )
    return schemas or None


def output_contract_from_request(req: ChatCompletionRequest) -> Optional[OutputContract]:
    """Map response_format / text.format to a canonical OutputContract."""
    spec = req.response_format
    if not spec and isinstance(req.text, dict):
        spec = req.text.get("format")
    if not isinstance(spec, dict):
        return None

    kind = spec.get("type")
    if kind == "json_object":
        return OutputContract(format="json")
    if kind == "json_schema":
        schema_block = spec.get("json_schema") if isinstance(spec.get("json_schema"), dict) else spec
        return OutputContract(
            format="json",
            json_schema=schema_block.get("schema") or schema_block.get("json_schema"),
            strict=bool(schema_block.get("strict") or False),
        )
    return None


def _request_extras(req: ChatCompletionRequest) -> Dict[str, Any]:
    """Return undeclared wire fields (Cursor ``reasoning``, etc.)."""
    extra = getattr(req, "model_extra", None) or getattr(req, "__pydantic_extra__", None)
    return dict(extra) if isinstance(extra, dict) else {}


def parse_thinking_opt_in(req: ChatCompletionRequest) -> Optional[str]:
    """Return requested reasoning effort when the client opts into thinking.

    Opt-in signals (any one is enough):
    - top-level ``reasoning_effort``
    - Responses-shaped ``reasoning`` object (truthy / with ``effort``)
    - Motet extension ``motet_enable_thinking: true``

    Returns the canonical effort string, or ``None`` when the client did not ask.
    """
    extras = _request_extras(req)
    effort_raw = extras.get("reasoning_effort")
    reasoning = extras.get("reasoning")
    motet_enable = extras.get("motet_enable_thinking")

    opted_in = False
    if isinstance(effort_raw, str) and effort_raw.strip():
        opted_in = True
    elif motet_enable is True or (
        isinstance(motet_enable, str) and motet_enable.strip().lower() in {"1", "true", "yes"}
    ):
        opted_in = True
    elif reasoning is not None and reasoning is not False:
        # Cursor sends reasoning: {effort: "..."} or similar; an empty dict still
        # means "please reason" for Responses-shaped bodies.
        if isinstance(reasoning, dict):
            opted_in = True
            if effort_raw is None and isinstance(reasoning.get("effort"), str):
                effort_raw = reasoning["effort"]
        elif reasoning is True:
            opted_in = True

    if not opted_in:
        return None
    return normalize_reasoning_effort(effort_raw, default="medium")


def apply_thinking_settings(
    settings: Dict[str, Any],
    req: ChatCompletionRequest,
    *,
    spec: Any,
    force_thinking: bool = False,
    force_thinking_effort: Optional[str] = None,
) -> Dict[str, Any]:
    """Attach enable_thinking / reasoning_effort when opted in or forced.

    Client opt-in and facade ``force_thinking`` OR together; client effort wins
    when both apply. When thinking is requested but the resolved model lacks
    ``CAP_REASONING``, thinking stays off (honest degrade, no 400) and a single
    log line records it.
    """
    from ....core.models.specs import CAP_REASONING

    effort = parse_thinking_opt_in(req)
    if effort is None and force_thinking:
        effort = normalize_reasoning_effort(force_thinking_effort, default="medium")
    if effort is None:
        return settings

    capabilities = set(getattr(spec, "capabilities", None) or [])
    if CAP_REASONING not in capabilities:
        logger.info(
            "openai_compat_thinking_stripped_model_lacks_capability",
            model=getattr(spec, "name", None) or settings.get("model_name"),
            provider=settings.get("provider"),
            force_thinking=bool(force_thinking),
            note="Thinking requested but model lacks CAP_REASONING.",
        )
        return settings

    settings["enable_thinking"] = True
    settings["reasoning_effort"] = effort
    return settings


def model_settings_from_request(
    req: ChatCompletionRequest,
    *,
    provider: str,
    registry_key: str,
    spec: Any = None,
    force_thinking: bool = False,
    force_thinking_effort: Optional[str] = None,
) -> Dict[str, Any]:
    """Build model_settings for the inference command."""
    settings: Dict[str, Any] = {"provider": provider, "model_name": registry_key}

    if req.temperature is not None:
        settings["temperature"] = req.temperature
    max_tokens = req.effective_max_output_tokens()
    if max_tokens is not None:
        settings["max_tokens"] = max_tokens

    forwarded = {}
    for name in _FORWARDED_PARAMS:
        value = getattr(req, name, None)
        if value is not None:
            settings[name] = value
            forwarded[name] = value
    if forwarded:
        # Adapters do not honor all of these yet; log so the gap is visible
        # rather than silently swallowed.
        logger.debug(
            "openai_compat_forwarded_params",
            provider=provider,
            model=registry_key,
            params=sorted(forwarded),
        )
    if spec is not None:
        apply_thinking_settings(
            settings,
            req,
            spec=spec,
            force_thinking=force_thinking,
            force_thinking_effort=force_thinking_effort,
        )
    return settings


def capability_check(spec: Any, *, needs_tools: bool, needs_structured: bool) -> None:
    """Reject requests the resolved model cannot satisfy (ADR-0125 §8)."""
    from ....core.models.specs import CAP_JSON_MODE, CAP_STRUCTURED_OUTPUT, CAP_TOOL_USE

    capabilities = set(getattr(spec, "capabilities", None) or [])
    if needs_tools and CAP_TOOL_USE not in capabilities:
        raise FacadeError(
            400,
            f"model '{getattr(spec, 'name', 'unknown')}' does not support tool calling",
            code="unsupported_capability",
            param="tools",
        )
    if needs_structured and not ({CAP_STRUCTURED_OUTPUT, CAP_JSON_MODE} & capabilities):
        raise FacadeError(
            400,
            f"model '{getattr(spec, 'name', 'unknown')}' does not support structured output",
            code="unsupported_capability",
            param="response_format",
        )


# ---------------------------------------------------------------------------
# Outbound: canonical result -> OpenAI wire
# ---------------------------------------------------------------------------

_FINISH_REASONS = {"stop", "length", "tool_calls", "content_filter", "function_call"}


def finish_reason_from_result(result: Dict[str, Any], *, has_tool_calls: bool) -> str:
    """Map a command result to an OpenAI finish_reason.

    Tool-call turns must report ``tool_calls`` or clients will not run the tool
    loop, so that takes precedence over whatever the provider reported.
    """
    if has_tool_calls:
        return "tool_calls"

    raw = str(result.get("finish_reason") or "").strip()
    if raw in _FINISH_REASONS:
        return raw

    canonical = {
        "natural_stop": "stop",
        "length_limit": "length",
        "stop_sequence": "stop",
        "safety_filter": "content_filter",
        "error": "stop",
    }
    return canonical.get(raw, "stop")


def tool_calls_to_openai(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Render canonical tool calls in OpenAI Chat Completions form."""
    calls = result.get("tool_calls_canonical") or []
    out: List[Dict[str, Any]] = []
    for index, call in enumerate(calls):
        if isinstance(call, dict):
            call_id = call.get("call_id") or call.get("id")
            name = call.get("tool_name") or call.get("name")
            arguments = call.get("arguments_json") or call.get("arguments")
        else:
            call_id = getattr(call, "call_id", None)
            name = getattr(call, "tool_name", None)
            arguments = getattr(call, "arguments_json", None)
        if not name:
            continue
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments or {})
        out.append(
            {
                "index": index,
                "id": call_id or new_call_id(),
                "type": "function",
                "function": {"name": tool_canonical_to_wire(str(name)), "arguments": arguments},
            }
        )
    return out


def tool_call_delta_to_openai(
    frame: Dict[str, Any],
    indices: Dict[str, int],
) -> Dict[str, Any]:
    """Render one tool-call argument fragment as an OpenAI streaming delta.

    The wire identifies a call being assembled by its position in the response,
    so the first fragment carries the identity (index, id, name) and later ones
    carry arguments against the same index. *indices* is the per-response
    call_id → index map and is updated here.
    """
    call_id = str(frame.get("call_id") or "")
    fragment = str(frame.get("arguments_delta") or "")
    if frame.get("first") or call_id not in indices:
        index = indices.setdefault(call_id, len(indices))
        return {
            "index": index,
            "id": call_id,
            "type": "function",
            "function": {
                "name": tool_canonical_to_wire(str(frame.get("tool_name") or "")),
                "arguments": fragment,
            },
        }
    return {"index": indices[call_id], "function": {"arguments": fragment}}


def function_call_item(
    call_id: str,
    tool_name: str,
    *,
    arguments: str = "",
    status: str = "in_progress",
) -> Dict[str, Any]:
    """Render a Responses ``function_call`` output item.

    Shared by the streaming body and the final response snapshot so the item id
    a client sees while the call is being generated is the same one it sees in
    ``response.completed`` — otherwise reconciling the two would look like two
    separate calls.
    """
    return {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": tool_canonical_to_wire(str(tool_name or "")),
        "arguments": arguments,
        "status": status,
    }


def warn_abandoned_streamed_calls(
    final_calls: List[Dict[str, Any]],
    streamed_call_ids: Any,
    *,
    task_id: str = "",
) -> None:
    """Log streamed calls the turn never handed back.

    The progress rail can stop a turn whose last model response asked for client
    tools, which leaves a call on the wire that the terminal event disowns.
    Logged rather than repaired: the bytes are already sent.
    """
    abandoned = sorted(set(streamed_call_ids) - {str(c.get("id") or "") for c in final_calls})
    if abandoned:
        logger.warning(
            "openai_compat_streamed_tool_calls_abandoned",
            task_id=task_id,
            call_ids=abandoned,
        )


def tool_calls_not_yet_streamed(
    tool_calls: List[Dict[str, Any]],
    streamed_indices: Dict[str, int],
    *,
    task_id: str = "",
) -> List[Dict[str, Any]]:
    """Drop calls the client already assembled from streamed fragments.

    Chat Completions ``delta.tool_calls`` frames are increments, so a call sent
    as fragments and then repeated whole would leave the client with doubled
    arguments. Indices are rebased past the streamed ones to keep positions
    unique within the response.

    The Responses wire needs no equivalent: ``response.completed`` carries a
    snapshot of the whole output, so listing a streamed call there is correct.
    """
    if not streamed_indices:
        return list(tool_calls)

    kept: List[Dict[str, Any]] = []
    for call in tool_calls:
        if str(call.get("id") or "") in streamed_indices:
            continue
        kept.append({**call, "index": len(streamed_indices) + len(kept)})

    warn_abandoned_streamed_calls(tool_calls, streamed_indices.keys(), task_id=task_id)
    return kept


def usage_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    """Render token usage in OpenAI Chat Completions form."""
    prompt = int(result.get("prompt_tokens") or 0)
    completion = int(result.get("completion_tokens") or 0)
    total = int(result.get("total_tokens") or (prompt + completion))
    payload: Dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    reasoning = result.get("reasoning_tokens")
    cached = result.get("cache_read_tokens")
    if reasoning:
        payload["completion_tokens_details"] = {"reasoning_tokens": int(reasoning)}
    if cached:
        payload["prompt_tokens_details"] = {"cached_tokens": int(cached)}
    return payload


def completion_payload(
    result: Dict[str, Any],
    *,
    model_id: str,
    completion_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a chat.completion response body from a command result."""
    tool_calls = tool_calls_to_openai(result)
    message: Dict[str, Any] = {
        "role": "assistant",
        "content": result.get("content") or ("" if not tool_calls else None),
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    reasoning_content = result.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content:
        message["reasoning_content"] = reasoning_content

    return {
        "id": completion_id or new_completion_id(),
        "object": "chat.completion",
        "created": now_ts(),
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": finish_reason_from_result(result, has_tool_calls=bool(tool_calls)),
            }
        ],
        "usage": usage_payload(result),
    }


def completion_chunk(
    *,
    completion_id: str,
    model_id: str,
    delta: Dict[str, Any],
    finish_reason: Optional[str] = None,
    created: Optional[int] = None,
) -> Dict[str, Any]:
    """Build one chat.completion.chunk frame."""
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created or now_ts(),
        "model": model_id,
        "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish_reason}],
    }


def usage_chunk(
    *,
    completion_id: str,
    model_id: str,
    result: Dict[str, Any],
    created: Optional[int] = None,
) -> Dict[str, Any]:
    """Build the terminal usage-bearing chunk for stream_options.include_usage."""
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created or now_ts(),
        "model": model_id,
        "choices": [],
        "usage": usage_payload(result),
    }


def responses_payload(
    result: Dict[str, Any],
    *,
    model_id: str,
    response_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    status: str = "completed",
) -> Dict[str, Any]:
    """Build a Responses API response body from a command result."""
    output: List[Dict[str, Any]] = []
    reasoning_content = result.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content:
        # Summary text only — opaque provider blocks stay Motet-internal.
        output.append(
            {
                "type": "reasoning",
                "id": f"rs_{new_message_id().removeprefix('msg_')}",
                "status": "completed",
                "summary": [{"type": "summary_text", "text": reasoning_content}],
            }
        )
    content = result.get("content") or ""
    if content:
        output.append(
            {
                "type": "message",
                "id": new_message_id(),
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": content, "annotations": []}],
            }
        )

    for call in tool_calls_to_openai(result):
        # Names are already wire-format here; function_call_item is idempotent.
        output.append(
            function_call_item(
                call["id"],
                call["function"]["name"],
                arguments=call["function"]["arguments"],
                status="completed",
            )
        )

    usage = usage_payload(result)
    payload: Dict[str, Any] = {
        "id": response_id or new_response_id(),
        "object": "response",
        "created_at": now_ts(),
        "status": status,
        "model": model_id,
        "output": output,
        "output_text": content,
        "usage": {
            "input_tokens": usage["prompt_tokens"],
            "output_tokens": usage["completion_tokens"],
            "total_tokens": usage["total_tokens"],
        },
    }
    if conversation_id:
        payload["conversation"] = {"id": conversation_id}
    return payload


def model_card(provider: str, registry_key: str, spec: Any) -> Dict[str, Any]:
    """Render a registry entry as an OpenAI models list item."""
    created = None
    released_at = getattr(spec, "released_at", None)
    if released_at is not None:
        try:
            from datetime import datetime, time as _time, timezone

            created = int(
                datetime.combine(released_at, _time.min, tzinfo=timezone.utc).timestamp()
            )
        except Exception:  # pragma: no cover - defensive, released_at is a date
            created = None
    return {
        "id": facade_model_id(provider, registry_key),
        "object": "model",
        "created": created or 0,
        "owned_by": provider,
    }


__all__ = [
    "allowed_models",
    "apply_thinking_settings",
    "capability_check",
    "completion_chunk",
    "completion_payload",
    "facade_model_id",
    "finish_reason_from_result",
    "function_call_item",
    "messages_to_canonical",
    "model_card",
    "model_settings_from_request",
    "output_contract_from_request",
    "parse_thinking_opt_in",
    "resolve_model",
    "responses_payload",
    "tool_call_delta_to_openai",
    "tool_calls_not_yet_streamed",
    "tool_calls_to_openai",
    "tools_to_canonical",
    "usage_chunk",
    "usage_payload",
    "validate_supported",
    "warn_abandoned_streamed_calls",
]
