"""
Motet - Execution capture truncation tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-14

Description:
    Unit tests for truncate_output_pair preferring stream tails so pytest
    summaries survive capture limits (ADR-0122 test gate).

Usage:
    pytest tests/unit/core/test_execution_capture.py -q
"""

from __future__ import annotations

from motet.core.execution.capture import truncate_output_pair


def test_truncate_keeps_pytest_summary_in_tail() -> None:
    head = "x" * 10_000
    summary = "============================== 21 passed in 0.35s ==============================\n"
    stdout = head + summary
    stderr = "compose noise\n"
    out, err, otrunc, etrunc = truncate_output_pair(stdout, stderr, max_total=4096)
    assert otrunc is True
    assert "21 passed" in out
    assert "...[truncated]..." in out
    assert "compose noise" in err
