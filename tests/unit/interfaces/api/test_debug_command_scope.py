"""
Motet - Debug Command Scope Filter Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-18

Description:
    Unit tests for manage-app Memory index SCAN patterns after ADR-0095
    prefixes and issue #218 collapsed ``mem:`` keys.

Dependencies:
    - pytest
    - motet.interfaces.api.v1.debug

Usage:
    pytest tests/unit/interfaces/api/test_debug_command_scope.py -q
"""

from __future__ import annotations

from motet.interfaces.api.v1.debug import _memory_index_scan_patterns


def test_memory_index_scan_patterns_cover_prefixed_and_legacy() -> None:
    assert _memory_index_scan_patterns("acme", "default") == (
        "acme:mem:default:idx:global",
    )
    patterns = _memory_index_scan_patterns(None, None)
    assert "*:mem:*:idx:global" in patterns
    assert "*:imf:mem:*:*:idx:global" not in patterns
