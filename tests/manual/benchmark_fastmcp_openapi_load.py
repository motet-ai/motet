"""
Motet - FastMCP OpenAPI Load Benchmark

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-19

Description:
    Manual benchmark script to isolate whether a particular OpenAPI spec URL is slow
    to download/parse or slow to convert into FastMCP tools.

    This is useful when worker startup/discovery appears to time out and you suspect
    OpenAPI adapter initialization (spec download + FastMCP.from_openapi) is the cause.

Dependencies:
    - httpx: HTTP client used by FastMCP and the adapter loader
    - fastmcp: OpenAPI-to-tools generator
    - motet.core.tools.mcp_adapters.openapi_adapter_server: shared spec loader with Redis caching
    - motet.core.distributed.redis_manager: optional, used for cache inspection/clearing

Usage:
    # Compare apis.guru spec vs official Zoom Meetings spec (no server run)
    python tests/manual/benchmark_fastmcp_openapi_load.py --compare-zoom

    # Benchmark a single URL
    python tests/manual/benchmark_fastmcp_openapi_load.py \
      --openapi-url "https://developers.zoom.us/api-hub/meetings/ma/master.json" \
      --base-url "https://api.zoom.us/v2" \
      --name "Zoom API" \
      --clear-cache

Notes:
    - This script does NOT start an MCP server; it only measures load/parse/build time.
    - Cache behavior matches the adapter:
      - cache key: openapi:parsed:<md5(url)>
      - cache only persisted when ETag/Last-Modified is present on HEAD.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import time
from typing import Any, Dict, Optional, Tuple

import httpx
from fastmcp import FastMCP

from motet.core.tools.mcp_adapters.openapi_adapter_server import load_spec


def _try_get_redis_client() -> Optional[Any]:
    """Return a sync redis client if available; otherwise None."""
    try:
        from motet.core.distributed.redis_manager import get_sync_redis_client

        return get_sync_redis_client("openapi_adapter_cache")
    except Exception:
        return None


def _cache_keys_for_url(url: str) -> Tuple[str, str]:
    cache_key = f"openapi:parsed:{hashlib.md5(url.encode()).hexdigest()}"
    etag_key = f"{cache_key}:etag"
    return cache_key, etag_key


def _clear_cache_for_url(url: str) -> None:
    redis_client = _try_get_redis_client()
    if not redis_client:
        print("[cache] Redis not available; cannot clear cache")
        return

    cache_key, etag_key = _cache_keys_for_url(url)
    deleted = redis_client.delete(cache_key, etag_key)
    print(f"[cache] deleted keys for url={url!r} deleted_count={deleted}")


def _cache_status_for_url(url: str) -> None:
    redis_client = _try_get_redis_client()
    if not redis_client:
        print("[cache] Redis not available; cannot inspect cache")
        return

    cache_key, etag_key = _cache_keys_for_url(url)
    # Note: cache values are pickled bytes. Depending on how the redis client was
    # configured, a plain `.get()` may attempt UTF-8 decoding (decode_responses=True)
    # and raise UnicodeDecodeError. Use disable_decoding=True when available.
    def _raw_get(key: str) -> Optional[bytes]:
        try:
            val = redis_client.get(key)
            if val is None:
                return None
            if isinstance(val, bytes):
                return val
            # If decode_responses=True, redis-py returns str; this is not expected for pickles.
            return val.encode("utf-8", errors="replace")
        except UnicodeDecodeError:
            try:
                # redis-py supports disable_decoding in execute_command
                return redis_client.execute_command("GET", key, disable_decoding=True)
            except Exception:
                return None

    def _raw_get_text(key: str) -> Optional[str]:
        try:
            val = redis_client.get(key)
            if val is None:
                return None
            if isinstance(val, bytes):
                return val.decode(errors="replace")
            return str(val)
        except UnicodeDecodeError:
            try:
                raw = redis_client.execute_command("GET", key, disable_decoding=True)
                if raw is None:
                    return None
                if isinstance(raw, bytes):
                    return raw.decode(errors="replace")
                return str(raw)
            except Exception:
                return None

    cached_spec_data = _raw_get(cache_key)
    cached_etag_text = _raw_get_text(etag_key)

    spec_bytes = len(cached_spec_data) if cached_spec_data else 0
    etag_value = cached_etag_text

    print(
        f"[cache] url={url!r} cache_key={cache_key!r} has_spec={bool(cached_spec_data)} "
        f"spec_bytes={spec_bytes} has_etag={bool(etag_value)} etag={etag_value!r}"
    )


def benchmark_openapi_to_fastmcp(
    *,
    openapi_url: str,
    base_url: str,
    name: str,
    clear_cache: bool,
) -> None:
    print("=" * 88)
    print(f"Benchmark: {name}")
    print(f"openapi_url: {openapi_url}")
    print(f"base_url:    {base_url}")

    if clear_cache:
        _clear_cache_for_url(openapi_url)

    _cache_status_for_url(openapi_url)

    t0 = time.perf_counter()
    spec: Dict[str, Any] = load_spec(openapi_url, None)
    t1 = time.perf_counter()

    paths_count = len(spec.get("paths", {}) or {})
    print(f"[timing] load_spec: {t1 - t0:.3f}s (paths={paths_count})")

    # FastMCP uses an AsyncClient; we do not run it.
    client = httpx.AsyncClient(
        base_url=base_url,
        headers={"User-Agent": "Motet-FastMCP-Benchmark/1.0"},
        timeout=60.0,
        follow_redirects=True,
    )

    try:
        t2 = time.perf_counter()
        _mcp = FastMCP.from_openapi(openapi_spec=spec, client=client, name=name)
        t3 = time.perf_counter()
        print(f"[timing] FastMCP.from_openapi: {t3 - t2:.3f}s")
        print(f"[timing] total: {t3 - t0:.3f}s")
    finally:
        # Close client.
        try:
            asyncio.run(client.aclose())
        except Exception:
            pass

    _cache_status_for_url(openapi_url)


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark OpenAPI spec load + FastMCP.from_openapi")
    parser.add_argument("--openapi-url", help="URL to OpenAPI spec")
    parser.add_argument("--base-url", default="https://api.zoom.us/v2", help="Base URL passed to httpx")
    parser.add_argument("--name", default="Zoom API", help="FastMCP server name")
    parser.add_argument("--clear-cache", action="store_true", help="Clear Redis cache for URL before benchmarking")
    parser.add_argument(
        "--compare-zoom",
        action="store_true",
        help="Compare apis.guru zoom spec vs official Zoom Meetings spec",
    )

    args = parser.parse_args()

    if args.compare_zoom:
        benchmark_openapi_to_fastmcp(
            openapi_url="https://api.apis.guru/v2/specs/zoom.us/2.0.0/openapi.json",
            base_url=args.base_url,
            name="Zoom API (apis.guru)",
            clear_cache=args.clear_cache,
        )
        benchmark_openapi_to_fastmcp(
            openapi_url="https://developers.zoom.us/api-hub/meetings/ma/master.json",
            base_url=args.base_url,
            name="Zoom API (official meetings spec)",
            clear_cache=args.clear_cache,
        )
        return

    if not args.openapi_url:
        raise SystemExit("Provide --openapi-url or use --compare-zoom")

    benchmark_openapi_to_fastmcp(
        openapi_url=args.openapi_url,
        base_url=args.base_url,
        name=args.name,
        clear_cache=args.clear_cache,
    )


if __name__ == "__main__":
    main()
