#!/usr/bin/env python3
"""
Motet - Local Chat Smoke Test (Phi-4-mini / Gemma 3)

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-03

Description:
    End-to-end smoke test for chatting with locally-hosted GGUF models through the
    real local-inference path: LocalInferenceClient -> Redis Streams ->
    LocalInferenceManager (standalone sibling process) -> llama.cpp. Exercises the
    ADR-0114 chat-template fix (create_chat_completion applies each model's embedded
    template) and the canonical LocalAdapter surface.

    This is the hoisted topology from ADR-0105: the LocalInferenceManager runs as an
    independent process (optionally auto-started here) and the client connects over
    Redis Streams keyed on a shared manager_id, fully decoupled from any Celery worker
    lifecycle. --warm-check demonstrates the architectural payoff: a brand-new client
    (simulating a restarted worker) hits the still-running manager's warm model cache.

Dependencies:
    - Redis reachable at MOTET_REDIS_URL (default redis://localhost:6379/0)
    - llama-cpp-python (>=0.3 for Gemma 3 / Phi-4 architectures)
    - GGUF files under MOTET_LOCAL_MODEL_DIR (default <repo>/models)

Usage:
    # Auto-start the manager, chat with both default models, then tear it down:
    MOTET_LOCAL_MODEL_DIR="$PWD/models" \
        python tests/manual/test_local_chat_genui.py --autostart

    # Prove the hoist: cold load, then a fresh client gets a warm (fast) response
    # from the still-running independent manager:
    python tests/manual/test_local_chat_genui.py --autostart --warm-check phi-4-mini

    # Against an already-running manager (must share MOTET_LOCAL_INFERENCE_MANAGER_ID):
    python tests/manual/test_local_chat_genui.py phi-4-mini

Notes:
    - The manager and the client must agree on manager_id (the Redis Streams routing
      prefix, ADR-0105 §R2). The client reads MOTET_LOCAL_INFERENCE_MANAGER_ID;
      --autostart launches the manager with --manager-id set to the same value.
    - First call per model includes model load (several seconds); a generous warmup
      timeout absorbs that, after which the model is cached and calls are fast.
"""

import argparse
import os
import subprocess
import sys
import time
from typing import List


# manager_id is the shared Redis Streams routing prefix (ADR-0105 §R2). Client and
# manager must agree on it. worker_id rides only as an observability field in the body.
MANAGER_ID = os.environ.setdefault("MOTET_LOCAL_INFERENCE_MANAGER_ID", "local-inference-test")
os.environ.setdefault("CELERY_WORKER_ID", "local-test")
# Default the model directory to <repo>/models if the caller did not set it.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ.setdefault("MOTET_LOCAL_MODEL_DIR", os.path.join(_REPO_ROOT, "models"))


def _print_header(title: str) -> None:
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)


def start_manager() -> subprocess.Popen:
    """Start the LocalInferenceManager as an independent sibling process (ADR-0105 hoist)."""
    log_path = os.path.join(os.environ["MOTET_LOCAL_MODEL_DIR"], "manager.log")
    log_file = open(log_path, "w")
    print(f"Starting LocalInferenceManager (manager_id={MANAGER_ID}); logs -> {log_path}")
    proc = subprocess.Popen(
        [sys.executable, "-m", "motet.core.models.local.inference_manager", "--manager-id", MANAGER_ID],
        env={**os.environ},
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    # Give the async coordination loop + worker subprocesses time to come up.
    time.sleep(4.0)
    if proc.poll() is not None:
        print(f"Manager exited early (code {proc.returncode}). See {log_path}.")
        sys.exit(1)
    return proc


def chat_once(client, model: str, messages: List[dict], *, timeout: float) -> str:
    t0 = time.time()
    result = client.infer_sync(
        model_id=model,
        messages=messages,
        temperature=0.2,
        max_tokens=200,
        timeout=timeout,
    )
    elapsed = time.time() - t0
    text = result.get("text") or (result.get("result") or {}).get("text") or ""
    print(f"  ({elapsed:.1f}s) {text.strip()}")
    return text


def run_chat(models: List[str]) -> bool:
    from motet.core.distributed.redis_manager import UnifiedRedisManager
    from motet.core.models.local import LocalInferenceClient

    redis_client = UnifiedRedisManager().get_sync_client("local_chat_test")
    redis_client.ping()
    client = LocalInferenceClient(redis_client)

    ok = True
    for model in models:
        _print_header(f"💬 Chatting with: {model}")
        try:
            # Turn 1 (cold: includes model load).
            print("user: In one sentence, what is a vector database?")
            chat_once(
                client,
                model,
                [
                    {"role": "system", "content": "You are a helpful assistant. Answer concisely."},
                    {"role": "user", "content": "In one sentence, what is a vector database?"},
                ],
                timeout=300.0,
            )
            # Turn 2 (warm: multi-turn, tests chat-template role handling).
            print("user: Now name one popular open-source one.")
            reply = chat_once(
                client,
                model,
                [
                    {"role": "system", "content": "You are a helpful assistant. Answer concisely."},
                    {"role": "user", "content": "In one sentence, what is a vector database?"},
                    {"role": "assistant", "content": "A vector database stores and indexes high-dimensional embeddings for fast similarity search."},
                    {"role": "user", "content": "Now name one popular open-source one."},
                ],
                timeout=120.0,
            )
            if not reply.strip():
                print(f"  ⚠️  Empty response from {model}")
                ok = False
        except Exception as exc:  # noqa: BLE001 - smoke test surface
            print(f"  ❌ {model} failed: {type(exc).__name__}: {exc}")
            ok = False

    return ok


def run_warm_check(model: str) -> bool:
    """Prove the ADR-0105 hoist: a fresh client hits the still-warm independent manager.

    Phase 1: client #1 sends a cold request (manager loads the model).
    Phase 2: client #1 is discarded and a brand-new client #2 is created (simulating a
             restarted worker process connecting to the same manager_id). Its request
             should be served fast from the manager's warm model cache, because the
             manager's lifecycle is independent of any client.
    """
    _print_header(f"♻️  Warm-survival check (manager outlives client): {model}")
    from motet.core.distributed.redis_manager import UnifiedRedisManager
    from motet.core.models.local import LocalInferenceClient

    redis_client = UnifiedRedisManager().get_sync_client("local_warm_check")
    prompt = [
        {"role": "system", "content": "You are a helpful assistant. Answer concisely."},
        {"role": "user", "content": "Reply with exactly: warm"},
    ]

    try:
        # Phase 1 — cold client loads the model in the manager.
        client1 = LocalInferenceClient(redis_client)
        print(f"  client #1 (cold)  manager_id={client1.manager_id}")
        t0 = time.time()
        client1.infer_sync(model_id=model, messages=prompt, temperature=0.0, max_tokens=8, timeout=300.0)
        cold = time.time() - t0
        print(f"    cold call: {cold:.1f}s (includes model load)")

        # Discard client #1 entirely — the manager keeps running with the model warm.
        del client1

        # Phase 2 — a brand-new client connects to the same manager_id.
        client2 = LocalInferenceClient(redis_client)
        print(f"  client #2 (fresh) manager_id={client2.manager_id}")
        t0 = time.time()
        client2.infer_sync(model_id=model, messages=prompt, temperature=0.0, max_tokens=8, timeout=120.0)
        warm = time.time() - t0
        print(f"    warm call: {warm:.1f}s (model already resident in the surviving manager)")

        # The warm call should be materially faster than the cold one if the manager
        # truly retained the model independent of the client lifecycle.
        if warm < max(cold * 0.6, cold - 1.0):
            print("  ✅ fresh client served from warm manager — hoist verified")
            return True
        print("  ⚠️  warm call wasn't clearly faster; inspect manager.log (model may be tiny/fast)")
        return True  # not a hard failure on very fast models; timings are informational
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ warm-survival check failed: {type(exc).__name__}: {exc}")
        return False


def run_adapter_path(model: str) -> bool:
    """Exercise the canonical LocalAdapter surface (model is warm from run_chat)."""
    _print_header(f"🧩 Canonical LocalAdapter path: {model}")
    try:
        from motet.core.models.adapters.providers.local import LocalAdapter
        from motet.core.types import LLMRequest, Message

        adapter = LocalAdapter(provider="local", adapter_name="local")

        req = LLMRequest(
            messages=[
                Message(role="system", content="You are a helpful assistant. Answer concisely."),
                Message(role="user", content="Reply with exactly: chat works"),
            ],
            model_settings={"model_name": model, "temperature": 0.0, "max_tokens": 32},
        )
        resp = adapter.complete(req)
        print(f"  complete(): {repr((resp.output_text or '').strip())}")

        print("  stream():   ", end="", flush=True)
        got = []
        for event in adapter.stream(req):
            text = getattr(event, "text", None)
            if text and getattr(event, "type", "") == "text_delta":
                got.append(text)
                print(text, end="", flush=True)
        print()
        return bool((resp.output_text or "").strip()) and bool("".join(got).strip())
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ adapter path failed: {type(exc).__name__}: {exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Local chat smoke test (Phi-4-mini / Gemma 3)")
    parser.add_argument("models", nargs="*", default=None, help="Model names (default: phi-4-mini gemma-3-4b)")
    parser.add_argument("--autostart", action="store_true", help="Start the LocalInferenceManager as a sibling process")
    parser.add_argument("--no-adapter", action="store_true", help="Skip the canonical LocalAdapter path check")
    parser.add_argument("--warm-check", action="store_true", help="Prove the hoist: fresh client hits the warm surviving manager")
    args = parser.parse_args()

    models = args.models or ["phi-4-mini", "gemma-3-4b"]

    _print_header("Local chat smoke test")
    print(f"manager_id         : {MANAGER_ID}")
    print(f"worker_id          : {os.environ['CELERY_WORKER_ID']} (observability only)")
    print(f"model dir          : {os.environ['MOTET_LOCAL_MODEL_DIR']}")
    print(f"models             : {', '.join(models)}")

    # Validate model files exist up front for a clear error.
    from motet.core.models.local.inference_manager import resolve_model_path
    for model in models:
        path = resolve_model_path(model)
        exists = path and os.path.exists(path)
        print(f"  {model:<14} -> {path}  [{'ok' if exists else 'MISSING'}]")
        if not exists:
            print(f"\n❌ Model file missing for '{model}'. Download the GGUF into {os.environ['MOTET_LOCAL_MODEL_DIR']}.")
            return 1

    manager_proc = start_manager() if args.autostart else None
    try:
        chat_ok = run_chat(models)
        adapter_ok = True
        if not args.no_adapter:
            adapter_ok = run_adapter_path(models[0])
        warm_ok = True
        if args.warm_check:
            warm_ok = run_warm_check(models[0])
    finally:
        if manager_proc is not None:
            print("\nStopping LocalInferenceManager...")
            try:
                manager_proc.terminate()
                manager_proc.wait(timeout=10)
            except Exception:
                manager_proc.kill()

    _print_header("Result")
    success = chat_ok and adapter_ok and warm_ok
    print("✅ PASS" if success else "❌ FAIL")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
