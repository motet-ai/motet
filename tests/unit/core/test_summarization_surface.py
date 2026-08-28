"""
Motet - Memory Summarization Surface Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Guards #180 Option A: the digest/summarize product surface is gone.
    No background job, no interval knob, no enable flag, no manager method.

Usage:
    pytest tests/unit/core/test_summarization_surface.py
"""

from __future__ import annotations

import motet.server as server
from motet.core.config import Config
from motet.core.memory.manager import MemoryManager


def test_summarization_surface_removed() -> None:
    cfg = Config()
    assert not hasattr(cfg, "enable_summarization")
    assert not hasattr(cfg, "summarization_interval_seconds")
    assert not hasattr(server, "_maybe_summarization_job")
    assert not hasattr(MemoryManager, "summarize_and_store")
