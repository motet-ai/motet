"""
Motet - ADR-0115 Local Native Tool Calling Manual Harness

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-04

Description:
Verifies native tool calling (ADR-0115 Path B) for the tool-capable local tier
against real GGUF weights — the per-family tool-call token format is the fragile
part that cannot be validated in CI. For each model it passes an OpenAI-style
tool schema to ``create_chat_completion`` and:

1. Non-stream: parses ``message.tool_calls`` via ``parse_tool_calls`` and reports
   the canonical ``ToolCallRequest`` (name + parsed arguments) and finish_reason.
2. Stream: accumulates streamed ``delta.tool_calls`` fragments and reports the
   consolidated tool name + arguments, mirroring the manager's stream path.

Dependencies:
- motet.core.models.local.model_cache: cache loader + stop resolution
- motet.core.models.local.reasoning: parse_tool_calls
- motet.core.models.local.inference_manager: DEFAULT_MODEL_PATHS
- llama-cpp-python (>=0.3.24)

Usage:
    # In the local-inference container (CPU):
    docker exec -e PYTHONPATH=/app motet_dev-local-inference-1 \\
        python /app/tests/manual/_adr0117_tools.py qwen3-8b-instruct

    # On host (Metal):
    MOTET_LOCAL_MODEL_DIR="$(pwd)/models" PYTHONPATH="$(pwd)" \\
        python3 tests/manual/_adr0117_tools.py ministral-3-8b-instruct llama-3.1-8b-instruct

Notes:
- Defaults to the tool-capable models: native-``tools=`` (qwen3 / ministral-3 /
  llama-3.1) plus phi-4-mini via system-message injection. phi-4's template reads
  tools from the system message (``message['tools']``), not llama.cpp's native
  ``tools=`` arg, so the manager injects them there (the phi profile's
  ``apply_tool_schemas``)
  and recovers the bare-JSON-array output via ``extract_tool_calls_from_text``.
- A model that does not emit a tool call for this prompt is reported (not an
  assertion failure) since tool-call propensity is prompt/model dependent. phi is
  most reliable at low temperature (it occasionally answers in prose otherwise).
"""
import json
import os
import sys

from motet.core.models.local.model_cache import LocalModelCache
from motet.core.models.local.reasoning import parse_tool_calls
from motet.core.models.local.inference_manager import (
    DEFAULT_MODEL_PATHS,
    LocalInferenceManager,
)

os.environ.setdefault("MOTET_LOCAL_MODEL_DIR", "/app/models")

model_ids = sys.argv[1:] or [
    "qwen3-8b-instruct",
    "ministral-3-8b-instruct",
    "llama-3.1-8b-instruct",
    "phi-4-mini",  # tool use via system-message injection (ADR-0115); see Notes
]

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "City name"}},
                "required": ["city"],
            },
        },
    }
]

PROMPT = "What's the weather in San Francisco right now? Use the get_weather tool."


def _run(manager: LocalInferenceManager, model_id: str) -> None:
    path = DEFAULT_MODEL_PATHS[model_id]
    print(f"\n=== {model_id} ===")
    print("path:", path, "exists:", os.path.exists(path))
    if not os.path.exists(path):
        print("SKIP (asset missing)")
        return

    cache = LocalModelCache(max_memory_gb=20, cache_size=1, engine="llama_cpp")
    model, _ = cache.get_or_load_model_sync(model_id, model_path=path)

    # Exercise the real manager path (sampling, tool kwargs, native + text-based
    # tool-call recovery), not raw create_chat_completion. enable_thinking=False so
    # reasoning tokens don't crowd out the tool call on a small budget.
    request = {
        "model_id": model_id,
        "messages": [{"role": "user", "content": PROMPT}],
        "tools": TOOLS,
        "tool_choice": "auto",
        "max_tokens": 512,
        "enable_thinking": False,
    }

    # 1) Non-stream (manager path).
    result = manager._run_llama_cpp_inference(model, request)
    canonical = parse_tool_calls(result.get("tool_calls"))
    print("[non-stream] finish_reason:", result.get("finish_reason"))
    print("[non-stream] tool_call_count:", len(canonical))
    for tc in canonical:
        print("  ->", tc.tool_name, "args:", tc.arguments_json)

    # 2) Stream (manager path): collect tool_call_complete events.
    completed = []
    final_finish = None
    for event in manager._run_llama_cpp_inference_stream(model, request):
        etype = event.get("type")
        if etype == "tool_call_complete":
            completed.append(event)
        elif etype == "final":
            final_finish = event.get("finish_reason")
    print("[stream] finish_reason:", final_finish, "tool_call_count:", len(completed))
    for ev in completed:
        try:
            parsed = json.loads(ev.get("arguments_json") or "{}")
        except ValueError:
            parsed = ev.get("arguments_json")
        print("  ->", ev.get("tool_name"), "args:", parsed)


_manager = LocalInferenceManager()
for mid in model_ids:
    _run(_manager, mid)

print("\nTOOLS_OK")
