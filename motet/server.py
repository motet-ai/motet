"""
Motet - Main Server

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Main server entry point for the Motet distributed framework.
    Provides HTTP API server with FastAPI and comprehensive startup
    validation. Includes configuration validation, health checks, and
    distributed service coordination.

Dependencies:
    - uvicorn: ASGI server for FastAPI
    - argparse: Command-line argument parsing
    - FastAPI: HTTP API framework

Usage:
    # Run server
    python -m motet.server --host 0.0.0.0 --port 8000

    # Validate configuration
    python -m motet.server --validate

    # Development mode with reload
    python -m motet.server --reload

Notes:
    - Provides main HTTP API server for distributed AI framework
    - Memory consolidation is a separate path from this server entrypoint
    - Supports configuration validation and health checks
    - Includes distributed service coordination
    - Supports development mode with auto-reload
    - Integrates with FastAPI and uvicorn
    - Includes comprehensive startup validation
"""

from __future__ import annotations

import argparse
import os
import sys

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Motet API server")
    parser.add_argument("--host", default=os.getenv("MOTET_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MOTET_PORT", "8000")))
    parser.add_argument("--reload", action="store_true", default=False)
    parser.add_argument("--validate", action="store_true", default=False, help="Validate provider config and exit")
    args = parser.parse_args()

    if args.validate:
        from .core.config import Config
        cfg = Config()
        ok = _startup_validation(cfg)
        if not ok:
            print("Startup validation detected issues.")
            sys.exit(1)
        print("Startup validation OK")
        return

    uvicorn.run(
        "motet.interfaces.http:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()


def _startup_validation(cfg):
    ok = True
    if not (cfg.openai_api_key or cfg.anthropic_api_key):
        print("Warning: No provider API key configured (MOTET_OPENAI_API_KEY or MOTET_ANTHROPIC_API_KEY)")
        ok = False
    if cfg.enable_memory and cfg.memory_backend == "redis" and cfg.redis_url:
        try:
            import redis
            redis.Redis.from_url(cfg.redis_url).ping()
        except Exception as exc:
            print(f"Warning: Redis not reachable: {exc}")
            ok = False
    if getattr(cfg, "enable_vector_memory", False):
        try:
            from .core.memory import store_registry
            vs = store_registry.build(
                "vector",
                "valkey",
                index_name=getattr(cfg, "memory_vector_valkey_index", None),
                key_prefix=getattr(cfg, "memory_vector_valkey_prefix", None),
                redis_client_id=getattr(cfg, "memory_vector_redis_client_id", "memory_vector_valkey"),
                embedding_model=cfg.embedding_model,
                enable_embedding_cache=False,
                enable_result_cache=False,
            )
            _ = vs._index_name
        except Exception as exc:
            print(f"Warning: Vector store init failed: {exc}")
            ok = False
    return ok

