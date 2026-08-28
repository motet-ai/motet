"""
Motet - Playback Token Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Unit tests for ADR-0118 Phase A.2 playback tokens: mint/verify roundtrip,
    expiry handling, artifact binding, and signature tampering.

Dependencies:
    - pytest
    - motet.core.artifacts.playback_tokens
"""

import time

import pytest

from motet.core.artifacts.playback_tokens import (
    PlaybackTokenError,
    mint_playback_token,
    verify_playback_token,
)

SECRET = "unit-test-secret"


def _mint(**overrides) -> str:
    kwargs = dict(
        artifact_id="art-1",
        tenant_id="tenant-1",
        principal_id="principal-1",
        motet_id="motet-1",
        ttl_seconds=60,
        secret=SECRET,
    )
    kwargs.update(overrides)
    return mint_playback_token(**kwargs)


def test_mint_verify_roundtrip():
    token = _mint()
    claims = verify_playback_token(token, artifact_id="art-1", secret=SECRET)
    assert claims.artifact_id == "art-1"
    assert claims.tenant_id == "tenant-1"
    assert claims.principal_id == "principal-1"
    assert claims.motet_id == "motet-1"
    assert claims.expires_at > time.time()


def test_verify_rejects_expired_token():
    token = _mint(ttl_seconds=1)
    # Force expiry by minting with the minimum TTL and waiting past it.
    time.sleep(1.1)
    with pytest.raises(PlaybackTokenError, match="expired"):
        verify_playback_token(token, artifact_id="art-1", secret=SECRET)


def test_verify_rejects_wrong_artifact():
    token = _mint()
    with pytest.raises(PlaybackTokenError, match="not valid for this artifact"):
        verify_playback_token(token, artifact_id="art-2", secret=SECRET)


def test_verify_rejects_wrong_secret():
    token = _mint()
    with pytest.raises(PlaybackTokenError, match="signature"):
        verify_playback_token(token, artifact_id="art-1", secret="other-secret")


def test_verify_rejects_tampered_claims():
    token = _mint()
    encoded, signature = token.rsplit(".", 1)
    # Flip a character in the encoded claims so the signature no longer matches.
    flipped = ("A" if encoded[0] != "A" else "B") + encoded[1:]
    with pytest.raises(PlaybackTokenError):
        verify_playback_token(f"{flipped}.{signature}", artifact_id="art-1", secret=SECRET)


def test_verify_rejects_malformed_token():
    with pytest.raises(PlaybackTokenError, match="Malformed"):
        verify_playback_token("not-a-token", artifact_id="art-1", secret=SECRET)


def test_admin_scope_token_carries_none_principal():
    token = _mint(principal_id=None)
    claims = verify_playback_token(token, artifact_id="art-1", secret=SECRET)
    assert claims.principal_id is None
    assert claims.tenant_id == "tenant-1"
