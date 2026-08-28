"""
Motet - CLI Interface Wrapper

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-05

Description:
    Lazy re-export of the live Motet CLI entry point (`motet.cli.main` /
    `motet-cli`). Kept so `from motet.interfaces.cli import cli_app` continues
    to work for older import paths without forcing motet_sdk into API images
    that only load the HTTP stack.

Dependencies:
    - motet.cli: modular CLI package (motet_sdk.cli-backed) — loaded only when
      cli_app is accessed

Usage:
    from motet.interfaces.cli import cli_app

    cli_app()  # same Click group as motet-cli

Notes:
    - Prefer `motet-cli` or `python -m motet.cli` for operators.
    - Importing this module must not require motet_sdk (API containers do not
      install it). Resolution is deferred until cli_app is used.
"""

from __future__ import annotations

from typing import Any

__all__ = ["cli_app"]


def __getattr__(name: str) -> Any:
    if name == "cli_app":
        from motet.cli import main as cli_app

        return cli_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
