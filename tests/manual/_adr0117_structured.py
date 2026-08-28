"""
Motet - ADR-0117 Grammar-Constrained Structured-Output Verification

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-04

Description:
Verifies the ADR-0114 grammar-constrained structured-output path on the ADR-0117
refreshed local model tier. The companion smoke test (_adr0117_smoke.py) only
covers load + embedded-Jinja template + stop behavior; this script closes the
remaining gap by exercising JSON-schema-constrained decoding. It mirrors exactly
what LocalInferenceManager does at runtime: compile the request's json_schema to
a GBNF grammar via llama.cpp's LlamaGrammar.from_json_schema and pass grammar= to
create_chat_completion, then assert the output is schema-valid JSON.

Dependencies:
- motet.core.models.local.model_cache: cache loader + stop-sequence resolution
- motet.core.models.local.inference_manager: DEFAULT_MODEL_PATHS for asset paths
- llama-cpp-python (>=0.3.24): LlamaGrammar.from_json_schema + grammar decoding

Usage:
    # Small models in the CPU container:
    docker exec -e PYTHONPATH=/app motet_dev-local-inference-1 \\
        python /app/tests/manual/_adr0117_structured.py gemma-4-e4b

    # 26B MoE on host (Metal):
    MOTET_LOCAL_MODEL_DIR="$(pwd)/models" PYTHONPATH="$(pwd)" \\
        python3 tests/manual/_adr0117_structured.py gemma-4-26b-a4b

Notes:
- Pass model ids as args, or default to the refreshed small tier.
- Exit code is non-zero if any model fails to produce schema-valid JSON.
"""
import json
import os
import sys

from llama_cpp import LlamaGrammar

from motet.core.models.local.model_cache import LocalModelCache
from motet.core.models.local.profiles import profile_for_model
from motet.core.models.local.inference_manager import DEFAULT_MODEL_PATHS


def stop_sequences_for_model(model_id):
    return profile_for_model(model_id).stop_sequences()

os.environ.setdefault("MOTET_LOCAL_MODEL_DIR", "/app/models")

# A small, unambiguous schema with mixed types and a required set.
SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer"},
        "city": {"type": "string"},
    },
    "required": ["name", "age", "city"],
    "additionalProperties": False,
}

PROMPT = (
    "Extract the person as JSON matching the schema. "
    "Text: 'Ada Lovelace is 36 and lives in London.'"
)

model_ids = sys.argv[1:] or [
    "gemma-4-e4b",
    "llama-3.1-8b-instruct",
    "qwen3-8b-instruct",
    "ministral-3-8b-instruct",
]

failures = []

for model_id in model_ids:
    path = DEFAULT_MODEL_PATHS[model_id]
    print(f"\n=== {model_id} ===")
    print("path:", path, "exists:", os.path.exists(path))
    if not os.path.exists(path):
        print("SKIP (asset missing)")
        continue

    cache = LocalModelCache(max_memory_gb=24, cache_size=1, engine="llama_cpp")
    model, _meta = cache.get_or_load_model_sync(model_id, model_path=path)

    # Mirror LocalInferenceManager._build_llama_grammar (ADR-0114).
    grammar = LlamaGrammar.from_json_schema(json.dumps(SCHEMA))
    stop = stop_sequences_for_model(model_id)

    out = model.create_chat_completion(
        messages=[{"role": "user", "content": PROMPT}],
        max_tokens=128,
        stop=stop,
        grammar=grammar,
    )
    choice = out["choices"][0]
    content = choice["message"]["content"]
    print("finish_reason:", choice.get("finish_reason"))
    print("raw output:", repr(content)[:300])

    ok = True
    reason = ""
    try:
        parsed = json.loads(content)
    except Exception as exc:  # noqa: BLE001 - test harness reports + records
        ok, reason = False, f"not valid JSON: {exc}"
        parsed = None

    if ok:
        missing = [k for k in SCHEMA["required"] if k not in parsed]
        extra = [k for k in parsed if k not in SCHEMA["properties"]]
        if missing:
            ok, reason = False, f"missing keys: {missing}"
        elif extra:
            ok, reason = False, f"unexpected keys: {extra}"
        elif not isinstance(parsed.get("age"), int):
            ok, reason = False, f"age not int: {type(parsed.get('age')).__name__}"

    print("schema_valid:", ok if ok else f"FAIL ({reason})")
    if not ok:
        failures.append((model_id, reason))

if failures:
    print("\nSTRUCTURED_FAIL:", failures)
    sys.exit(1)

print("\nSTRUCTURED_OK")
