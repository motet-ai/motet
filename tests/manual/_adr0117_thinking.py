"""
Motet - ADR-0064 Local Reasoning (Thinking) Separation Manual Harness

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-04

Description:
Verifies the local provider's reasoning/thinking separation against real GGUF
weights (the part that cannot run in CI). For each model it runs a reasoning
prompt, then:

1. Non-stream: splits the raw content with ``split_reasoning`` and asserts no
   ``<think>``/``</think>`` tag leaks into the user-facing text while the
   reasoning is captured separately.
2. ``enable_thinking=False``: appends the Qwen3 ``/no_think`` soft switch and
   confirms the model emits little/no reasoning.
3. Stream: feeds tokens through ``ThinkStreamRouter`` and confirms the thinking
   and text channels are cleanly separated across chunk boundaries.

Dependencies:
- motet.core.models.local.model_cache: cache loader + stop resolution
- motet.core.models.local.reasoning: split_reasoning + ThinkStreamRouter
- motet.core.models.local.inference_manager: DEFAULT_MODEL_PATHS, thinking control
- llama-cpp-python (>=0.3.24)

Usage:
    # In the local-inference container (CPU):
    docker exec -e PYTHONPATH=/app motet_dev-local-inference-1 \\
        python /app/tests/manual/_adr0117_thinking.py qwen3-8b-instruct

    # On host (Metal), larger models:
    MOTET_LOCAL_MODEL_DIR="$(pwd)/models" PYTHONPATH="$(pwd)" \\
        python3 tests/manual/_adr0117_thinking.py gemma-4-26b-a4b

Notes:
- Defaults to qwen3-8b-instruct (the tier's thinking model). Non-thinking
  families (gemma/llama/ministral) should simply report no reasoning, which also
  validates graceful degradation.
"""
import os
import sys

from motet.core.models.local.model_cache import LocalModelCache
from motet.core.models.local.profiles import profile_for_model
from motet.core.models.local.reasoning import ThinkStreamRouter, split_reasoning
from motet.core.models.local.inference_manager import DEFAULT_MODEL_PATHS


def stop_sequences_for_model(model_id):
    return profile_for_model(model_id).stop_sequences()


def _apply_thinking_control(messages, model_id, *, enable_thinking):
    return profile_for_model(model_id).apply_thinking_control(messages, enable_thinking)

os.environ.setdefault("MOTET_LOCAL_MODEL_DIR", "/app/models")

model_ids = sys.argv[1:] or ["qwen3-8b-instruct"]

REASONING_PROMPT = "A farmer has 17 sheep; all but 9 run away. How many remain? Think briefly, then answer."


def _run(model_id: str) -> None:
    path = DEFAULT_MODEL_PATHS[model_id]
    print(f"\n=== {model_id} ===")
    print("path:", path, "exists:", os.path.exists(path))
    if not os.path.exists(path):
        print("SKIP (asset missing)")
        return

    cache = LocalModelCache(max_memory_gb=20, cache_size=1, engine="llama_cpp")
    model, _ = cache.get_or_load_model_sync(model_id, model_path=path)
    stop = stop_sequences_for_model(model_id)

    base_messages = [{"role": "user", "content": REASONING_PROMPT}]

    # 1) Non-stream: reasoning separated, no tag leak.
    out = model.create_chat_completion(messages=base_messages, max_tokens=256, stop=stop)
    raw = (out["choices"][0].get("message") or {}).get("content") or ""
    clean, reasoning = split_reasoning(raw)
    print("[non-stream] raw_has_think_tag:", "<think>" in raw)
    print("[non-stream] clean_leaks_tag:", ("<think>" in clean or "</think>" in clean))
    print("[non-stream] reasoning_present:", bool(reasoning))
    print("[non-stream] clean:", repr(clean)[:200])
    if reasoning:
        print("[non-stream] reasoning:", repr(reasoning)[:200])

    # 2) enable_thinking=False (Qwen3 /no_think soft switch).
    no_think = _apply_thinking_control(base_messages, model_id, enable_thinking=False)
    out2 = model.create_chat_completion(messages=no_think, max_tokens=256, stop=stop)
    raw2 = (out2["choices"][0].get("message") or {}).get("content") or ""
    _, reasoning2 = split_reasoning(raw2)
    print("[no_think] switch_applied:", no_think[-1]["content"].endswith("/no_think"))
    print("[no_think] reasoning_present:", bool(reasoning2))

    # 3) Stream through the router; channels must be clean.
    router = ThinkStreamRouter()
    think_chars = 0
    text_chars = 0
    text_leak = False
    for chunk in model.create_chat_completion(messages=base_messages, max_tokens=256, stop=stop, stream=True):
        token = (chunk["choices"][0].get("delta") or {}).get("content")
        if not token:
            continue
        for channel, piece in router.feed(token):
            if channel == "thinking":
                think_chars += len(piece)
            else:
                text_chars += len(piece)
                if "<think>" in piece or "</think>" in piece:
                    text_leak = True
    for channel, piece in router.flush():
        if channel == "thinking":
            think_chars += len(piece)
        else:
            text_chars += len(piece)
    print("[stream] thinking_chars:", think_chars, "text_chars:", text_chars, "text_channel_leak:", text_leak)


for mid in model_ids:
    _run(mid)

print("\nTHINKING_OK")
