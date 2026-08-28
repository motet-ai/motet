"""
Motet - Execution output capture limits

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-14

Description:
    Truncate stdout/stderr pairs to a max byte budget (shared by execution backends).
    Prefer stream tails so CLI summaries (e.g. pytest) survive truncation.

Dependencies:
    - typing

Usage:
    from motet.core.execution.capture import truncate_output_pair

    out, err, otrunc, etrunc = truncate_output_pair(stdout, stderr, max_total=1_048_576)
"""

from __future__ import annotations

from typing import Tuple


def truncate_output_pair(
    stdout: str, stderr: str, max_total: int
) -> Tuple[str, str, bool, bool]:
    """Truncate stdout/stderr to ~``max_total`` bytes, preferring stream tails.

    Pytest and most CLI tools put the pass/fail summary at the end of stdout.
    Keeping only the head discarded those summaries for long suites (ADR-0122
    app-builder test gate). When truncating a stream, keep a short head for
    context and the remainder as a tail.

    Note: stderr is guaranteed a floor of 1024 bytes even when stdout consumed
    the whole budget, so the combined result may exceed ``max_total`` by up to
    ~1 KiB (plus truncation markers). Deliberate trade: never lose stderr
    entirely, since it usually carries the failure reason.
    """
    out_trunc = err_trunc = False
    if len(stdout) + len(stderr) <= max_total:
        return stdout, stderr, out_trunc, err_trunc

    half = max(max_total // 2, 1024)
    stdout, out_trunc = _truncate_prefer_tail(stdout, half)
    remain = max(max_total - len(stdout), 1024)
    stderr, err_trunc = _truncate_prefer_tail(stderr, remain)
    return stdout, stderr, out_trunc, err_trunc


def _truncate_prefer_tail(text: str, limit: int) -> Tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    marker = "\n...[truncated]...\n"
    if limit <= len(marker) + 64:
        return text[-limit:], True
    keep_head = min(256, max(0, (limit - len(marker)) // 4))
    keep_tail = limit - len(marker) - keep_head
    return text[:keep_head] + marker + text[-keep_tail:], True


__all__ = ["truncate_output_pair"]
