"""
Motet - Google Gemini generateContent Adapter

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Native Gemini adapter targeting the **generateContent** / **streamGenerateContent** API
    via the `google-genai` client. Translates canonical LLMRequest/LLMResponse and stream
    events to Gemini `Content`/`Part` models (systemInstruction, tools.functionDeclarations).

Notes:
    - Gemini has no native tool call IDs; we emit synthetic `gemini_fc_<part_index>` IDs
      and require tool results to be ordered consistently.
    - Consecutive canonical `role="tool"` messages are coalesced into one `user` turn
      with multiple `functionResponse` parts, sorted by synthetic call index.
    - Gemini 3+ thought signatures: each functionCall part carries a `thought_signature`
      that MUST be replayed with the call on later turns (400 otherwise). Captured as
      base64url on `ToolCallRequest.thought_signature` / `ToolCallCompleteEvent`, persisted
      in the canonical tool_call dict, and re-attached to the Part on replay.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple

import structlog

from ....types import (
    CanonicalToolSchema,
    ErrorEvent,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    LLMUsage,
    Message,
    StopEvent,
    StopReason,
    TextDeltaEvent,
    TextPart,
    ToolCallCompleteEvent,
    ToolCallRequest,
    UsageEvent,
)
from ..base import CapabilityDescriptor
from ...registry import get_model_spec
from ...specs import (
    CAP_JSON_MODE,
    CAP_REASONING,
    CAP_STREAM,
    CAP_SYSTEM_PROMPT,
    CAP_TOOL_USE,
    CAP_VISION,
)
from .message_history_sanitizer import sanitize_orphan_tool_call_messages
from ..tool_call_codec import inbound_tool_call_request, tool_calls_from_message

logger = structlog.get_logger(__name__)

_GEMINI_FC_ID_RE = re.compile(r"^gemini_fc_(\d+)$")


def _as_dict(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    md = getattr(obj, "model_dump", None)
    if callable(md):
        try:
            out = md(mode="json", by_alias=False)
            return out if isinstance(out, dict) else {}
        except TypeError:
            try:
                out = md()
                return out if isinstance(out, dict) else {}
            except Exception:
                return {}
    try:
        return vars(obj) if hasattr(obj, "__dict__") else {}
    except Exception:
        return {}


def _flatten_text_parts(m: Message) -> str:
    parts = getattr(m, "content_parts", None) or []
    if not parts:
        return m.content
    chunks: List[str] = []
    for part in parts:
        p_type = getattr(part, "type", None) if not isinstance(part, dict) else part.get("type")
        if p_type != "text":
            continue
        p_text = getattr(part, "text", None) if not isinstance(part, dict) else part.get("text")
        if isinstance(p_text, str) and p_text:
            chunks.append(p_text)
    return "\n\n".join(chunks) if chunks else m.content


def _decode_thought_signature(sig: Any, *, call_id: str) -> Optional[bytes]:
    """
    Decode a persisted thought_signature back to the bytes Gemini expects on the Part.

    Signatures are captured from ``model_dump(mode="json")``, which encodes bytes as
    base64url **without padding** — restore padding before decoding. A corrupt value
    is logged and dropped (the call is replayed unsigned) rather than breaking
    history rendering.
    """
    if not isinstance(sig, str) or not sig:
        return None
    try:
        return base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
    except Exception as e:
        logger.warning("gemini_thought_signature_decode_failed", tool_call_id=call_id, error=str(e))
        return None


def _synthetic_call_id(part_index: int) -> str:
    return f"gemini_fc_{part_index}"


def _call_id_sort_key(call_id: str) -> int:
    m = _GEMINI_FC_ID_RE.match(str(call_id or ""))
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return 10**9
    return 10**9


def _extract_system_instruction(messages: List[Message]) -> Tuple[Optional[str], List[Message]]:
    sys_chunks: List[str] = []
    remaining: List[Message] = []
    for m in messages:
        if m.role in {"system", "developer"}:
            if m.content:
                sys_chunks.append(m.content)
            for part in getattr(m, "content_parts", None) or []:
                if getattr(part, "type", None) == "text":
                    t = getattr(part, "text", None)
                    if isinstance(t, str) and t:
                        sys_chunks.append(t)
                elif isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                    sys_chunks.append(part["text"])
            continue
        remaining.append(m)
    if not sys_chunks:
        return None, remaining
    text = "\n\n".join(c for c in sys_chunks if str(c).strip())
    return (text if text.strip() else None), remaining


def _image_parts_to_gemini(
    *,
    m: Message,
    request_context: Any,
) -> List[Any]:
    """Build google.genai.types.Part for images (lazy import inside)."""
    enable = bool(getattr(request_context, "enable_multimodal", False))
    if not enable:
        return []

    from google.genai import types

    tenant_id = getattr(request_context, "tenant_id", None)
    principal_id = getattr(request_context, "principal_id", None)
    motet_id = getattr(request_context, "motet_id", None)
    if not tenant_id or not principal_id:
        return []

    import base64

    out: List[Any] = []
    for part in getattr(m, "content_parts", None) or []:
        p_type = getattr(part, "type", None) if not isinstance(part, dict) else part.get("type")
        if p_type != "media":
            continue
        media_type = getattr(part, "media_type", None) if not isinstance(part, dict) else part.get("media_type")
        if media_type != "image":
            continue
        content_type = (
            getattr(part, "mime_type", None)
            if not isinstance(part, dict)
            else (part.get("mime_type") or part.get("content_type"))
        )
        if not isinstance(content_type, str) or not content_type.startswith("image/"):
            continue

        b64_data = getattr(part, "base64_data", None) if not isinstance(part, dict) else part.get("base64_data")
        raw: Optional[bytes] = None
        if isinstance(b64_data, str) and b64_data:
            try:
                raw = base64.b64decode(b64_data)
            except Exception:
                raw = None
        if raw is None:
            artifact_id = getattr(part, "artifact_id", None) if not isinstance(part, dict) else part.get("artifact_id")
            if not artifact_id:
                continue
            from ....artifacts import get_artifact_store

            store = get_artifact_store()
            payload = store.get(
                str(artifact_id),
                tenant_id=str(tenant_id),
                principal_id=str(principal_id),
                motet_id=str(motet_id) if motet_id else None,
            )
            if not isinstance(payload, (bytes, bytearray)):
                continue
            raw = bytes(payload)

        if raw:
            out.append(types.Part.from_bytes(data=raw, mime_type=content_type))
    return out


def _tool_result_to_response_dict(text: str) -> Dict[str, Any]:
    s = (text or "").strip()
    if not s:
        return {"result": ""}
    try:
        parsed = json.loads(s)
        if isinstance(parsed, dict):
            return parsed
        return {"result": parsed}
    except json.JSONDecodeError:
        return {"result": text}


def _format_gemini_contents(
    *,
    messages: List[Message],
    request_context: Any,
) -> Tuple[Optional[Any], List[Any]]:
    """
    Returns (system_instruction str or None, list of google.genai.types.Content).
    """
    from google.genai import types

    sys_text, remaining = _extract_system_instruction(messages)
    out: List[Any] = []
    pending_tools: List[Message] = []

    def flush_tools() -> None:
        nonlocal pending_tools
        if not pending_tools:
            return
        pending_tools = sorted(pending_tools, key=lambda mm: _call_id_sort_key(str(getattr(mm, "tool_call_id", None) or "")))
        parts: List[Any] = []
        for tm in pending_tools:
            call_id = str(getattr(tm, "tool_call_id", None) or "")
            name = str(getattr(tm, "name", None) or "") or str((tm.metadata or {}).get("tool_name") or "")
            if not name:
                logger.warning("gemini_skip_tool_message_no_name", tool_call_id=call_id)
                continue
            parts.append(types.Part.from_function_response(name=name, response=_tool_result_to_response_dict(_flatten_text_parts(tm))))
        pending_tools = []
        if parts:
            out.append(types.Content(role="user", parts=parts))

    for m in remaining:
        if m.role == "tool":
            pending_tools.append(m)
            continue
        flush_tools()

        if m.role == "user":
            parts: List[Any] = []
            text = _flatten_text_parts(m)
            if text:
                parts.append(types.Part.from_text(text=text))
            parts.extend(_image_parts_to_gemini(m=m, request_context=request_context))
            if parts:
                out.append(types.Content(role="user", parts=parts))
            continue

        if m.role == "assistant":
            parts = []
            text = _flatten_text_parts(m)
            if text:
                parts.append(types.Part.from_text(text=text))
            for tc in tool_calls_from_message(m):
                args = tc.arguments if isinstance(tc.arguments, dict) else {}
                if not args and tc.arguments_json:
                    try:
                        parsed = json.loads(tc.arguments_json)
                        args = parsed if isinstance(parsed, dict) else {}
                    except Exception:
                        args = {}
                part = types.Part.from_function_call(name=tc.tool_name, args=args)
                sig_bytes = _decode_thought_signature(
                    tc.thought_signature, call_id=tc.call_id
                )
                if sig_bytes is not None:
                    part.thought_signature = sig_bytes
                parts.append(part)
            if parts:
                out.append(types.Content(role="model", parts=parts))
            continue

        logger.debug("gemini_skip_unknown_role", role=m.role)

    flush_tools()
    return sys_text, out


def _canonical_tools_to_gemini(tools: Optional[List[CanonicalToolSchema]]) -> Optional[List[Any]]:
    if not tools:
        return None
    from google.genai import types

    decls: List[Any] = []
    for t in tools:
        decls.append(
            types.FunctionDeclaration.model_validate(
                {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters_json_schema": t.json_schema,
                }
            )
        )
    return [types.Tool(function_declarations=decls)]


def _usage_from_raw(raw: Dict[str, Any]) -> Optional[LLMUsage]:
    um = raw.get("usage_metadata")
    if not isinstance(um, dict):
        return None
    pt = um.get("prompt_token_count")
    ct = um.get("candidates_token_count")
    total = um.get("total_token_count")
    cached = um.get("cached_content_token_count")
    thoughts = um.get("thoughts_token_count")
    try:
        tot = int(total) if isinstance(total, int) else None
        if tot is None and isinstance(pt, int) and isinstance(ct, int):
            tot = pt + ct
    except Exception:
        tot = None
    return LLMUsage(
        prompt_tokens=pt if isinstance(pt, int) else None,
        output_tokens=ct if isinstance(ct, int) else None,
        total_tokens=tot,
        cache_read_tokens=cached if isinstance(cached, int) else None,
        reasoning_tokens=thoughts if isinstance(thoughts, int) else None,
        provider_metadata=um,
    )


def _finish_to_stop(finish: Optional[str], *, has_tool_calls: bool) -> StopReason:
    if has_tool_calls:
        return StopReason.TOOL_CALLS
    f = str(finish or "").upper()
    mapping = {
        "STOP": StopReason.NATURAL_STOP,
        "MAX_TOKENS": StopReason.LENGTH_LIMIT,
        "SAFETY": StopReason.SAFETY_FILTER,
        "RECITATION": StopReason.SAFETY_FILTER,
        "BLOCKLIST": StopReason.SAFETY_FILTER,
        "PROHIBITED_CONTENT": StopReason.SAFETY_FILTER,
        "SPII": StopReason.SAFETY_FILTER,
        "MALFORMED_FUNCTION_CALL": StopReason.ERROR,
        "OTHER": StopReason.ERROR,
        "LANGUAGE": StopReason.NATURAL_STOP,
    }
    return mapping.get(f, StopReason.NATURAL_STOP)


def _parse_candidate_parts(parts: List[Any]) -> Tuple[str, List[ToolCallRequest]]:
    text_chunks: List[str] = []
    tools: List[ToolCallRequest] = []
    for idx, p in enumerate(parts or []):
        pd = _as_dict(p)
        tx = pd.get("text")
        if isinstance(tx, str) and tx:
            text_chunks.append(tx)
        fc = pd.get("function_call")
        if isinstance(fc, dict):
            name = str(fc.get("name") or "")
            args = fc.get("args")
            args_dict = args if isinstance(args, dict) else {}
            cid = _synthetic_call_id(idx)
            # Gemini 3+ binds a thought_signature to each functionCall part; it must be
            # replayed with the call on later turns or the API rejects the request
            # (400 "Function call is missing a thought_signature"). _as_dict uses
            # model_dump(mode="json"), so the bytes arrive base64url-encoded.
            sig = pd.get("thought_signature")
            tools.append(
                inbound_tool_call_request(
                    call_id=cid,
                    tool_name=name,
                    arguments_json=json.dumps(args_dict),
                    tool_call_index=idx,
                    thought_signature=sig if isinstance(sig, str) and sig else None,
                )
            )
    return "".join(text_chunks), tools


def _parse_response(raw: Dict[str, Any]) -> Tuple[str, List[ToolCallRequest], StopReason, Optional[LLMUsage]]:
    cands = raw.get("candidates") or []
    cand = cands[0] if cands else {}
    cd = _as_dict(cand)
    content = _as_dict(cd.get("content"))
    parts = content.get("parts") or []
    text, tool_calls = _parse_candidate_parts(parts)
    finish = cd.get("finish_reason")
    stop = _finish_to_stop(str(finish) if finish is not None else None, has_tool_calls=bool(tool_calls))
    return text, tool_calls, stop, _usage_from_raw(raw)


def _build_config(
    *,
    settings: Dict[str, Any],
    sys_instruction: Optional[str],
    gemini_tools: Optional[List[Any]],
    output_contract: Any,
) -> Any:
    from google.genai import types

    from ...output_limits import resolve_max_output_tokens

    max_out = resolve_max_output_tokens(
        settings,
        provider="gemini",
        model_name=settings.get("model_name") or settings.get("model"),
        fallback=None,
    )
    temp_raw = settings.get("temperature", 0.2)
    temperature = float(0.2 if temp_raw is None else temp_raw)

    cfg_kwargs: Dict[str, Any] = {
        "temperature": temperature,
    }
    if max_out is not None:
        cfg_kwargs["max_output_tokens"] = max_out
    if sys_instruction:
        cfg_kwargs["system_instruction"] = sys_instruction

    if gemini_tools:
        cfg_kwargs["tools"] = gemini_tools
        cfg_kwargs["tool_config"] = types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode=types.FunctionCallingConfigMode.AUTO)
        )

    if output_contract is not None and getattr(output_contract, "format", None) == "json":
        cfg_kwargs["response_mime_type"] = "application/json"
        schema = getattr(output_contract, "json_schema", None)
        if isinstance(schema, dict) and schema:
            cfg_kwargs["response_json_schema"] = schema

    return types.GenerateContentConfig(**cfg_kwargs)


@dataclass
class GeminiGenerateContentAdapter:
    provider: str
    adapter_name: str
    credentials: Optional[Dict[str, Any]] = None

    def capabilities(self, *, model: str) -> CapabilityDescriptor:
        spec = get_model_spec("gemini", model)
        caps = set(spec.capabilities) if spec else set()
        return CapabilityDescriptor(
            provider=self.provider,
            model=model,
            supports_streaming=CAP_STREAM in caps,
            supports_tools=CAP_TOOL_USE in caps,
            supports_parallel_tool_calls=CAP_TOOL_USE in caps,
            supports_tool_call_id=False,
            supports_vision=CAP_VISION in caps,
            supports_json_mode=CAP_JSON_MODE in caps,
            supports_json_schema_strict=CAP_JSON_MODE in caps,
            supports_stateful_sessions=False,
            supports_builtin_tools=bool(getattr(spec, "supported_builtin_tools", None)),
            supports_system_prompt=CAP_SYSTEM_PROMPT in caps if spec else True,
            supports_reasoning=(CAP_REASONING in caps) if spec else False,
            provider_metadata={"adapter": "gemini_generate_content"},
        )

    def _api_key(self) -> str:
        cred = self.credentials or {}
        key = cred.get("gemini_api_key") or cred.get("api_key")
        if not key:
            raise ValueError("Gemini API key missing (expected credentials.gemini_api_key or credentials.api_key)")
        return str(key)

    def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            from google import genai
        except Exception as exc:
            raise RuntimeError("google-genai package not available") from exc

        settings = request.model_settings or {}
        model_name = settings.get("model_name") or settings.get("model") or ""
        if not model_name:
            raise ValueError("LLMRequest.model_settings.model_name is required for Gemini adapter")

        safe_messages, sanitize_stats = sanitize_orphan_tool_call_messages(request.messages)
        if sanitize_stats["removed_assistant_calls"] > 0 or sanitize_stats["removed_tool_messages"] > 0:
            logger.warning(
                "provider_boundary_orphan_tool_calls_pruned",
                provider=self.provider,
                model=model_name,
                removed_assistant_calls=sanitize_stats["removed_assistant_calls"],
                removed_tool_messages=sanitize_stats["removed_tool_messages"],
            )

        sys_text, contents = _format_gemini_contents(messages=safe_messages, request_context=request.request_context)
        gemini_tools = _canonical_tools_to_gemini(request.tools)
        config = _build_config(
            settings=settings,
            sys_instruction=sys_text,
            gemini_tools=gemini_tools,
            output_contract=request.output_contract,
        )

        client = genai.Client(api_key=self._api_key())
        resp = client.models.generate_content(model=model_name, contents=contents, config=config)
        raw = _as_dict(resp)

        text, tool_calls, stop, usage = _parse_response(raw)
        output_items: List[Any] = []
        if text:
            output_items.append(TextPart(text=text))
        output_items.extend(tool_calls)

        return LLMResponse(
            output_text=text or None,
            output_items=output_items,
            stop_reason=stop,
            usage=usage,
            raw_provider_metadata={"raw": raw},
        )

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]:
        try:
            from google import genai
        except Exception as exc:
            yield ErrorEvent(error_type="ImportError", message=str(exc))
            yield StopEvent(reason=StopReason.ERROR)
            return

        settings = request.model_settings or {}
        model_name = settings.get("model_name") or settings.get("model") or ""
        if not model_name:
            yield ErrorEvent(error_type="ValueError", message="model_name required")
            yield StopEvent(reason=StopReason.ERROR)
            return

        try:
            safe_messages, sanitize_stats = sanitize_orphan_tool_call_messages(request.messages)
            if sanitize_stats["removed_assistant_calls"] > 0 or sanitize_stats["removed_tool_messages"] > 0:
                logger.warning(
                    "provider_boundary_orphan_tool_calls_pruned",
                    provider=self.provider,
                    model=model_name,
                    removed_assistant_calls=sanitize_stats["removed_assistant_calls"],
                    removed_tool_messages=sanitize_stats["removed_tool_messages"],
                )

            sys_text, contents = _format_gemini_contents(messages=safe_messages, request_context=request.request_context)
            gemini_tools = _canonical_tools_to_gemini(request.tools)
            config = _build_config(
                settings=settings,
                sys_instruction=sys_text,
                gemini_tools=gemini_tools,
                output_contract=request.output_contract,
            )

            client = genai.Client(api_key=self._api_key())
            accumulated = ""
            last_raw: Dict[str, Any] = {}

            for chunk in client.models.generate_content_stream(model=model_name, contents=contents, config=config):
                last_raw = _as_dict(chunk)
                cands = last_raw.get("candidates") or []
                cand = _as_dict(cands[0] if cands else {})
                content = _as_dict(cand.get("content"))
                parts = content.get("parts") or []
                texts: List[str] = []
                for p in parts:
                    pd = _as_dict(p)
                    tx = pd.get("text")
                    if isinstance(tx, str):
                        texts.append(tx)
                chunk_text = "".join(texts)
                if chunk_text:

                    def gen_deltas() -> Iterator[TextDeltaEvent]:
                        nonlocal accumulated
                        if not chunk_text:
                            return
                        if len(chunk_text) >= len(accumulated) and chunk_text.startswith(accumulated):
                            delta = chunk_text[len(accumulated) :]
                            accumulated = chunk_text
                            if delta:
                                yield TextDeltaEvent(text=delta)
                        else:
                            accumulated += chunk_text
                            yield TextDeltaEvent(text=chunk_text)

                    for ev in gen_deltas():
                        yield ev

            text, tool_calls, stop, usage = _parse_response(last_raw)
            for tc in tool_calls:
                yield ToolCallCompleteEvent(
                    call_id=tc.call_id,
                    tool_name=tc.tool_name,
                    arguments_json=tc.arguments_json,
                    kind=None,
                    thought_signature=tc.thought_signature,
                )
            if usage is not None:
                yield UsageEvent(usage=usage)
            yield StopEvent(reason=stop)
        except Exception as exc:
            logger.error("gemini_stream_failed", error=str(exc), error_type=type(exc).__name__, exc_info=True)
            yield ErrorEvent(error_type=type(exc).__name__, message=str(exc))
            yield StopEvent(reason=StopReason.ERROR)


__all__ = ["GeminiGenerateContentAdapter"]
