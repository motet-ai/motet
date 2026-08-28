"""
Motet - Generative-UI Structured Output Proof (ADR-0114)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-03

Description:
End-to-end proof that LLMRequest.output_contract now flows through the
canonical model path and constrains local decoding to a JSON Schema.

It drives the real LocalAdapter (not the raw client) so the full
focused-slice path is exercised:

    LLMRequest.output_contract
      -> LocalAdapter._structured_output_kwargs
      -> LocalInferenceClient.infer(json_schema=...)
      -> LocalInferenceManager._build_llama_grammar (JSON Schema -> GBNF)
      -> grammar-constrained decoding (guaranteed-parseable output)

The script compares an UNCONSTRAINED run (no contract) against a
CONSTRAINED run (output_contract with a tiny generative-UI DSL schema) and
asserts the constrained output parses as JSON and satisfies the schema's
required keys + enum.

Dependencies:
- motet.core.models.adapters.providers.local.LocalAdapter
- motet.core.types: LLMRequest, Message, OutputContract
- A running, hoisted local-inference manager (ADR-0105) sharing
  MOTET_LOCAL_INFERENCE_MANAGER_ID and the target GGUF model.

Usage:
    # Inside a worker container (Redis + manager reachable):
    docker exec motet_dev-worker-2-1 \\
        python tests/manual/test_genui_structured_output.py phi-4-mini

Notes:
- The model must advertise CAP_STRUCTURED_OUTPUT (e.g. phi-4-mini, gemma-3-4b);
  otherwise the adapter degrades to unconstrained generation by design.
"""

from __future__ import annotations

import json
import sys
import time

from motet.core.types import LLMRequest, Message, OutputContract
from motet.core.models.adapters.providers.local import LocalAdapter


# A tiny generative-UI DSL: the model must emit exactly this shape.
GENUI_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "component": {"type": "string", "enum": ["card", "list", "form"]},
        "title": {"type": "string"},
        "items": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["component", "title", "items"],
}

PROMPT = (
    "Render a UI for a user's todo list with three tasks: buy milk, walk dog, "
    "pay rent. Respond with the UI spec only."
)


def _run(adapter: LocalAdapter, model: str, *, contract: OutputContract | None) -> tuple[str, float]:
    req = LLMRequest(
        messages=[Message(role="user", content=PROMPT)],
        model_settings={
            "model_name": model,
            "provider": "local",
            "temperature": 0.0,
            "max_tokens": 256,
        },
        output_contract=contract,
    )
    start = time.time()
    resp = adapter.complete(req)
    return (resp.output_text or ""), time.time() - start


def main() -> int:
    model = sys.argv[1] if len(sys.argv) > 1 else "phi-4-mini"
    adapter = LocalAdapter(provider="local", adapter_name="local")

    print(f"== Generative-UI structured output proof (ADR-0114) ==")
    print(f"model: {model}\n")

    print("--- UNCONSTRAINED (output_contract=None) ---")
    free_text, free_dt = _run(adapter, model, contract=None)
    print(f"[{free_dt:.2f}s] {free_text!r}\n")

    print("--- CONSTRAINED (output_contract -> JSON Schema -> GBNF) ---")
    contract = OutputContract(format="json", json_schema=GENUI_SCHEMA, strict=True)
    constrained, c_dt = _run(adapter, model, contract=contract)
    print(f"[{c_dt:.2f}s] {constrained!r}\n")

    # The whole point: constrained output MUST parse and satisfy the schema.
    try:
        parsed = json.loads(constrained)
    except json.JSONDecodeError as exc:
        print(f"FAIL: constrained output is not valid JSON: {exc}")
        return 1

    ok = (
        isinstance(parsed, dict)
        and parsed.get("component") in {"card", "list", "form"}
        and isinstance(parsed.get("title"), str)
        and isinstance(parsed.get("items"), list)
    )
    if not ok:
        print(f"FAIL: constrained output does not satisfy DSL schema: {parsed}")
        return 1

    print("PASS: constrained output parsed and conforms to the generative-UI DSL schema.")
    print(json.dumps(parsed, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
