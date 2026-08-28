"""
Motet - Artifact Preparation Hashing Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-07

Description:
    Unit tests for ADR-0110 hashing helpers used to make artifact preparation
    cache keys depend on source bytes, strategy versions, and canonical config.

Dependencies:
    - pytest for assertions
    - artifact preparation hashing helpers

Usage:
    pytest tests/unit/core/artifacts/preparation/test_hashing.py

Notes:
    - These tests protect cache invalidation invariants for chunk preparation.
"""

from __future__ import annotations

import hashlib

from motet.core.artifacts.preparation.hashing import (
    chunk_cache_key,
    effective_source_content_hash,
    source_bytes_sha256,
)
from motet.core.artifacts.preparation.strategies.text import chunk_text_to_prepared_chunks


def test_effective_source_content_hash_prefers_declared_checksum() -> None:
    assert effective_source_content_hash(declared_hash=" declared ", payload_bytes=b"changed") == "declared"


def test_effective_source_content_hash_hashes_raw_source_bytes_when_unset() -> None:
    payload = b"raw\nsource\r\nbytes"

    assert effective_source_content_hash(declared_hash=None, payload_bytes=payload) == hashlib.sha256(payload).hexdigest()
    assert source_bytes_sha256(payload) == hashlib.sha256(payload).hexdigest()


def test_chunk_cache_key_changes_with_strategy_version_and_config() -> None:
    base = chunk_cache_key(
        source_content_hash="source",
        strategy_id="text_default",
        strategy_version="1.0.0",
        canonical_config_hash="cfg-a",
    )
    changed_version = chunk_cache_key(
        source_content_hash="source",
        strategy_id="text_default",
        strategy_version="1.0.1",
        canonical_config_hash="cfg-a",
    )
    changed_config = chunk_cache_key(
        source_content_hash="source",
        strategy_id="text_default",
        strategy_version="1.0.0",
        canonical_config_hash="cfg-b",
    )

    assert base != changed_version
    assert base != changed_config


def test_text_chunk_helper_fallback_hash_uses_normalized_text_bytes() -> None:
    chunks = chunk_text_to_prepared_chunks(
        " hello\r\nworld ",
        source_artifact_id="src",
        derived_artifact_id=None,
        tenant_id="tenant",
        principal_id="principal",
        motet_id="motet",
        conversation_id="conv",
        prep_strategy_id="text_default",
        prep_strategy_version="1.0.0",
        canonical_config_hash="cfg",
    )
    expected_key = chunk_cache_key(
        source_content_hash=source_bytes_sha256(b"hello\nworld"),
        strategy_id="text_default",
        strategy_version="1.0.0",
        canonical_config_hash="cfg",
    )

    assert chunks
    assert chunks[0].chunk_cache_key == expected_key
