"""
Motet - ADR-0117 Jinja-Primary Chat Template Smoke Test

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-04

Description:
Verifies the ADR-0117 jinja-primary chat-template load path for the refreshed
local model tier. Loads each refreshed GGUF and reports whether the embedded
Jinja template was selected (primary path) vs. the pinned per-family fallback,
then runs a tiny inference to confirm a clean, stop-terminated response (no
runaway generation, per the ADR-0114 stop-sequence safety net).

Dependencies:
- motet.core.models.local.model_cache: chat-format/stop resolution + cache loader
- motet.core.models.local.inference_manager: DEFAULT_MODEL_PATHS for asset paths
- llama-cpp-python (>=0.3.24): embedded Jinja template support

Usage:
    # In the local-inference container (CPU):
    docker exec -e PYTHONPATH=/app motet_dev-local-inference-1 \\
        python /app/tests/manual/_adr0117_smoke.py gemma-4-e4b

    # On host (Metal), for larger models that exceed container memory:
    MOTET_LOCAL_MODEL_DIR="$(pwd)/models" PYTHONPATH="$(pwd)" \\
        python3 tests/manual/_adr0117_smoke.py gemma-4-26b-a4b

Notes:
- Pass model ids as args, or default to the refreshed small tier.
- The 26B MoE exceeds the CPU dev container's memory cap; run it on the host.
"""
import os
import sys

from motet.core.models.local.model_cache import (
    LocalModelCache,
    chat_format_for_model,
)
from motet.core.models.local.profiles import profile_for_model
from motet.core.models.local.inference_manager import DEFAULT_MODEL_PATHS


def stop_sequences_for_model(model_id):
    return profile_for_model(model_id).stop_sequences()

os.environ.setdefault("MOTET_LOCAL_MODEL_DIR", "/app/models")

model_ids = sys.argv[1:] or [
    "gemma-4-e4b",
    "llama-3.1-8b-instruct",
    "qwen3-8b-instruct",
    "ministral-3-8b-instruct",
]

for model_id in model_ids:
    path = DEFAULT_MODEL_PATHS[model_id]
    print(f"\n=== {model_id} ===")
    print("path:", path, "exists:", os.path.exists(path))
    if not os.path.exists(path):
        print("SKIP (asset missing)")
        continue

    cache = LocalModelCache(max_memory_gb=20, cache_size=1, engine="llama_cpp")
    model, meta = cache.get_or_load_model_sync(model_id, model_path=path)

    md = getattr(model, "metadata", {}) or {}
    print("embedded_jinja_template_present:", bool(md.get("tokenizer.chat_template")))
    print("active chat_format:", getattr(model, "chat_format", None))
    print("active chat_handler:", type(getattr(model, "chat_handler", None)).__name__)
    print("pinned fallback would be:", chat_format_for_model(model_id))

    stop = stop_sequences_for_model(model_id)
    out = model.create_chat_completion(
        messages=[{"role": "user", "content": "Reply with exactly: hello world"}],
        max_tokens=64,
        stop=stop,
    )
    choice = out["choices"][0]
    print("finish_reason:", choice.get("finish_reason"))
    print("output:", repr(choice["message"]["content"])[:300])
    print("output_tokens:", out.get("usage", {}).get("completion_tokens"))

print("\nSMOKE_OK")
