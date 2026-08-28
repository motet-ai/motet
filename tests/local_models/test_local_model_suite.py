"""
Motet - Live Local Model Capability Smoke Suite

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-04

Description:
    Gated pytest suite for validating the local GGUF tier against real model
    assets. It loads every configured local model and exercises the manager path
    for native chat templates, stop behavior, structured JSON output, native
    tool-call recovery, post-tool finalization, and Qwen-style thinking controls.
    These checks are not part of normal CI because they require multi-GB model
    files and llama.cpp.

Dependencies:
    - pytest: Parametrized smoke tests and environment-gated skipping
    - llama-cpp-python: Real GGUF loading and inference
    - LocalModelCache: llama.cpp model loading with embedded-Jinja preference
    - LocalInferenceManager: Runtime manager inference path under test
    - ModelSpec registry: Capability gating for structured output, tools, and reasoning

Usage:
    # Host / Metal, using repo-local models:
    MOTET_RUN_LOCAL_MODEL_TESTS=1 MOTET_LOCAL_MODEL_DIR="$(pwd)/models" \\
        PYTHONPATH="$(pwd)" pytest tests/local_models/test_local_model_suite.py -s

    # Container, using /app/models:
    docker exec -e PYTHONPATH=/app -e MOTET_RUN_LOCAL_MODEL_TESTS=1 \\
        motet_dev-local-inference-1 \\
        pytest /app/tests/local_models/test_local_model_suite.py -s

Notes:
    - The suite skips by default unless MOTET_RUN_LOCAL_MODEL_TESTS=1.
    - Missing GGUF assets skip by default; set MOTET_LOCAL_MODEL_REQUIRE_ASSETS=1
      to fail instead when any configured local model is absent.
    - Tool-call propensity can vary by model/template. A tool-capable model that
      answers in prose instead of calling the tool is reported as xfail so the
      suite records the behavior without breaking unrelated local-model smoke.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from motet.core.models.local.inference_manager import (
    DEFAULT_MODEL_PATHS,
    LocalInferenceManager,
)
from motet.core.models.local.model_cache import LocalModelCache
from motet.core.models.local.reasoning import parse_tool_calls
from motet.core.models.registry import get_model_spec
from motet.core.models.specs import (
    CAP_REASONING,
    CAP_STRUCTURED_OUTPUT,
    CAP_TOOL_USE,
)


pytestmark = [
    pytest.mark.local_models,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.getenv("MOTET_RUN_LOCAL_MODEL_TESTS") != "1",
        reason="Set MOTET_RUN_LOCAL_MODEL_TESTS=1 to run live GGUF local-model tests.",
    ),
]

MODEL_IDS: Tuple[str, ...] = tuple(DEFAULT_MODEL_PATHS.keys())
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HOST_MODEL_DIR = REPO_ROOT / "models"

CHAT_PROMPT = "Reply with exactly: hello world"
STRUCTURED_PROMPT = (
    "Extract the person as JSON matching the schema. "
    "Text: 'Ada Lovelace is 36 and lives in London.'"
)
THINKING_PROMPT = (
    "A farmer has 17 sheep; all but 9 run away. How many remain? "
    "Think briefly, then answer."
)
FACT_PROMPT = "What is the population of Paris?"
TOOL_PROMPT = "Call the get_weather tool for San Francisco. Do not answer in prose."

PERSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer"},
        "city": {"type": "string"},
    },
    "required": ["name", "age", "city"],
    "additionalProperties": False,
}

WEATHER_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"},
                },
                "required": ["city"],
            },
        },
    }
]


def _model_dir() -> Path:
    return Path(os.getenv("MOTET_LOCAL_MODEL_DIR") or DEFAULT_HOST_MODEL_DIR)


def _configured_model_paths() -> Dict[str, Path]:
    custom = os.getenv("MOTET_LOCAL_MODEL_PATHS")
    if custom:
        raw = json.loads(custom)
        return {model_id: Path(path) for model_id, path in raw.items()}
    model_dir = _model_dir()
    return {
        model_id: model_dir / Path(default_path).name
        for model_id, default_path in DEFAULT_MODEL_PATHS.items()
    }


def _spec_capabilities(model_id: str) -> set[str]:
    spec = get_model_spec("local", model_id)
    return set(spec.capabilities) if spec else set()


def _require_model_path(model_id: str) -> Path:
    path = _configured_model_paths()[model_id]
    if path.exists():
        return path
    message = (
        f"Missing GGUF for {model_id}: {path}. "
        "Set MOTET_LOCAL_MODEL_DIR or MOTET_LOCAL_MODEL_PATHS."
    )
    if os.getenv("MOTET_LOCAL_MODEL_REQUIRE_ASSETS") == "1":
        pytest.fail(message)
    pytest.skip(message)


def _load_model(model_id: str) -> Tuple[Any, LocalInferenceManager]:
    try:
        import llama_cpp  # type: ignore[reportMissingImports]  # noqa: F401
    except ImportError:
        pytest.skip("llama-cpp-python is not installed.")

    path = _require_model_path(model_id)
    max_memory_gb = float(os.getenv("MOTET_LOCAL_MODEL_TEST_MAX_MEMORY_GB", "24"))
    cache = LocalModelCache(max_memory_gb=max_memory_gb, cache_size=1, engine="llama_cpp")
    model, _metadata = cache.get_or_load_model_sync(model_id, model_path=str(path))
    manager = LocalInferenceManager.__new__(LocalInferenceManager)
    return model, manager


def _assert_clean_assistant_text(text: str) -> None:
    assert text.strip(), "model returned empty assistant text"
    assert "<think>" not in text and "</think>" not in text
    assert "User:" not in text and "Assistant:" not in text


def _assert_schema_valid_json(text: str) -> None:
    parsed = json.loads(text)
    assert set(PERSON_SCHEMA["required"]).issubset(parsed.keys())
    assert not (set(parsed.keys()) - set(PERSON_SCHEMA["properties"].keys()))
    assert isinstance(parsed["age"], int)


def _print_result(label: str, result: Dict[str, Any]) -> None:
    text = str(result.get("text") or "")
    print(
        f"[{label}] finish={result.get('finish_reason')} "
        f"text={text[:180]!r} reasoning={bool(result.get('reasoning'))} "
        f"tools={bool(result.get('tool_calls'))}"
    )


@pytest.mark.parametrize("model_id", MODEL_IDS)
@pytest.mark.timeout(300)
def test_local_model_live_capability_suite(model_id: str) -> None:
    """Load one local model and run capability-scoped live smoke checks."""
    caps = _spec_capabilities(model_id)
    model, manager = _load_model(model_id)

    chat_result = manager._run_llama_cpp_inference(
        model,
        {
            "model_id": model_id,
            "messages": [{"role": "user", "content": CHAT_PROMPT}],
            "temperature": 0.0,
            "max_tokens": 64,
            "enable_thinking": False,
        },
    )
    _print_result(f"{model_id}:chat", chat_result)
    _assert_clean_assistant_text(str(chat_result.get("text") or ""))
    assert chat_result.get("finish_reason") in {"stop", "length"}

    if CAP_STRUCTURED_OUTPUT in caps:
        structured_result = manager._run_llama_cpp_inference(
            model,
            {
                "model_id": model_id,
                "messages": [{"role": "user", "content": STRUCTURED_PROMPT}],
                "temperature": 0.0,
                "max_tokens": 160,
                "enable_thinking": False,
                "json_schema": PERSON_SCHEMA,
            },
        )
        _print_result(f"{model_id}:structured", structured_result)
        _assert_schema_valid_json(str(structured_result.get("text") or ""))

    if CAP_TOOL_USE in caps:
        tool_result = manager._run_llama_cpp_inference(
            model,
            {
                "model_id": model_id,
                "messages": [{"role": "user", "content": TOOL_PROMPT}],
                "temperature": 0.0,
                "max_tokens": 512,
                "enable_thinking": False,
                "tools": WEATHER_TOOLS,
                "tool_choice": "auto",
            },
        )
        _print_result(f"{model_id}:tools", tool_result)
        canonical = parse_tool_calls(tool_result.get("tool_calls"))
        if not canonical:
            pytest.xfail(f"{model_id} did not emit a tool call for the smoke prompt.")
        assert canonical[0].tool_name == "get_weather"
        assert canonical[0].arguments_json

        final_result = manager._run_llama_cpp_inference(
            model,
            {
                "model_id": model_id,
                "messages": [
                    {"role": "user", "content": TOOL_PROMPT},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "call_id": canonical[0].call_id,
                                "tool_name": canonical[0].tool_name,
                                "arguments_json": canonical[0].arguments_json,
                                "arguments": canonical[0].arguments,
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "content": '{"temperature": 72, "condition": "sunny"}',
                        "name": canonical[0].tool_name,
                        "tool_call_id": canonical[0].call_id,
                    },
                ],
                "temperature": 0.0,
                "max_tokens": 192,
                "enable_thinking": False,
                "tools": WEATHER_TOOLS,
                "tool_choice": "auto",
            },
        )
        _print_result(f"{model_id}:post_tool", final_result)
        if final_result.get("tool_calls") and model_id.startswith("gemma-4"):
            pytest.fail(f"{model_id} repeated a tool call after receiving a tool result.")
        if final_result.get("tool_calls"):
            pytest.xfail(f"{model_id} repeated a tool call after receiving a tool result.")
        _assert_clean_assistant_text(str(final_result.get("text") or ""))

    if CAP_REASONING in caps:
        thinking_result = manager._run_llama_cpp_inference(
            model,
            {
                "model_id": model_id,
                "messages": [{"role": "user", "content": THINKING_PROMPT}],
                "temperature": 0.2,
                "max_tokens": 256,
                "enable_thinking": True,
            },
        )
        _print_result(f"{model_id}:thinking", thinking_result)
        assert thinking_result.get("reasoning"), "reasoning-capable model emitted no reasoning"
        text = str(thinking_result.get("text") or "")
        assert "<think>" not in text and "</think>" not in text

        no_think_result = manager._run_llama_cpp_inference(
            model,
            {
                "model_id": model_id,
                "messages": [{"role": "user", "content": FACT_PROMPT}],
                "temperature": 0.0,
                "max_tokens": 160,
                "enable_thinking": False,
            },
        )
        _print_result(f"{model_id}:no_think", no_think_result)
        assert not no_think_result.get("reasoning")
        _assert_clean_assistant_text(str(no_think_result.get("text") or ""))
