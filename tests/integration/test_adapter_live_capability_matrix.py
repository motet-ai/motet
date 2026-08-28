"""
Motet - Live Adapter Capability Matrix (ADR-0064)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Opt-in live API suite that exercises Motet adapters against real provider
    endpoints and asserts ADR-0064 / ADR-0137 canonical shapes:

    - text complete → LLMResponse
    - stream → canonical event grammar (ends with StopEvent)
    - tools → two-round loop with an MCP-named tool (mcp.test.add_two_numbers):
      model.py wire-name convert outbound, inbound ToolCallRequest is canonical
      mcp.*, then history replay via tool_calls_canonical (thought_signature /
      reasoning_blocks regression) (CAP_TOOL_USE)
    - thinking → reasoning_content and/or ThinkingEvent (CAP_REASONING)
    - reasoning effort → canonical `max` is accepted by every provider after adapter
      clamping (CAP_REASONING)
    - vision → model acknowledges a small solid PNG (CAP_VISION)
    - prompt cache hit → identical tools+system prefix yields cache_read_tokens > 0
      on the second call (ADR-0124 / CAP_PROMPT_CACHING)
    - native web_search → URL-bearing citations when ModelSpec lists a
      ``*.web_search`` builtin (OpenAI / Anthropic / Grok / DeepSeek); those
      citations must also serialize through ``_citations_payload`` (the
      ``model_inference`` result shape ``core.web_search`` reads). Mixing the
      builtin with a Motet function tool must not 400.

    Safety / spend control:
      export MOTET_LIVE_ADAPTER_MATRIX=1
      export DEEPSEEK_API_KEY=...   # (and/or other provider keys)

    Defaults to one canary per provider: newest ModelSpec.released_at month,
    then cheapest input price among stream+tool_use models (aliases skipped).
    Optional case filter:
      export MOTET_LIVE_ADAPTER_CASES=deepseek:deepseek-v4-pro,openai:gpt-5.5

Dependencies:
    - pytest
    - Provider SDKs (openai / anthropic / google-genai as needed)
    - tests.fixtures.live_adapter_matrix / canonical_adapter_contract

Usage:
    # Fast local (keys required; no Docker stack needed for adapter-only calls):
    MOTET_LIVE_ADAPTER_MATRIX=1 DEEPSEEK_API_KEY=... \\
      pytest tests/integration/test_adapter_live_capability_matrix.py -v

    # In Docker test-runner (AGENTS.md integration path):
    docker-compose -f tests/docker-compose.test.yml run --rm \\
      -e MOTET_LIVE_ADAPTER_MATRIX=1 -e DEEPSEEK_API_KEY -e OPENAI_API_KEY \\
      test-runner python -m pytest -q \\
      tests/integration/test_adapter_live_capability_matrix.py -v
"""

from __future__ import annotations

import time
from typing import Any, List, Optional
from uuid import uuid4

import pytest

from motet.core.commands.builtin.model import (
    _apply_wire_names,
    _apply_wire_names_to_messages,
    _citations_payload,
)
from motet.core.models.adapters import adapter_registry
from motet.core.models.adapters.provider_builtin_tools import (
    get_unified_web_search_schema,
    tool_canonical_to_wire,
)
from motet.core.models.specs import (
    CAP_PROMPT_CACHING,
    CAP_REASONING,
    CAP_STREAM,
    CAP_TOOL_USE,
    CAP_VISION,
)
from motet.core.types import (
    CanonicalToolSchema,
    LLMRequest,
    LLMUsage,
    MediaPart,
    Message,
    RequestContext,
    StopEvent,
    TextDeltaEvent,
    ThinkingEvent,
    ToolCallRequest,
)
from tests.fixtures.canonical_adapter_contract import (
    assert_canonical_llm_response,
    assert_canonical_stream_events,
    collect_stream,
)
from tests.fixtures.live_adapter_matrix import (
    LIVE_TINY_PNG_B64,
    LiveAdapterCase,
    iter_live_cases,
    live_cacheable_system_text,
    live_matrix_enabled,
    resolve_credentials,
)


pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.live_llm]


def _require_live_opt_in() -> None:
    if not live_matrix_enabled():
        pytest.skip(
            "Live adapter matrix disabled. Set MOTET_LIVE_ADAPTER_MATRIX=1 "
            "and provider API keys to run (incurs provider spend)."
        )


_LIVE_CASES: List[LiveAdapterCase] = iter_live_cases()


def _case_id(case: LiveAdapterCase) -> str:
    return f"{case.provider}/{case.model}/{case.adapter_name}"


def _build_adapter(case: LiveAdapterCase) -> Any:
    creds = resolve_credentials(case.provider)
    if not creds:
        pytest.skip(f"No API key configured for provider={case.provider}")
    return adapter_registry.build(
        case.provider,
        case.adapter_name,
        credentials=creds,
    )


def _api_model_name(case: LiveAdapterCase) -> str:
    """Wire model id: prefer ModelSpec.name so registry aliases resolve (e.g. claude-sonnet-4.5)."""
    return case.spec.name or case.model


def _always_on_thinking(case: LiveAdapterCase) -> bool:
    """Providers/models that stream reasoning even when enable_thinking is False."""
    if case.provider == "xai":
        return True
    if case.provider == "meta":
        return True
    if case.provider == "moonshot" and case.model.lower() == "kimi-k3":
        return True
    return False


def _base_settings(case: LiveAdapterCase, **extra: Any) -> dict[str, Any]:
    # Reasoning models (Kimi K3, etc.) may spend the budget on CoT before content.
    default_max = 512 if CAP_REASONING in case.spec.capabilities else 128
    settings: dict[str, Any] = {
        "provider": case.provider,
        "model_name": _api_model_name(case),
        "temperature": 0.0,
        "max_tokens": default_max,
    }
    settings.update(extra)
    return settings


@pytest.mark.parametrize("case", _LIVE_CASES, ids=[_case_id(c) for c in _LIVE_CASES])
def test_live_text_complete_canonical(case: LiveAdapterCase) -> None:
    _require_live_opt_in()
    adapter = _build_adapter(case)

    # Keep headroom for always-on / CAP_REASONING CoT so content is not truncated.
    text_max = 256 if (_always_on_thinking(case) or CAP_REASONING in case.spec.capabilities) else 32
    resp = adapter.complete(
        LLMRequest(
            messages=[
                Message(
                    role="user",
                    content="Reply with exactly the single word: pong",
                )
            ],
            model_settings=_base_settings(case, max_tokens=text_max),
        )
    )
    assert_canonical_llm_response(resp)
    assert resp.output_text is not None
    assert "pong" in resp.output_text.strip().lower()


@pytest.mark.parametrize("case", _LIVE_CASES, ids=[_case_id(c) for c in _LIVE_CASES])
def test_live_stream_canonical_events(case: LiveAdapterCase) -> None:
    _require_live_opt_in()
    if CAP_STREAM not in case.spec.capabilities:
        pytest.skip("model does not advertise CAP_STREAM")

    adapter = _build_adapter(case)
    # Always-on reasoning hosts (Kimi K3, xAI) still emit ThinkingEvent.
    allow_thinking = _always_on_thinking(case)
    events = collect_stream(
        adapter.stream(
            LLMRequest(
                messages=[
                    Message(
                        role="user",
                        content="Reply with exactly the single word: pong",
                    )
                ],
                model_settings=_base_settings(
                    case,
                    max_tokens=256 if allow_thinking else 64,
                    enable_thinking=False,
                ),
            )
        )
    )
    assert_canonical_stream_events(events, allow_thinking=allow_thinking)
    assert any(isinstance(ev, TextDeltaEvent) for ev in events)
    assert isinstance(events[-1], StopEvent)
    text = "".join(ev.text for ev in events if isinstance(ev, TextDeltaEvent))
    assert "pong" in text.strip().lower()


@pytest.mark.parametrize("case", _LIVE_CASES, ids=[_case_id(c) for c in _LIVE_CASES])
def test_live_tool_call_canonical(case: LiveAdapterCase) -> None:
    _require_live_opt_in()
    if CAP_TOOL_USE not in case.spec.capabilities:
        pytest.skip("model does not advertise CAP_TOOL_USE")

    adapter = _build_adapter(case)
    # MCP-style dotted name: adapters must not see dots. model.py converts
    # outbound; inbound_tool_call_request converts back (ADR-0137).
    canonical_tool_name = "mcp.test.add_two_numbers"
    wire_tool_name = tool_canonical_to_wire(canonical_tool_name)
    tools = _apply_wire_names(
        [
            CanonicalToolSchema(
                name=canonical_tool_name,
                description="Add two integers and return their sum.",
                json_schema={
                    "type": "object",
                    "properties": {
                        "a": {"type": "integer"},
                        "b": {"type": "integer"},
                    },
                    "required": ["a", "b"],
                },
            )
        ]
    )
    prompt = (
        f"Use the {wire_tool_name} tool to compute 7 + 5. "
        "Do not answer without calling the tool."
    )
    resp = adapter.complete(
        LLMRequest(
            messages=[Message(role="user", content=prompt)],
            tools=tools,
            model_settings=_base_settings(case, max_tokens=256, enable_thinking=False),
        )
    )
    assert_canonical_llm_response(resp)
    tool_calls = [item for item in resp.output_items if isinstance(item, ToolCallRequest)]
    assert tool_calls, f"{case.provider}/{case.model} returned no ToolCallRequest"
    assert any(tc.tool_name == canonical_tool_name for tc in tool_calls), (
        f"{case.provider}/{case.model} inbound name was not canonical "
        f"{canonical_tool_name!r}: {[tc.tool_name for tc in tool_calls]}"
    )
    for tc in tool_calls:
        assert tc.call_id
        assert isinstance(tc.arguments_json, str)

    # Round 2: replay the assistant tool-call turn + tool result, exactly as the
    # agentic loop persists it (tool_calls_canonical). model.py then wires
    # names before the adapter. This is the regression net for provider replay
    # requirements: Gemini 3+ rejects functionCall parts without their
    # thought_signature, and reasoning_content/reasoning_blocks must round-trip
    # without breaking history rendering.
    followup = _apply_wire_names_to_messages(
        [
            Message(role="user", content=prompt),
            Message(
                role="assistant",
                content=resp.output_text or "",
                tool_calls_canonical=tool_calls,
                reasoning_content=getattr(resp, "reasoning_content", None),
                reasoning_blocks=getattr(resp, "reasoning_blocks", None),
            ),
        ]
        + [
            Message(
                role="tool",
                name=tc.tool_name,
                content='{"sum": 12}',
                tool_call_id=tc.call_id,
            )
            for tc in tool_calls
        ]
    )
    resp2 = adapter.complete(
        LLMRequest(
            messages=followup,
            tools=tools,
            model_settings=_base_settings(case, max_tokens=512, enable_thinking=False),
        )
    )
    assert_canonical_llm_response(resp2)
    assert resp2.output_text, f"{case.provider}/{case.model} returned empty text after tool result replay"
    assert "12" in resp2.output_text


@pytest.mark.parametrize("case", _LIVE_CASES, ids=[_case_id(c) for c in _LIVE_CASES])
def test_live_thinking_surfaces_reasoning(case: LiveAdapterCase) -> None:
    _require_live_opt_in()
    if CAP_REASONING not in case.spec.capabilities:
        pytest.skip("model does not advertise CAP_REASONING")

    adapter = _build_adapter(case)
    settings = _base_settings(
        case,
        max_tokens=512,
        enable_thinking=True,
        reasoning_effort="high",
    )

    # Prefer streaming so we can assert ThinkingEvent when the provider streams it.
    if CAP_STREAM in case.spec.capabilities:
        events = collect_stream(
            adapter.stream(
                LLMRequest(
                    messages=[
                        Message(
                            role="user",
                            content=(
                                "What is 17 * 19? Think step by step, then give the final number."
                            ),
                        )
                    ],
                    model_settings=settings,
                )
            )
        )
        assert_canonical_stream_events(events, allow_thinking=True)
        thinking = [ev for ev in events if isinstance(ev, ThinkingEvent)]
        text = "".join(ev.text for ev in events if isinstance(ev, TextDeltaEvent))
        # Accept either streamed thinking events or a non-empty final answer that
        # proves the call succeeded with thinking enabled (some hosts fold CoT).
        assert thinking or text.strip(), (
            f"{case.provider}/{case.model}: no ThinkingEvent and no text with thinking on"
        )
        return

    resp = adapter.complete(
        LLMRequest(
            messages=[
                Message(
                    role="user",
                    content="What is 17 * 19? Think step by step, then give the final number.",
                )
            ],
            model_settings=settings,
        )
    )
    assert_canonical_llm_response(resp)
    assert resp.reasoning_content or (resp.output_text and resp.output_text.strip())


@pytest.mark.parametrize("case", _LIVE_CASES, ids=[_case_id(c) for c in _LIVE_CASES])
def test_live_top_of_reasoning_effort_ladder_is_accepted(case: LiveAdapterCase) -> None:
    """
    The canonical top rung must never 400, whatever the provider's own vocabulary is.

    Providers disagree about the top of the ladder (xAI rejects `max`, DeepSeek has no
    rung below `high`), so adapters clamp instead of forwarding. This is the live net
    for that mapping: a request at `max` should come back as a normal answer.
    """
    _require_live_opt_in()
    if CAP_REASONING not in case.spec.capabilities:
        pytest.skip("model does not advertise CAP_REASONING")

    adapter = _build_adapter(case)
    resp = adapter.complete(
        LLMRequest(
            messages=[Message(role="user", content="What is 12 + 30? Answer with the number only.")],
            model_settings=_base_settings(
                case,
                max_tokens=2048,
                enable_thinking=True,
                reasoning_effort="max",
            ),
        )
    )
    assert_canonical_llm_response(resp)
    assert resp.reasoning_content or (resp.output_text and resp.output_text.strip()), (
        f"{case.provider}/{case.model} returned nothing at reasoning_effort=max"
    )


@pytest.mark.parametrize("case", _LIVE_CASES, ids=[_case_id(c) for c in _LIVE_CASES])
def test_live_vision_image_input(case: LiveAdapterCase) -> None:
    _require_live_opt_in()
    if CAP_VISION not in case.spec.capabilities:
        pytest.skip("model does not advertise CAP_VISION")

    adapter = _build_adapter(case)
    req = LLMRequest(
        messages=[
            Message(
                role="user",
                content="Describe this image in one short sentence.",
                content_parts=[
                    MediaPart(
                        media_type="image",
                        mime_type="image/png",
                        base64_data=LIVE_TINY_PNG_B64,
                    ),
                ],
            )
        ],
        # Always-on CoT (Kimi K3) can spend 250+ tokens reasoning about an image
        # before any visible text; 512 keeps headroom.
        model_settings=_base_settings(
            case,
            max_tokens=512 if (_always_on_thinking(case) or CAP_REASONING in case.spec.capabilities) else 64,
            enable_thinking=False,
        ),
        request_context=RequestContext(
            enable_multimodal=True,
            tenant_id="live-adapter-matrix",
            principal_id="live-adapter-matrix",
        ),
    )
    resp = adapter.complete(req)
    assert_canonical_llm_response(resp)
    assert resp.output_text and resp.output_text.strip(), (
        f"{case.provider}/{case.model} returned empty vision response"
    )


def _cache_probe_tools() -> List[CanonicalToolSchema]:
    """Stable tool schemas (byte-identical across calls) for tools-prefix caching."""
    long_desc = (
        "Probe tool for Motet live prompt-cache tests. Arguments are ignored; "
        "the schema exists so the tools segment of the provider prefix is large "
        "and stable across turns (ADR-0124 / ADR-0074 Rule 15). "
    ) * 8
    return [
        CanonicalToolSchema(
            name="cache_probe_alpha",
            description=long_desc + "Variant alpha.",
            json_schema={
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "Unused probe field."},
                },
                "required": [],
            },
        ),
        CanonicalToolSchema(
            name="cache_probe_beta",
            description=long_desc + "Variant beta.",
            json_schema={
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "Unused probe field."},
                },
                "required": [],
            },
        ),
    ]


def _usage_cache_read(usage: Optional[LLMUsage]) -> int:
    if usage is None:
        return 0
    return int(usage.cache_read_tokens or 0)


def _has_native_web_search(case: LiveAdapterCase) -> bool:
    tools = getattr(case.spec, "supported_builtin_tools", None) or []
    return any("web_search" in str(t) for t in tools)


def _citation_urls(resp: Any) -> List[str]:
    urls: List[str] = []
    for citation in getattr(resp, "citations", None) or []:
        url = getattr(citation, "url", None)
        if url and str(url).strip():
            urls.append(str(url).strip())
    return urls


@pytest.mark.timeout(180)
@pytest.mark.parametrize("case", _LIVE_CASES, ids=[_case_id(c) for c in _LIVE_CASES])
def test_live_native_web_search_returns_urls(case: LiveAdapterCase) -> None:
    """Provider-executed web_search must surface at least one citation URL."""
    _require_live_opt_in()
    if not _has_native_web_search(case):
        pytest.skip("model does not advertise a native web_search builtin")

    adapter = _build_adapter(case)
    resp = adapter.complete(
        LLMRequest(
            messages=[
                Message(
                    role="user",
                    content=(
                        "Search the web for the official website of the City of Boston. "
                        "Reply with the URL and cite your sources."
                    ),
                )
            ],
            tools=[get_unified_web_search_schema()],
            model_settings=_base_settings(case, max_tokens=1024, enable_thinking=False),
        )
    )
    assert_canonical_llm_response(resp)
    urls = _citation_urls(resp)
    assert urls, (
        f"{case.provider}/{case.model}: native web_search returned no citation URLs "
        f"(needed for core.web_search web_search_path=llm)"
    )
    assert any(url.startswith("http") for url in urls)
    # Same objects must survive the model_inference envelope (not OpenAI-only).
    payload = _citations_payload(resp)
    assert payload, (
        f"{case.provider}/{case.model}: adapter citations did not serialize for "
        "model_inference / core.web_search"
    )
    assert any(str(item.get("url") or "").startswith("http") for item in payload)


@pytest.mark.timeout(180)
@pytest.mark.parametrize("case", _LIVE_CASES, ids=[_case_id(c) for c in _LIVE_CASES])
def test_live_native_web_search_mixes_with_function_tools(case: LiveAdapterCase) -> None:
    """Native web_search plus a Motet function tool on one request must be accepted."""
    _require_live_opt_in()
    if not _has_native_web_search(case):
        pytest.skip("model does not advertise a native web_search builtin")
    if CAP_TOOL_USE not in case.spec.capabilities:
        pytest.skip("model does not advertise CAP_TOOL_USE")

    adapter = _build_adapter(case)
    function_name = "mcp.test.add_two_numbers"
    tools = [
        get_unified_web_search_schema(),
        CanonicalToolSchema(
            name=tool_canonical_to_wire(function_name),
            description="Add two integers and return their sum.",
            json_schema={
                "type": "object",
                "properties": {
                    "a": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["a", "b"],
            },
        ),
    ]
    resp = adapter.complete(
        LLMRequest(
            messages=[
                Message(
                    role="user",
                    content=(
                        "Search the web for one current fact about Boston City Hall. "
                        "You may also call the add-two-numbers tool if you need arithmetic."
                    ),
                )
            ],
            tools=tools,
            model_settings=_base_settings(case, max_tokens=1024, enable_thinking=False),
        )
    )
    assert_canonical_llm_response(resp)
    function_calls = [
        item for item in resp.output_items if isinstance(item, ToolCallRequest)
    ]
    urls = _citation_urls(resp)
    text = (resp.output_text or "").strip()
    assert urls or function_calls or text, (
        f"{case.provider}/{case.model}: mixed tools request returned no citations, "
        "function calls, or text"
    )


def _usage_cache_creation(usage: Optional[LLMUsage]) -> int:
    if usage is None:
        return 0
    return int(usage.cache_creation_tokens or 0)


@pytest.mark.parametrize("case", _LIVE_CASES, ids=[_case_id(c) for c in _LIVE_CASES])
def test_live_prompt_cache_hit_on_stable_prefix(case: LiveAdapterCase) -> None:
    """
    ADR-0124 live hit check: identical tools + system prefix → cache_read on call 2.

    Mirrors the sticky tool-set goal (ADR-0074 Rule 15): keep the tools→system
    segment byte-stable across turns so provider prefix caches can hit. Skips
    models without CAP_PROMPT_CACHING (e.g. Gemini).
    """
    _require_live_opt_in()
    if CAP_PROMPT_CACHING not in case.spec.capabilities:
        pytest.skip("model does not advertise CAP_PROMPT_CACHING")
    if CAP_TOOL_USE not in case.spec.capabilities:
        pytest.skip("cache hit probe requires CAP_TOOL_USE for a stable tools prefix")

    adapter = _build_adapter(case)
    conversation_id = f"live-cache-{case.provider}-{uuid4().hex[:12]}"
    system_text = live_cacheable_system_text()
    tools = _cache_probe_tools()
    # Always-on reasoning hosts need headroom; content is still a short token.
    out_max = 256 if (_always_on_thinking(case) or CAP_REASONING in case.spec.capabilities) else 32

    def _request(user_text: str) -> LLMRequest:
        return LLMRequest(
            messages=[
                Message(role="system", content=system_text),
                Message(role="user", content=user_text),
            ],
            tools=tools,
            model_settings=_base_settings(
                case,
                max_tokens=out_max,
                enable_thinking=False,
                enable_prompt_caching=True,
            ),
            request_context=RequestContext(
                conversation_id=conversation_id,
                tenant_id="live-adapter-matrix",
                principal_id="live-adapter-matrix",
            ),
        )

    resp1 = adapter.complete(_request("Reply with exactly the single word: one"))
    assert_canonical_llm_response(resp1)
    assert resp1.usage is not None, f"{case.provider}/{case.model}: missing usage on cache seed call"

    resp2 = adapter.complete(_request("Reply with exactly the single word: two"))
    assert_canonical_llm_response(resp2)
    assert resp2.usage is not None, f"{case.provider}/{case.model}: missing usage on cache replay call"

    read2 = _usage_cache_read(resp2.usage)
    if read2 <= 0:
        # Automatic providers may need a beat for cache affinity / write visibility.
        time.sleep(1.5)
        resp3 = adapter.complete(_request("Reply with exactly the single word: three"))
        assert_canonical_llm_response(resp3)
        read2 = _usage_cache_read(resp3.usage)
        resp2 = resp3

    created1 = _usage_cache_creation(resp1.usage)
    read1 = _usage_cache_read(resp1.usage)
    assert read2 > 0, (
        f"{case.provider}/{case.model}: expected cache_read_tokens > 0 on stable "
        f"tools+system prefix replay (ADR-0124). "
        f"usage1(creation={created1}, read={read1}) "
        f"usage2(creation={_usage_cache_creation(resp2.usage)}, read={read2}) "
        f"conversation_id={conversation_id}"
    )
