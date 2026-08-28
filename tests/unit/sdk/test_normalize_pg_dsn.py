"""
Motet - PostgreSQL DSN Normalization Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-16

Description:
    Unit tests for ``_normalize_pg_dsn`` used by ``migrate-pgvector``.

Dependencies:
    - motet_sdk.cli.database: DSN helper under test.

Usage:
    pytest tests/unit/sdk/test_normalize_pg_dsn.py

Notes:
    - Already-encoded RDS-style passwords must not be double-encoded.
"""

from __future__ import annotations

from urllib.parse import quote, unquote, urlparse

from motet_sdk.cli.database import _normalize_pg_dsn


def test_normalize_encodes_raw_special_chars() -> None:
    # '+' in a raw (not pre-encoded) password must become %2B.
    raw = "postgresql://motet:p+ssword@db.example:5432/motet_distributed?sslmode=require"
    out = _normalize_pg_dsn(raw)
    assert unquote(urlparse(out).password or "") == "p+ssword"
    assert quote("p+ssword", safe="") in out


def test_normalize_is_idempotent_for_preencoded_rds_password() -> None:
    password = "35*x>8$_6N^4llVD-XN{$Fwz"
    encoded = quote(password, safe="")
    source = (
        f"postgresql://motet:{encoded}@qa-db.example:5432/"
        "motet_distributed?sslmode=require"
    )
    once = _normalize_pg_dsn(source)
    twice = _normalize_pg_dsn(once)
    assert once == twice
    assert unquote(urlparse(once).password or "") == password
    # Must not turn %2A into %252A
    assert "%252A" not in once
    assert "%2A" in once


def test_normalize_leaves_dsn_without_password() -> None:
    source = "postgresql://motet@db.example:5432/postgres"
    assert _normalize_pg_dsn(source) == source
