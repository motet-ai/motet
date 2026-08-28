"""
Motet - Embedding Server Entrypoint

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Command-line entrypoint for running the text embedding server with
    Uvicorn. This keeps local and container startup simple without adding a new
    console script.

Dependencies:
    - argparse for command-line options
    - os for environment defaults
    - uvicorn for serving the FastAPI app

Usage:
    python -m motet.core.embedding.server --host 0.0.0.0 --port 8091

Notes:
    - The app import string is used so Uvicorn can manage application loading.
"""

from __future__ import annotations

import argparse
import os

import uvicorn


def main() -> None:
    """Run the embedding server."""

    parser = argparse.ArgumentParser(description="Run Motet embedding server")
    parser.add_argument("--host", default=os.getenv("MOTET_EMBEDDING_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MOTET_EMBEDDING_PORT", "8091")))
    parser.add_argument("--reload", action="store_true", default=False)
    args = parser.parse_args()

    uvicorn.run(
        "motet.core.embedding.server.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
