"""
Motet - Live Meta-Tool Disclosure PoC

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Opt-in live harness for the capability-disclosure design note: offer only
    core__tools_search + core__tool_call against a fake Motet catalog and score
    whether each model closes search → tool_call on capability-needing prompts.

    Safety / spend control (same gate as the live adapter matrix):
      export MOTET_LIVE_ADAPTER_MATRIX=1
      export OPENAI_API_KEY=...   # (and/or other provider keys)

    Optional case filter:
      export MOTET_LIVE_ADAPTER_CASES=openai:gpt-5.5,anthropic:claude-sonnet-4.5

Dependencies:
    - pytest
    - tests.fixtures.live_adapter_matrix / meta_tool_disclosure_poc
    - motet.core.models.adapters / types

Usage:
    MOTET_LIVE_ADAPTER_MATRIX=1 OPENAI_API_KEY=... \\
      pytest tests/integration/test_live_meta_tool_disclosure_poc.py -v -s

Notes:
    - No Docker stack required (adapter-only calls).
    - Does not wire real registry tools or the agentic shortlist.
    - Replay applies model.py ``_apply_wire_names_to_messages`` so inbound
      canonical names (core.tools_search) are provider-safe on the next turn.
"""

from __future__ import annotations

import json
from typing import Any, List
from uuid import uuid4

import pytest

from motet.core.commands.builtin.model import _apply_wire_names_to_messages
from motet.core.models.adapters import adapter_registry
from motet.core.models.specs import CAP_REASONING, CAP_TOOL_USE
from motet.core.types import LLMRequest, Message, RequestContext, ToolCallRequest
from tests.fixtures.canonical_adapter_contract import assert_canonical_llm_response
from tests.fixtures.live_adapter_matrix import (
    LiveAdapterCase,
    iter_live_cases,
    live_matrix_enabled,
    resolve_credentials,
)
from tests.fixtures.meta_tool_disclosure_poc import (
    META_TOOL_SCHEMAS,
    SCENARIOS,
    SYSTEM_PROMPT,
    Scenario,
    ScenarioResult,
    RoundTrace,
    dispatch_meta_tool,
    format_summary_table,
    parse_tool_arguments,
    score_scenario,
    _META_CALL_ALIASES,
    _META_SEARCH_ALIASES,
    _normalize_catalog_name,
)


pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.live_llm]

_MAX_ROUNDS = 5
_POC_RESULTS: List[ScenarioResult] = []


def _require_live_opt_in() -> None:
    if not live_matrix_enabled():
        pytest.skip(
            "Live adapter matrix disabled. Set MOTET_LIVE_ADAPTER_MATRIX=1 "
            "and provider API keys to run (incurs provider spend)."
        )


_LIVE_CASES: List[LiveAdapterCase] = iter_live_cases()


def _case_id(case: LiveAdapterCase) -> str:
    return f"{case.provider}/{case.model}"


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
    return case.spec.name or case.model


def _base_settings(case: LiveAdapterCase, **extra: Any) -> dict[str, Any]:
    default_max = 1024 if CAP_REASONING in case.spec.capabilities else 512
    settings: dict[str, Any] = {
        "provider": case.provider,
        "model_name": _api_model_name(case),
        "temperature": 0.0,
        "max_tokens": default_max,
        "enable_thinking": False,
    }
    settings.update(extra)
    return settings


def _tool_calls_from_response(resp: Any) -> List[ToolCallRequest]:
    return [item for item in (resp.output_items or []) if isinstance(item, ToolCallRequest)]


def _record_round(
    tool_calls: List[ToolCallRequest],
    observations: List[str],
) -> RoundTrace:
    """Build a RoundTrace from dispatched meta-tool calls."""
    trace = RoundTrace()
    for tc, obs in zip(tool_calls, observations):
        name = tc.tool_name or ""
        trace.tool_names.append(name)
        args = parse_tool_arguments(tc.arguments_json, tc.arguments)
        if name in _META_SEARCH_ALIASES:
            q = str(args.get("query") or "")
            if q:
                trace.search_queries.append(q)
        if name in _META_CALL_ALIASES:
            target = str(args.get("tool_name") or args.get("name") or "")
            canonical = _normalize_catalog_name(target)
            trace.tool_call_targets.append(canonical or target)
            try:
                payload = json.loads(obs)
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict) and payload.get("ok") is True:
                ok_name = str(payload.get("tool_name") or canonical)
                trace.successful_calls.append(ok_name)
            else:
                trace.failed_calls.append(canonical or target)
    return trace


def run_meta_disclosure_loop(
    adapter: Any,
    case: LiveAdapterCase,
    scenario: Scenario,
) -> ScenarioResult:
    """Multi-round tools_search → tool_call loop against the fake catalog."""
    case_id = _case_id(case)
    conversation_id = f"meta-poc-{case.provider}-{uuid4().hex[:10]}"
    messages: List[Message] = [
        Message(role="system", content=SYSTEM_PROMPT),
        Message(role="user", content=scenario.user_prompt),
    ]
    tools = META_TOOL_SCHEMAS
    traces: List[RoundTrace] = []

    for round_idx in range(1, _MAX_ROUNDS + 1):
        resp = adapter.complete(
            LLMRequest(
                messages=_apply_wire_names_to_messages(messages) or messages,
                tools=tools,
                model_settings=_base_settings(case),
                request_context=RequestContext(
                    conversation_id=conversation_id,
                    tenant_id="meta-tool-disclosure-poc",
                    principal_id="meta-tool-disclosure-poc",
                ),
            )
        )
        assert_canonical_llm_response(resp)
        tool_calls = _tool_calls_from_response(resp)
        if not tool_calls:
            # Model stopped without further tools — score what we have.
            result = score_scenario(
                scenario, case_id=case_id, traces=traces, rounds=round_idx
            )
            return result

        observations: List[str] = []
        for tc in tool_calls:
            args = parse_tool_arguments(tc.arguments_json, tc.arguments)
            observations.append(dispatch_meta_tool(tc.tool_name, args))

        traces.append(_record_round(tool_calls, observations))

        # Early success for need-* once a required tool_call succeeds.
        if scenario.required_tool_names:
            successes = {n for t in traces for n in t.successful_calls}
            if successes & scenario.required_tool_names:
                return score_scenario(
                    scenario, case_id=case_id, traces=traces, rounds=round_idx
                )

        # Early fail for idle if tool_call was attempted.
        if scenario.forbid_tool_call and any(t.tool_call_targets for t in traces):
            return score_scenario(
                scenario, case_id=case_id, traces=traces, rounds=round_idx
            )

        messages.append(
            Message(
                role="assistant",
                content=resp.output_text or "",
                tool_calls_canonical=tool_calls,
                reasoning_content=getattr(resp, "reasoning_content", None),
                reasoning_blocks=getattr(resp, "reasoning_blocks", None),
            )
        )
        for tc, obs in zip(tool_calls, observations):
            messages.append(
                Message(
                    role="tool",
                    name=tc.tool_name,
                    content=obs,
                    tool_call_id=tc.call_id,
                )
            )

    return score_scenario(
        scenario, case_id=case_id, traces=traces, rounds=_MAX_ROUNDS
    )


@pytest.fixture(scope="session", autouse=True)
def _print_poc_summary(request: pytest.FixtureRequest) -> None:
    """Print compact results table after the session (visible with pytest -s)."""

    def _finalize() -> None:
        if not _POC_RESULTS:
            return
        print("\n=== Meta-tool disclosure PoC summary ===")
        print(format_summary_table(_POC_RESULTS))
        print("=== end PoC summary ===\n")

    request.addfinalizer(_finalize)


@pytest.mark.parametrize("case", _LIVE_CASES, ids=[_case_id(c) for c in _LIVE_CASES])
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.scenario_id for s in SCENARIOS])
def test_live_meta_tool_disclosure_poc(
    case: LiveAdapterCase, scenario: Scenario
) -> None:
    _require_live_opt_in()
    if CAP_TOOL_USE not in case.spec.capabilities:
        result = ScenarioResult(
            case_id=_case_id(case),
            scenario_id=scenario.scenario_id,
            verdict="SKIP",
            reason="no_cap_tool_use",
        )
        _POC_RESULTS.append(result)
        pytest.skip("model does not advertise CAP_TOOL_USE")

    adapter = _build_adapter(case)
    result = run_meta_disclosure_loop(adapter, case, scenario)
    _POC_RESULTS.append(result)

    assert result.verdict == "PASS", (
        f"{result.case_id} {result.scenario_id}: {result.verdict} "
        f"reason={result.reason} searched={result.searched} "
        f"calls={result.tool_calls} warnings={result.warnings}"
    )
