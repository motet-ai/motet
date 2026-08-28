"""
Motet - Events SSE Tenant Filter Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-19

Description:
    Unit tests for GET /api/v1/events fail-closed tenant filtering (issue #233).
    Missing or mismatched data.tenant_id must not reach a subscriber.

Dependencies:
    - pytest
    - motet.interfaces.api.v1.events

Usage:
    pytest tests/unit/interfaces/api/test_events_sse_filter.py -q
"""

from __future__ import annotations

from motet.interfaces.api.v1.events import event_matches_caller_tenant


def test_sse_keeps_matching_tenant() -> None:
    event = {"kind": "command_started", "data": {"tenant_id": "acme"}}
    assert event_matches_caller_tenant(event, "acme") is True


def test_sse_drops_mismatched_tenant() -> None:
    event = {"kind": "command_started", "data": {"tenant_id": "other"}}
    assert event_matches_caller_tenant(event, "acme") is False


def test_sse_drops_missing_tenant() -> None:
    assert event_matches_caller_tenant({"kind": "circuit_breaker", "data": {}}, "acme") is False
    assert event_matches_caller_tenant({"kind": "circuit_breaker"}, "acme") is False
    assert event_matches_caller_tenant({"kind": "x", "data": {"tenant_id": ""}}, "acme") is False
    assert event_matches_caller_tenant({"kind": "x", "data": {"tenant_id": None}}, "acme") is False


def test_sse_drops_blank_caller_mismatch() -> None:
    event = {"kind": "command_started", "data": {"tenant_id": "acme"}}
    assert event_matches_caller_tenant(event, "") is False
