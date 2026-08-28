"""
Motet - Turn Hook Resolve Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Unit tests for registry-resolved turn hooks: skip on None, Motet
    defaults accepted at load, finalize fallback, and payload mismatch.

Usage:
    pytest tests/unit/core/orchestration/test_hook_resolve.py
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from motet.core.orchestration.turn.hook_models import (
    ConversationAnalysisResult,
    TurnAfterFinalizeData,
    parse_analysis_result,
)
from motet.core.orchestration.turn.hook_resolve import (
    DEFAULT_FINALIZE_COMMAND,
    HookPayloadError,
    instantiate_hook_data,
    resolve_hook_implementation,
    validate_turn_hooks,
)
from motet.core.orchestration.turn.output_contract import validate_contract_text
from motet.core.types import OutputContract


def test_none_hook_skips() -> None:
    assert resolve_hook_implementation(None, slot="context_prepare") is None
    assert resolve_hook_implementation("", slot="finalize") is None


def test_motet_defaults_pass_load_validation() -> None:
    hooks = SimpleNamespace(
        conversation_analysis="core.conversation_analysis",
        memory_reset="core.memory_reset",
        context_prepare="core.prepare_context",
        finalize="core.finalize_turn",
        context_inject=["core.page_context"],
        after_finalize=None,
    )
    validate_turn_hooks(hooks, require_registered=True)


def test_unknown_non_finalize_returns_none(monkeypatch) -> None:
    from motet.core.commands.command_type_registry import command_type_registry

    monkeypatch.setattr(command_type_registry, "get", lambda name: None)
    assert resolve_hook_implementation("bundle.missing", slot="context_prepare") is None


def test_unknown_finalize_falls_back(monkeypatch) -> None:
    from motet.core.commands.command_type_registry import command_type_registry

    fallback = SimpleNamespace(implementation=lambda data: {"ok": True})

    def _get(name: str) -> Any:
        if name == DEFAULT_FINALIZE_COMMAND:
            return fallback
        return None

    monkeypatch.setattr(command_type_registry, "get", _get)
    impl = resolve_hook_implementation("bundle.typo_finalize", slot="finalize")
    assert impl is fallback.implementation


def test_parse_nested_analysis_result() -> None:
    parsed = parse_analysis_result(
        {
            "intent": {"primary": "question", "confidence": 0.9},
            "complexity": {"tool_requirements": "basic"},
            "rag": {"wanted": True},
            "metadata": {"analysis_mode": "full"},
        }
    )
    assert isinstance(parsed, ConversationAnalysisResult)
    assert parsed.intent == "question"
    assert parsed.intent_confidence == 0.9
    assert parsed.tool_requirements == "basic"
    assert parsed.analysis_mode == "full"
    assert parsed.rag == {"wanted": True}


def test_instantiate_hook_payload_mismatch() -> None:
    from motet.core.commands.command_data_registry import command_data_registry

    class StrictData(TurnAfterFinalizeData):
        extra_required: str

    command_data_registry.register("test.strict_hook", StrictData)
    try:
        with pytest.raises(HookPayloadError):
            instantiate_hook_data(
                "test.strict_hook",
                TurnAfterFinalizeData(assistant_response="hi", agent_id="core.default"),
            )
    finally:
        command_data_registry.unregister("test.strict_hook")


def test_stale_hook_field_names_fail_at_parse() -> None:
    from motet.core.agents.registry import TurnHooks
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TurnHooks(memory_prepare="core.prepare_context")
    with pytest.raises(ValidationError):
        TurnHooks(memory_finalize="core.finalize_turn")


def test_output_contract_json_validation() -> None:
    contract = OutputContract(
        format="json",
        json_schema={"type": "object", "required": ["answer"]},
    )
    assert validate_contract_text('{"answer": "yes"}', contract) is None
    assert validate_contract_text("not json", contract) is not None
    assert validate_contract_text("{}", contract) is not None
