"""
Motet - Unit tests for commands API conversation_id allocation

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-14

Description:
Unit tests for POST /api/v1/commands/.../execute conversation_id resolution:
clients may omit an id; the API allocates ``api-exec-<uuid>`` so cost events
and workflow child steps share a correlation id.

Dependencies:
- pytest
- motet.interfaces.api.v1.commands

Usage:
  pytest tests/unit/interfaces/api/test_commands_conversation_id.py -q

Notes:
- Pure helper coverage; does not hit Redis or workers.
"""

from __future__ import annotations

from motet.interfaces.api.v1.commands import (
    ExecuteCommandRequest,
    _resolve_execute_conversation_id,
)


def test_resolve_execute_conversation_id_uses_client_value():
    req = ExecuteCommandRequest(data={}, conversation_id="  client-cid  ")
    assert _resolve_execute_conversation_id(req) == "client-cid"


def test_resolve_execute_conversation_id_allocates_when_missing():
    req = ExecuteCommandRequest(data={})
    cid = _resolve_execute_conversation_id(req)
    assert cid.startswith("api-exec-")
    assert len(cid) > len("api-exec-")


def test_resolve_execute_conversation_id_allocates_when_blank():
    req = ExecuteCommandRequest(data={}, conversation_id="   ")
    cid = _resolve_execute_conversation_id(req)
    assert cid.startswith("api-exec-")
