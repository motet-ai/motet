"""
Motet - Local Inference Reasoning / Tool-Call Parsing Helpers

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Pure, dependency-light helpers that bring the local provider path to canonical
    LLM-protocol parity without pulling in llama.cpp. They cover the
    fragile, family-specific parts of local inference so they can be unit-tested in
    CI without GGUF assets:

    - ``split_reasoning``: separate ``<think>...</think>`` and Gemma 4
    ``<|channel>thought...<channel|>`` reasoning from user-facing content for the
    non-streaming path.
    - ``ThinkStreamRouter``: a stateful streaming router that classifies token chunks
    as ``text`` vs ``thinking`` while tolerating ``<think>``/``</think>`` tags that
    are split across chunk boundaries.
    - ``ToolCallStreamGate``: a streaming gate that passes text through immediately
    and withholds only tool-call markup (sentinels or bare-JSON tool emissions)
    so tool-capable turns stream incrementally instead of buffering whole-turn.
    - ``map_finish_reason`` / ``map_usage``: map llama.cpp ``finish_reason`` /
    ``usage`` shapes onto canonical ``StopReason`` / ``LLMUsage``.
    - ``parse_tool_calls``: parse OpenAI-style ``message.tool_calls`` dicts (as
    emitted by llama-cpp-python's chat path) into canonical ``ToolCallRequest``.

Dependencies:
    - motet.core.types: canonical ``StopReason``, ``LLMUsage``, ``ToolCallRequest``.

Usage:
    from motet.core.models.local.reasoning import split_reasoning, ThinkStreamRouter
    clean, reasoning = split_reasoning(raw_text)

    router = ThinkStreamRouter()
    for channel, chunk in router.feed(token):
        ...  # channel is "text" or "thinking"

Notes:
    - These helpers never raise on malformed input; they degrade to treating content
      as plain text so an unexpected model output can never break the stream.
    - The manager (subprocess) only uses ``split_reasoning``; the adapter uses the
      rest. Keeping them together centralizes the family-specific parsing surface.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ...types import LLMUsage, StopReason, ToolCallRequest

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"
_GEMMA_THOUGHT_OPEN = "<|channel>thought"
_GEMMA_CHANNEL_CLOSE = "<channel|>"


def _split_reasoning_blocks(text: str, open_tag: str, close_tag: str) -> Tuple[str, List[str]]:
    """Split tagged reasoning blocks out of a complete model output."""
    if not text or open_tag not in text:
        return text or "", []

    clean_parts: List[str] = []
    reasoning_parts: List[str] = []
    rest = text
    while rest:
        idx = rest.find(open_tag)
        if idx == -1:
            clean_parts.append(rest)
            break
        clean_parts.append(rest[:idx])
        after = rest[idx + len(open_tag):]
        close = after.find(close_tag)
        if close == -1:
            reasoning_parts.append(after)
            break
        reasoning_parts.append(after[:close])
        rest = after[close + len(close_tag):]

    clean = "".join(clean_parts).strip()
    return clean, reasoning_parts


def split_reasoning(text: str) -> Tuple[str, Optional[str]]:
    """Split reasoning control blocks out of a complete model output.

    Returns ``(clean_text, reasoning_text_or_None)``. Multiple reasoning blocks
    are concatenated (newline-joined) into the reasoning result. An unclosed block
    treats everything after the open tag as reasoning. When no reasoning block is
    present the text is returned unchanged with ``None`` reasoning.
    """
    clean, think_parts = _split_reasoning_blocks(text, _THINK_OPEN, _THINK_CLOSE)
    clean, gemma_parts = _split_reasoning_blocks(
        clean, _GEMMA_THOUGHT_OPEN, _GEMMA_CHANNEL_CLOSE
    )
    reasoning = "\n".join(
        p.strip() for p in [*think_parts, *gemma_parts] if p.strip()
    ).strip()
    return clean, (reasoning or None)


def _partial_tag_suffix(data: str, tag: str) -> str:
    """Longest suffix of ``data`` that is a proper prefix of ``tag``.

    Used to hold back a few trailing characters that might be the start of a
    ``<think>``/``</think>`` tag split across the next chunk (e.g. ``"...<thi"``).
    """
    max_len = min(len(tag) - 1, len(data))
    for size in range(max_len, 0, -1):
        if data[-size:] == tag[:size]:
            return data[-size:]
    return ""


class ThinkStreamRouter:
    """Stateful router splitting a token stream into ``text`` / ``thinking`` runs.

    Feed incremental token chunks; each ``feed`` returns a list of
    ``(channel, chunk)`` tuples where ``channel`` is ``"text"`` or ``"thinking"``.
    Partial ``<think>``/``</think>`` tags spanning chunk boundaries are buffered
    internally and resolved once enough characters arrive. Call ``flush`` at end
    of stream to drain any buffered tail.
    """

    def __init__(self) -> None:
        self._in_think = False
        self._buf = ""

    def feed(self, chunk: str) -> List[Tuple[str, str]]:
        out: List[Tuple[str, str]] = []
        data = self._buf + (chunk or "")
        self._buf = ""
        while data:
            tag = _THINK_CLOSE if self._in_think else _THINK_OPEN
            idx = data.find(tag)
            if idx != -1:
                segment = data[:idx]
                if segment:
                    out.append(("thinking" if self._in_think else "text", segment))
                data = data[idx + len(tag):]
                self._in_think = not self._in_think
                continue

            partial = _partial_tag_suffix(data, tag)
            if partial:
                emit = data[: len(data) - len(partial)]
                if emit:
                    out.append(("thinking" if self._in_think else "text", emit))
                self._buf = partial
            elif data:
                out.append(("thinking" if self._in_think else "text", data))
            data = ""
        return out

    def flush(self) -> List[Tuple[str, str]]:
        """Drain any buffered partial-tag tail at end of stream."""
        out: List[Tuple[str, str]] = []
        if self._buf:
            out.append(("thinking" if self._in_think else "text", self._buf))
            self._buf = ""
        return out


# Text markers that begin a per-family tool-call emission. Anything from one of
# these onward is held back from the live stream so tool-call markup never leaks
# to the user; the held text is parsed at end of stream (ADR-0115).
_TOOL_TEXT_SENTINELS: Tuple[str, ...] = (
    "<tool_call>",      # Qwen / Hermes / ChatML
    "[TOOL_CALLS]",     # Mistral / Ministral
    "```json",          # Phi-4 fenced JSON tool-call block
    "```python",        # Phi-4/Gemma fenced Python tool-call block
    "```py",            # Python fence variant
    "```tool_call",     # Gemma fenced tool block (variant)
    "```tool_code",     # Gemma fenced tool block
    "<|python_tag|>",   # Llama 3.1 python-tag prefix
)


def _partial_sentinel_suffix(data: str) -> str:
    """Longest suffix of ``data`` that could begin one of the tool sentinels."""
    best = ""
    for sentinel in _TOOL_TEXT_SENTINELS:
        candidate = _partial_tag_suffix(data, sentinel)
        if len(candidate) > len(best):
            best = candidate
    return best


class ToolCallStreamGate:
    """Pass-through stream gate that withholds tool-call text markup.

    Used by the manager's streaming path when tools are requested. Unlike the
    previous whole-turn buffering (which suppressed all streaming and made slow
    local turns race the client timeout with zero output), this gate emits text
    immediately and holds back only:

    - everything from a tool sentinel (``<tool_call>``, ``[TOOL_CALLS]``,
      fenced JSON/Python/tool_code blocks, ``<|python_tag|>``) onward, and
    - the entire content when its first non-whitespace character is ``{`` or
      ``[`` (Llama 3.1 / phi-4 emit the tool call as a bare top-level JSON
      object/array — there is no inner sentinel to key on).

    Partial sentinels split across chunk boundaries are buffered until resolved.
    Call ``flush`` at end of stream: it returns ``(tail_text, held_text)`` where
    ``tail_text`` is safe to emit and ``held_text`` should be passed to
    ``extract_tool_calls_from_text`` (whose leftover clean text is then emitted).
    """

    def __init__(self) -> None:
        self._held: List[str] = []
        self._holding = False
        self._pending = ""
        self._at_start = True

    def feed(self, chunk: str) -> str:
        """Feed a token chunk; returns the portion safe to emit now."""
        if self._holding:
            self._held.append(chunk or "")
            return ""
        data = self._pending + (chunk or "")
        self._pending = ""
        if not data:
            return ""

        if self._at_start:
            stripped = data.lstrip()
            if not stripped:
                # Whitespace-only so far; defer the start-of-content decision.
                self._pending = data
                return ""
            if stripped[0] in "{[":
                self._holding = True
                self._held.append(data)
                return ""
            self._at_start = False

        # Hold from the earliest sentinel occurrence onward.
        earliest = -1
        for sentinel in _TOOL_TEXT_SENTINELS:
            idx = data.find(sentinel)
            if idx != -1 and (earliest == -1 or idx < earliest):
                earliest = idx
        if earliest != -1:
            self._holding = True
            self._held.append(data[earliest:])
            return data[:earliest]

        # Withhold a trailing partial sentinel until the next chunk resolves it.
        tail = _partial_sentinel_suffix(data)
        if tail:
            self._pending = tail
            return data[: len(data) - len(tail)]
        return data

    def flush(self) -> Tuple[str, str]:
        """End of stream: return ``(tail_text_to_emit, held_text_to_parse)``."""
        tail = "" if self._holding else self._pending
        held = "".join(self._held)
        if self._holding and self._pending:  # defensive; pending is unused while holding
            held += self._pending
        self._pending = ""
        self._held = []
        self._holding = False
        self._at_start = True
        return tail, held


# llama.cpp / OpenAI-style finish_reason -> canonical StopReason.
_FINISH_REASON_MAP = {
    "stop": StopReason.NATURAL_STOP,
    "length": StopReason.LENGTH_LIMIT,
    "tool_calls": StopReason.TOOL_CALLS,
    "function_call": StopReason.TOOL_CALLS,
    "content_filter": StopReason.SAFETY_FILTER,
}


def map_finish_reason(finish_reason: Optional[str], *, has_tool_calls: bool = False) -> StopReason:
    """Map a raw llama.cpp ``finish_reason`` onto the canonical ``StopReason``.

    Tool calls win regardless of the reported reason (some builds still report
    ``stop`` alongside emitted tool calls). Unknown values degrade to
    ``NATURAL_STOP``.
    """
    if has_tool_calls:
        return StopReason.TOOL_CALLS
    return _FINISH_REASON_MAP.get((finish_reason or "").lower(), StopReason.NATURAL_STOP)


def map_usage(raw_usage: Optional[Dict[str, Any]]) -> Optional[LLMUsage]:
    """Map a llama.cpp ``usage`` dict onto canonical ``LLMUsage``.

    llama.cpp reports ``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``.
    Returns ``None`` when no usable counts are present (graceful degradation).
    """
    if not raw_usage or not isinstance(raw_usage, dict):
        return None
    prompt = raw_usage.get("prompt_tokens")
    output = raw_usage.get("completion_tokens")
    total = raw_usage.get("total_tokens")
    if prompt is None and output is None and total is None:
        return None
    if total is None and (prompt is not None or output is not None):
        total = (prompt or 0) + (output or 0)
    return LLMUsage(
        prompt_tokens=prompt,
        output_tokens=output,
        total_tokens=total,
        provider_metadata=raw_usage,
    )


_TOOL_CALL_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_MISTRAL_TOOL_CALLS_RE = re.compile(r"\[TOOL_CALLS\]\s*(\[.*\]|\{.*\})", re.DOTALL)
# Gemma 4 emits documented function calls as control-token blocks, e.g.
# ``<|tool_call>call:get_weather{location:<|"|>London<|"|>}<tool_call|>``.
_GEMMA_CONTROL_TOOL_CALL_RE = re.compile(
    r"<\|tool_call\>\s*call:([A-Za-z_][A-Za-z0-9_.]*)\s*\{(.*?)\}\s*<tool_call\|>"
    r"(?:\s*<\|tool_response\>)?",
    re.DOTALL,
)
_GEMMA_CONTROL_ARG_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?:<\|\"\|>(.*?)<\|\"\|>|([^,}]*))",
    re.DOTALL,
)
# Gemma emits tool calls as a ```tool_code``` (sometimes ```tool_call```/```python```)
# block containing a Python expression, e.g. ``get_weather(city='SF')``.
_GEMMA_TOOL_CODE_RE = re.compile(r"```(?:tool_code|tool_call|python|py)\s*\n(.*?)```", re.DOTALL)
_BARE_PYTHON_CALL_RE = re.compile(
    r"\s*[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?\s*\(.*\)\s*",
    re.DOTALL,
)
_BARE_NAME_JSON_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\s*(\{.*\})\s*$",
    re.DOTALL,
)
# Markdown-fenced JSON tool call (small models often narrate then emit the call
# inside a ```json ... ``` block; the trailing fence breaks the bare-JSON parser).
# The fence body is captured non-greedily so nested braces are preserved.
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)
# Python builtins/utilities that may appear in a tool_code block but are not tool
# calls; skipped when no explicit tool-name allowlist is supplied.
_PY_CALL_DENYLIST = {"print", "len", "str", "int", "float", "list", "dict", "range"}


def _to_openai_tool_call(index: int, name: str, arguments: Any) -> Dict[str, Any]:
    """Build an OpenAI-style tool-call dict from a parsed name + arguments."""
    if isinstance(arguments, str):
        args_json = arguments
    else:
        try:
            args_json = json.dumps(arguments if arguments is not None else {})
        except (TypeError, ValueError):
            args_json = "{}"
    return {
        "id": f"call_{index}",
        "type": "function",
        "function": {"name": name, "arguments": args_json},
    }


def _tool_call_from_entry(
    entry: Any, index: int, tool_names: Optional[Set[str]] = None
) -> Optional[Dict[str, Any]]:
    """Convert a parsed JSON object with name/arguments into a tool-call dict."""
    if not isinstance(entry, dict) or not entry.get("name"):
        return None
    if "arguments" not in entry and "parameters" not in entry:
        return None
    name = str(entry["name"])
    canonical_name = _canonical_tool_name(name, tool_names)
    if canonical_name is None:
        return None
    return _to_openai_tool_call(
        index,
        canonical_name,
        entry.get("arguments", entry.get("parameters", {})),
    )


def _parse_python_call(node: "ast.Call", index: int) -> Optional[Dict[str, Any]]:
    """Convert an ``ast.Call`` (e.g. ``get_weather(city='SF')``) to a tool-call dict.

    Resolves the function name (plain ``Name`` or dotted ``Attribute``) and maps
    keyword arguments to a JSON arguments object via ``ast.literal_eval`` (with a
    source fallback for non-literal values). Positional args are indexed as
    ``arg0``/``arg1`` since the Python call site carries no parameter names.
    Returns ``None`` if the function name cannot be resolved.
    """
    func = node.func
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    else:
        return None
    if not name:
        return None

    arguments: Dict[str, Any] = {}
    for i, arg in enumerate(node.args):
        arguments[f"arg{i}"] = _literal_or_source(arg)
    for kw in node.keywords:
        if kw.arg is None:  # **kwargs splat — not representable
            continue
        arguments[kw.arg] = _literal_or_source(kw.value)
    return _to_openai_tool_call(index, name, arguments)


def _canonical_tool_name(name: str, tool_names: Optional[Set[str]]) -> Optional[str]:
    """Return canonical declared tool name, accepting case-insensitive variants.

    Small local models frequently drop the namespace prefix, emitting
    ``http_get_browser`` for the declared ``core.http_get_browser``. When no exact
    (case-insensitive) match exists, fall back to matching the segment after the
    last dot, but only when exactly one declared tool matches that suffix — an
    ambiguous suffix is rejected rather than guessed.
    """
    if tool_names is None:
        return None if name in _PY_CALL_DENYLIST else name
    lowered = name.lower()
    for declared in tool_names:
        if name == declared or lowered == declared.lower():
            return declared
    # Wire-format match: tool schemas are sanitized to the provider wire form
    # (canonical dots -> ``__``) before the model sees them, and small models echo
    # that sanitized name back (e.g. ``core__http_get_browser`` for the declared
    # ``core.http_get_browser``). Match against each declared tool's wire form.
    # NOTE: this transform must stay in sync with ``tool_canonical_to_wire``
    # (adapters/provider_builtin_tools.py): all dots become ``__``. It is inlined
    # here because importing the adapters package from this module would create a
    # cycle (adapters/__init__ imports providers/local.py, which imports this
    # module) and would pull the full adapter package into the dependency-light
    # inference subprocess.
    for declared in tool_names:
        wire = declared.replace(".", "__")
        if name == wire or lowered == wire.lower():
            return declared
    name_suffix = name.rsplit(".", 1)[-1].lower()
    suffix_matches = [
        declared
        for declared in tool_names
        if declared.rsplit(".", 1)[-1].lower() == name_suffix
    ]
    if len(suffix_matches) == 1:
        return suffix_matches[0]
    return None


def _literal_or_source(node: "ast.AST") -> Any:
    """Best-effort literal value for an AST node, falling back to its source."""
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        try:
            return ast.unparse(node)
        except Exception:  # pragma: no cover - extremely defensive
            return None


def _cast_gemma_control_value(value: str) -> Any:
    """Cast Gemma control-token argument strings into simple JSON values."""
    stripped = value.strip()
    if not stripped:
        return ""
    lowered = stripped.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        return float(stripped)
    except ValueError:
        return stripped.strip("'\"")


def _parse_gemma_control_arguments(arguments_text: str) -> Dict[str, Any]:
    """Parse Gemma 4 ``key:value`` control-token arguments."""
    arguments: Dict[str, Any] = {}
    for match in _GEMMA_CONTROL_ARG_RE.finditer(arguments_text):
        key = match.group(1)
        raw_value = match.group(2) if match.group(2) is not None else match.group(3)
        arguments[key] = _cast_gemma_control_value(raw_value or "")
    return arguments


def _extract_gemma_control_tool_calls(
    text: str, tool_names: Optional[Set[str]]
) -> Tuple[str, List[Dict[str, Any]]]:
    """Extract Gemma 4 control-token tool calls."""
    calls: List[Dict[str, Any]] = []

    def _block_sub(match: "re.Match[str]") -> str:
        canonical_name = _canonical_tool_name(match.group(1), tool_names)
        if canonical_name is None:
            return match.group(0)
        arguments = _parse_gemma_control_arguments(match.group(2))
        calls.append(_to_openai_tool_call(len(calls), canonical_name, arguments))
        return ""

    clean = _GEMMA_CONTROL_TOOL_CALL_RE.sub(_block_sub, text)
    return clean, calls


def _extract_gemma_tool_code(
    text: str, tool_names: Optional[Set[str]]
) -> Tuple[str, List[Dict[str, Any]]]:
    """Extract Gemma ``tool_code`` Python-block tool calls.

    Gemma emits tool calls as a fenced ``tool_code`` block containing Python that
    calls the tool (e.g. ``weather = get_weather(city='SF')``). Each ``ast.Call``
    whose function name is in ``tool_names`` (or, absent an allowlist, is not a
    common Python builtin) becomes a tool call. Matched blocks are removed from
    the returned text. Never raises.
    """
    calls: List[Dict[str, Any]] = []

    def _block_sub(match: "re.Match[str]") -> str:
        block = match.group(1)
        try:
            tree = ast.parse(block)
        except SyntaxError:
            return match.group(0)  # leave unparseable block untouched
        matched_any = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            candidate = _parse_python_call(node, len(calls))
            if candidate is None:
                continue
            fn_name = candidate["function"]["name"]
            canonical_name = _canonical_tool_name(fn_name, tool_names)
            if canonical_name is None:
                continue
            candidate["function"]["name"] = canonical_name
            calls.append(candidate)
            matched_any = True
        return "" if matched_any else match.group(0)

    clean = _GEMMA_TOOL_CODE_RE.sub(_block_sub, text)
    return clean, calls


def _extract_bare_python_call(
    text: str, tool_names: Optional[Set[str]]
) -> Tuple[str, List[Dict[str, Any]]]:
    """Extract a full-text Python-style tool call, e.g. ``GET_WEATHER(city="SF")``.

    Phi-4 sometimes emits an uppercase function-call expression after tool schema
    injection instead of the JSON array documented by its template. Restrict this
    recovery path to text that is entirely call-shaped so ordinary prose with
    parentheses is not misinterpreted as a tool call.
    """
    stripped = text.strip()
    if not _BARE_PYTHON_CALL_RE.fullmatch(stripped):
        return text, []
    try:
        tree = ast.parse(stripped)
    except SyntaxError:
        return text, []

    calls: List[Dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        candidate = _parse_python_call(node, len(calls))
        if candidate is None:
            continue
        fn_name = candidate["function"]["name"]
        canonical_name = _canonical_tool_name(fn_name, tool_names)
        if canonical_name is None:
            continue
        candidate["function"]["name"] = canonical_name
        calls.append(candidate)

    return ("", calls) if calls else (text, [])


def _extract_bare_name_json_call(
    text: str, tool_names: Optional[Set[str]]
) -> Tuple[str, List[Dict[str, Any]]]:
    """Extract ``tool_name{"arg": "value"}`` full-text calls.

    Ministral can emit the function name immediately followed by a JSON object
    instead of wrapping it in ``[TOOL_CALLS]``. Only accept declared tool names
    (when available) and a valid JSON object argument payload.
    """
    match = _BARE_NAME_JSON_RE.match(text)
    if not match:
        return text, []
    canonical_name = _canonical_tool_name(match.group(1), tool_names)
    if canonical_name is None:
        return text, []
    try:
        arguments = json.loads(match.group(2))
    except (ValueError, TypeError):
        return text, []
    if not isinstance(arguments, dict):
        return text, []
    return "", [_to_openai_tool_call(0, canonical_name, arguments)]


def _extract_json_tool_calls_from_payload(
    payload: Any, tool_names: Optional[Set[str]]
) -> List[Dict[str, Any]]:
    """Extract one or more tool calls from a parsed JSON object or array."""
    entries = payload if isinstance(payload, list) else [payload]
    calls: List[Dict[str, Any]] = []
    for entry in entries:
        call = _tool_call_from_entry(entry, len(calls), tool_names)
        if call is not None:
            calls.append(call)
    return calls


def _extract_fenced_json_tool_calls(
    text: str, tool_names: Optional[Set[str]]
) -> Tuple[str, List[Dict[str, Any]]]:
    """Extract tool calls emitted inside a Markdown ```json fence.

    phi-4 and other small models often narrate and then wrap the tool call in a
    ``\\`\\`\\`json ... \\`\\`\\``` block. The closing fence makes the bare/trailing
    JSON parser see trailing data and fail, so the call is otherwise lost. Parse
    each fence body and, when it is tool-call-shaped and names a declared tool,
    recover it and strip the fence. Non-JSON fences (e.g. Gemma ``tool_code``
    Python blocks) fail to parse and are left untouched for their own handlers.
    """
    calls: List[Dict[str, Any]] = []

    def _sub(match: "re.Match[str]") -> str:
        body = match.group(1).strip()
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            return match.group(0)
        recovered = _extract_json_tool_calls_from_payload(payload, tool_names)
        if not recovered:
            return match.group(0)
        calls.extend(recovered)
        return ""

    clean = _JSON_FENCE_RE.sub(_sub, text)
    return clean, calls


def _extract_trailing_json_tool_calls(
    text: str, tool_names: Optional[Set[str]]
) -> Tuple[str, List[Dict[str, Any]]]:
    """Extract a trailing JSON tool-call payload after optional prose.

    Phi-4 often emits a short lead-in sentence followed by a JSON array tool call
    (e.g. ``I will use the tool.\n\n[{...}]``). Treat only a syntactically valid
    trailing JSON object/array containing declared tool-call objects as a tool
    payload, leaving the lead-in prose as clean text.
    """
    stripped = text.rstrip()
    candidates = [idx for idx, char in enumerate(stripped) if char in "[{"]
    for start in candidates:
        prefix = stripped[:start].rstrip()
        suffix = stripped[start:].strip()
        try:
            payload = json.loads(suffix)
        except (ValueError, TypeError):
            continue
        calls = _extract_json_tool_calls_from_payload(payload, tool_names)
        if calls:
            return prefix, calls
    return text, []


def extract_tool_calls_from_text(
    text: str, tool_names: Optional[Iterable[str]] = None
) -> Tuple[str, List[Dict[str, Any]]]:
    """Extract tool calls a model emitted as text into OpenAI-style dicts.

    llama-cpp-python's embedded-Jinja chat path renders tool schemas into the
    prompt but does not always parse the model's tool-call output back into
    ``message.tool_calls`` (verified: Qwen3 emits ``<tool_call>{...}</tool_call>``
    into ``content``). This recovers tool calls from the common per-family text
    formats so native tool calling works regardless:

    - Qwen3 / Hermes / ChatML: ``<tool_call>{"name", "arguments"}</tool_call>``
      (one or more blocks).
    - Mistral / Ministral: ``[TOOL_CALLS][{"name", "arguments"}, ...]``.
      Some Ministral GGUFs emit ``tool_name{"arg": "value"}`` instead.
    - Llama 3.1 (best-effort): a bare top-level JSON object with ``name`` and
      ``arguments``/``parameters``.
    - phi-4: a bare top-level JSON *array* of ``{"name", "arguments"}`` objects,
      emitted after tools are injected into the system message (its template reads
      ``message['tools']``, never the native ``tools=`` channel — see ADR-0115 and
      the phi profile's ``apply_tool_schemas``), or a bare Python-style
      call such as ``GET_WEATHER(city="San Francisco")``. Some outputs include
      lead-in prose before a trailing JSON tool-call array.
    - Gemma 4: documented control-token calls, e.g.
      ``<|tool_call>call:get_weather{location:<|"|>London<|"|>}<tool_call|>``.
      Older/generic Gemma templates may emit a ```` ```tool_code ```` Python
      block calling the tool, e.g. ``get_weather(city='SF')`` (parsed via
      ``ast``).

    Small models also commonly (a) wrap the call in a Markdown ``\\`\\`\\`json`` or
    ``\\`\\`\\`python`` fence after a prose lead-in, (b) drop the namespace prefix
    (emit ``http_get_browser`` for the declared ``core.http_get_browser``), or
    (c) echo the provider wire-format name they were shown (``core__http_get_browser``
    for ``core.http_get_browser``). All are tolerated: fenced JSON tool calls are
    unwrapped, wire-format names are mapped back to their declared canonical name,
    and an unprefixed name is matched to a declared tool by its post-dot suffix
    when that match is unambiguous.

    ``tool_names`` is the set of declared tool names; when supplied it scopes the
    Python-block (Gemma) path to real tool calls (so ``print(...)`` and friends
    are ignored) and enables the unprefixed-name suffix match. Other formats are
    self-delimiting and ignore it.

    Returns ``(clean_text, tool_calls)`` where ``clean_text`` has the tool-call
    markup removed. Never raises; unparseable fragments are left in ``clean_text``.

    Not enabled (verified empirically):
    - ``gemma-3-4b``: the ``tool_code`` Python block above is one of *several*
      shapes it emits for the same task (also a freeform ```` ```tool ```` block,
      and occasionally native ``tool_calls`` with the wrong parameter name). The
      parser covers the documented Python-block form, but gemma-3-4b is too
      inconsistent to advertise ``CAP_TOOL_USE`` today; the parser stays dormant
      until tools are routed to a model whose family reliably uses this format.
    """
    if not text:
        return "", []

    names_set: Optional[Set[str]] = set(tool_names) if tool_names is not None else None
    tool_calls: List[Dict[str, Any]] = []

    # 1) <tool_call>...</tool_call> blocks (Qwen3 / Hermes / ChatML).
    def _tag_sub(match: "re.Match[str]") -> str:
        payload = match.group(1)
        try:
            obj = json.loads(payload)
        except (ValueError, TypeError):
            return match.group(0)  # leave malformed block untouched
        name = obj.get("name")
        if not name:
            return match.group(0)
        canonical_name = _canonical_tool_name(str(name), names_set)
        if canonical_name is None:
            return match.group(0)
        tool_calls.append(
            _to_openai_tool_call(
                len(tool_calls),
                canonical_name,
                obj.get("arguments", obj.get("parameters", {})),
            )
        )
        return ""

    clean = _TOOL_CALL_TAG_RE.sub(_tag_sub, text)

    # 2) Mistral [TOOL_CALLS] payload (array or single object).
    if not tool_calls:
        m = _MISTRAL_TOOL_CALLS_RE.search(clean)
        if m:
            try:
                payload = json.loads(m.group(1))
                entries = payload if isinstance(payload, list) else [payload]
                recovered: List[Dict[str, Any]] = []
                for entry in entries:
                    call = _tool_call_from_entry(entry, len(recovered), names_set)
                    if call is not None:
                        recovered.append(call)
                if recovered:
                    tool_calls.extend(recovered)
                    clean = clean[: m.start()] + clean[m.end():]
            except (ValueError, TypeError):
                pass

    # 3) Gemma 4 control-token call (e.g.
    #    ``<|tool_call>call:get_weather{location:<|"|>London<|"|>}<tool_call|>``).
    if not tool_calls and "<|tool_call>" in clean:
        clean, gemma_control_calls = _extract_gemma_control_tool_calls(clean, names_set)
        tool_calls.extend(gemma_control_calls)

    # 4) Gemma ```tool_code``` Python block (e.g. get_weather(city='SF')).
    if not tool_calls and "```" in clean:
        clean, gemma_calls = _extract_gemma_tool_code(clean, names_set)
        tool_calls.extend(gemma_calls)

    # 4b) Markdown ```json fenced JSON tool call (phi-4 and other small models
    #     narrate then wrap the call in a fence; the trailing ``` breaks the bare
    #     JSON parser below). Only fence bodies that parse to declared tool calls
    #     are consumed; other fences are left for their own handlers.
    if not tool_calls and "```" in clean:
        clean, fenced_calls = _extract_fenced_json_tool_calls(clean, names_set)
        tool_calls.extend(fenced_calls)

    # 5) Bare tool-name + JSON object (observed from Ministral, e.g.
    #    ``get_weather{"city": "San Francisco"}``).
    if not tool_calls:
        clean, bare_json_calls = _extract_bare_name_json_call(clean, names_set)
        tool_calls.extend(bare_json_calls)

    # 6) Bare Python-style full-text call (observed from phi-4, e.g.
    #    ``GET_WEATHER(city="San Francisco")``).
    if not tool_calls:
        clean, bare_calls = _extract_bare_python_call(clean, names_set)
        tool_calls.extend(bare_calls)

    # 7) Bare/trailing JSON tool call(s): a single object (Llama 3.1) or an array
    #    of them (phi-4 Path A emits ``[{"name", "arguments"}, ...]``), possibly
    #    after a short prose lead-in.
    if not tool_calls:
        clean, json_calls = _extract_trailing_json_tool_calls(
            clean.strip().removeprefix("<|python_tag|>").strip(),
            names_set,
        )
        tool_calls.extend(json_calls)

    return clean.strip(), tool_calls


def _payload_is_tool_call_shaped(payload: Any) -> bool:
    """True if a parsed JSON payload is a tool-call object (or array of them).

    Tool-call shape is a dict carrying a ``name`` plus ``arguments``/``parameters``
    (or a non-empty list where *every* element has that shape). This is the same
    shape ``_tool_call_from_entry`` accepts, minus the declared-name match.
    """
    entries = payload if isinstance(payload, list) else [payload]
    if not entries:
        return False
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("name"):
            return False
        if "arguments" not in entry and "parameters" not in entry:
            return False
    return True


def looks_like_unmatched_tool_call_markup(text: str) -> bool:
    """True if ``text`` is tool-call-shaped JSON that names no declared tool.

    Small local models can imitate the example invocations embedded in tool
    descriptions, emitting e.g. ``[{"name": "core.agent_turn", "arguments": {}}]``
    for a trivial prompt. ``extract_tool_calls_from_text`` correctly refuses to
    treat those as real tool calls (the names don't match anything declared), but
    that leaves the raw JSON as "leftover" text which must not be shown to the
    user verbatim. This detector lets callers suppress that leak.

    Only call this when tool-call recovery returned *no* tool calls; a positive
    result means "tool-call-shaped but unrecognized", not "valid tool call".
    Genuine bare-JSON answers (e.g. ``{"answer": 42}``) are not tool-call-shaped
    and return False, so they are still surfaced as text.
    """
    if not text:
        return False
    stripped = text.strip().removeprefix("<|python_tag|>").strip()
    for match in _TOOL_CALL_TAG_RE.finditer(stripped):
        try:
            payload = json.loads(match.group(1))
        except (ValueError, TypeError):
            continue
        if _payload_is_tool_call_shaped(payload):
            return True
    mistral_match = _MISTRAL_TOOL_CALLS_RE.search(stripped)
    if mistral_match:
        try:
            payload = json.loads(mistral_match.group(1))
        except (ValueError, TypeError):
            payload = None
        if _payload_is_tool_call_shaped(payload):
            return True
    s = stripped.rstrip()
    for start in (idx for idx, ch in enumerate(s) if ch in "[{"):
        suffix = s[start:].strip()
        try:
            payload = json.loads(suffix)
        except (ValueError, TypeError):
            continue
        if _payload_is_tool_call_shaped(payload):
            return True
    return False


def parse_tool_calls(raw_tool_calls: Optional[List[Dict[str, Any]]]) -> List[ToolCallRequest]:
    """Parse OpenAI-style ``message.tool_calls`` dicts into canonical requests.

    Accepts the shape llama-cpp-python emits from its chat path::

        [{"id": "call_0", "type": "function",
          "function": {"name": "get_weather", "arguments": "{\\"city\\": \\"NYC\\"}"}}]

    ``arguments`` may be a JSON string (most common) or an already-parsed
    dict/list. Entries without a tool name are skipped. Never raises.
    """
    out: List[ToolCallRequest] = []
    if not raw_tool_calls or not isinstance(raw_tool_calls, list):
        return out
    for i, tc in enumerate(raw_tool_calls):
        if not isinstance(tc, dict):
            continue
        raw_fn = tc.get("function")
        fn = raw_fn if isinstance(raw_fn, dict) else {}
        name = str(fn.get("name") or "")
        if not name:
            continue
        args = fn.get("arguments")
        parsed: Optional[Dict[str, Any]] = None
        if isinstance(args, (dict, list)):
            args_json = json.dumps(args)
            parsed = args if isinstance(args, dict) else None
        else:
            args_json = str(args or "{}")
            try:
                candidate = json.loads(args_json)
                parsed = candidate if isinstance(candidate, dict) else None
            except (ValueError, TypeError):
                parsed = None
        call_id = str(tc.get("id") or f"call_{i}")
        out.append(
            ToolCallRequest(
                call_id=call_id,
                tool_name=name,
                arguments_json=args_json,
                arguments=parsed,
                tool_call_index=i,
            )
        )
    return out


__all__ = [
    "split_reasoning",
    "ThinkStreamRouter",
    "ToolCallStreamGate",
    "map_finish_reason",
    "map_usage",
    "parse_tool_calls",
    "extract_tool_calls_from_text",
]
