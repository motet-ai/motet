"""
Motet - Command Stream Encryption Tests

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Unit tests for ADR-0056 command streaming encryption. Ensures that
    MotetContext.stream_event() writes encrypted-only payload fields into Redis Streams.

Dependencies:
    - pytest: test runner
    - unittest.mock: patching encryption service and redis client

Usage:
    pytest tests/unit/core/orchestration/test_command_stream_encryption.py
"""

from __future__ import annotations

import base64
import os
from typing import Any, Dict
from unittest.mock import MagicMock, patch

from motet.core.commands.decorator import MotetContext


class DummyEncryptionService:
    """Minimal KEK wrapper used by envelope_helper (wrap/unwrap only)."""

    def wrap_key(self, dek: bytes, tenant_id: str) -> Dict[str, Any]:
        return {
            "wrapped_key": base64.b64encode(dek).decode("ascii"),
            "iv": base64.b64encode(b"0123456789ab").decode("ascii"),
            "tenant_id": tenant_id,
            "encryption_version": "aes-256-gcm-v1",
        }

    def unwrap_key(self, wrapped_blob: Dict[str, Any]) -> bytes:
        return base64.b64decode(wrapped_blob["wrapped_key"])


def test_stream_event_writes_encrypted_envelope_not_plaintext():
    # Minimal MotetContext with required fields and a mocked redis client
    ctx = MotetContext(
        redis=MagicMock(),
        task_id="task-1",
        command_id="cmd-1",
        tenant_id="tenant-a",
        motet_id="motet-a",
    )

    # Token events are buffered; force an immediate flush by shrinking the buffer size.
    os.environ["MOTET_STREAM_TOKEN_MAX_CHARS"] = "1"

    with patch("motet.core.security.encryption_service.get_encryption_service", return_value=DummyEncryptionService()):
        ctx.stream_event("token", data="secret-token")

    assert ctx.redis.xadd.called
    _, kwargs = ctx.redis.xadd.call_args
    # stream_key is positional in call; fields are second positional
    args = ctx.redis.xadd.call_args[0]
    assert len(args) >= 2
    fields = args[1]

    assert fields["event"] == "token"
    assert "_envelope" in fields
    assert "data" not in fields  # no plaintext token


def test_stream_event_writes_parent_agent_id_plaintext():
    ctx = MotetContext(
        redis=MagicMock(),
        task_id="task-1",
        command_id="cmd-1",
        tenant_id="tenant-a",
        motet_id="motet-a",
        metadata={
            "agent_id": "core.default.spawn-1",
            "parent_agent_id": "core.default",
        },
    )
    os.environ["MOTET_STREAM_TOKEN_MAX_CHARS"] = "1"

    with patch(
        "motet.core.security.encryption_service.get_encryption_service",
        return_value=DummyEncryptionService(),
    ):
        ctx.stream_event("token", data="secret-token")

    fields = ctx.redis.xadd.call_args[0][1]
    assert fields["agent_id"] == "core.default.spawn-1"
    assert fields["parent_agent_id"] == "core.default"
    assert "data" not in fields


