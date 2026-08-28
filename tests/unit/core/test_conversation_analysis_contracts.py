"""
Motet - Conversation Analysis Output Contract Tests

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-22

Description:
    Guards the ADR-0114 output contracts that the conversation analysis
    dimensions attach to their inference calls. The *Result models are the
    schema source, so a classification field typed as a bare ``str`` reaches
    the model as an unconstrained string and it will echo the prompt's
    "a|b|c" option list back as a literal value -- which is how intent
    returned "plan|question|task_request" in production.

Dependencies:
    - pytest: Test framework
    - conversation_analysis data_classes: The models under test
    - conversation_analysis dimension modules: Prompt builders the schemas
      must stay in step with

Usage:
    pytest tests/unit/core/test_conversation_analysis_contracts.py

Notes:
    - Asserts each classification field emits a JSON Schema enum
    - Asserts the enum matches the option list in that dimension's prompt,
      so the two cannot drift apart in either direction
    - Free-text and numeric fields are deliberately excluded
"""

import re

import pytest

from motet.core.commands.builtin.conversation_analysis.complexity_analysis import (
    _build_complexity_prompt,
)
from motet.core.commands.builtin.conversation_analysis.data_classes import (
    DEFAULT_ANALYSIS_DIMENSIONS,
    ComplexityAnalysisResult,
    ConversationAnalysisData,
    IntentAnalysisResult,
    ToneAnalysisResult,
)
from motet.core.commands.builtin.conversation_analysis.intent_analysis import (
    _build_intent_prompt,
)
from motet.core.commands.builtin.conversation_analysis.tone_analysis import (
    _build_tone_prompt,
)

# Fields the model must pick from a fixed vocabulary, by result model.
CONSTRAINED_FIELDS = {
    IntentAnalysisResult: ["primary"],
    ToneAnalysisResult: [
        "emotion",
        "urgency",
        "satisfaction",
        "communication_style",
    ],
    ComplexityAnalysisResult: [
        "level",
        "scope",
        "tool_requirements",
        "expertise_needed",
    ],
}

PROMPT_BUILDERS = {
    IntentAnalysisResult: _build_intent_prompt,
    ToneAnalysisResult: _build_tone_prompt,
    ComplexityAnalysisResult: _build_complexity_prompt,
}


def _prompt_options(prompt: str, field: str) -> list:
    """Pull the "a|b|c" option list a prompt offers for one field."""
    match = re.search(rf'"{field}":\s*"([^"]*\|[^"]*)"', prompt)
    return match.group(1).split("|") if match else []


@pytest.mark.parametrize(
    "model,field",
    [
        (model, field)
        for model, fields in CONSTRAINED_FIELDS.items()
        for field in fields
    ],
)
def test_classification_fields_emit_enums(model, field):
    """Every classification field constrains the model to a fixed set."""
    schema = model.model_json_schema()["properties"][field]

    assert "enum" in schema, (
        f"{model.__name__}.{field} reaches the model as an unconstrained "
        "string, so it can echo the prompt's option list verbatim. Type it "
        "as a Literal."
    )
    assert schema["enum"], f"{model.__name__}.{field} has an empty enum"


@pytest.mark.parametrize(
    "model,field",
    [
        (model, field)
        for model, fields in CONSTRAINED_FIELDS.items()
        for field in fields
    ],
)
def test_enums_match_the_prompt_they_ship_with(model, field):
    """Schema and prompt offer the same vocabulary."""
    prompt = PROMPT_BUILDERS[model]("example message", None)
    offered = _prompt_options(prompt, field)

    if not offered:
        pytest.skip(f"{field} is not offered as an option list in the prompt")

    enum = model.model_json_schema()["properties"][field]["enum"]
    assert sorted(enum) == sorted(offered), (
        f"{model.__name__}.{field} drifted from its prompt: schema offers "
        f"{sorted(enum)}, prompt offers {sorted(offered)}"
    )


def test_default_analysis_has_no_llm_dimensions():
    """ADR-0138: the turn gate is local, so the default turn calls no model here.

    Every dimension lost its consumer — `intent` routed nothing once
    `strategy_hint` went, and `context` existed to feed the clarification route
    the agent loop now covers. All five stay available opt-in.
    """
    assert DEFAULT_ANALYSIS_DIMENSIONS == []
    data = ConversationAnalysisData(messages=[])
    assert data.analysis_dimensions == []


def test_dimensions_remain_available_opt_in():
    data = ConversationAnalysisData(
        messages=[], analysis_dimensions=["intent", "complexity"]
    )
    assert data.analysis_dimensions == ["intent", "complexity"]
