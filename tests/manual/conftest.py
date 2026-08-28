"""
Motet - Manual Test Collection Guard

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-04

Description:
    Prevents pytest from auto-collecting anything in tests/manual/. The files
    here are manual verification harnesses, not automated tests: they require
    real model assets (multi-GB GGUFs), running services (Redis, the
    local-inference container, a live API, or a browser), and/or execute
    top-level code on import. Collecting them during a normal `pytest` /
    `pytest tests/` run would import those modules (loading models, hitting
    services) and hang or fail the suite.

    `collect_ignore_glob` excludes every module in this directory from
    collection while leaving them runnable directly, e.g.:

        python tests/manual/test_local_inference.py
        python tests/manual/_adr0117_structured.py gemma-4-e4b

Dependencies:
    - pytest: collection hook (collect_ignore_glob)

Usage:
    Automatic — pytest loads this conftest and skips collection of sibling
    modules. No action needed; run the harnesses directly with `python`.

Notes:
    - conftest.py itself is always loaded by pytest (it is not a collected test
      item), so ignoring "*.py" here does not disable this guard.
    - The colocation under tests/ keeps manual harnesses discoverable next to the
      automated suite without making them part of CI collection.
"""

collect_ignore_glob = ["*.py"]
