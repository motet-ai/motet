"""
Motet - Publish Digest-Pinning Enforcement Tests (ADR-0100 §rule 2)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

ADR-0100 §rule 2 says the runtime ``oci_image_ref`` MUST use ``@sha256:...``
in production. The catalog UI surfaces a "mutable tag" warning today; this
test pins the gate-based enforcement that fires when
``MOTET_REQUIRE_DIGEST_PINNED_PUBLISH=true`` is set.

We exercise the small, pure helpers (``_is_oci_ref_digest_pinned``,
``_digest_pinning_enforced``, ``_enforce_publish_digest_pinning``) directly
rather than rebuilding a full ``publish_bundle`` harness — the integration
into ``publish_bundle`` is a single conditional call site that's exercised
by these helpers' contracts.
"""

from __future__ import annotations

import pytest

from motet.core.bundles.deploy import (
    BundleDeployError,
    _digest_pinning_enforced,
    _enforce_publish_digest_pinning,
    _is_oci_ref_digest_pinned,
)


# ---------------------------------------------------------------------------
# Pure-function: digest-form recognizer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ref",
    [
        "registry.example.com/foo@sha256:" + "a" * 64,
        "ghcr.io/org/img@sha256:" + "0123456789abcdef" * 4,
        # Mixed case in hex is tolerated (we lowercase before validating).
        "registry.example.com/foo@sha256:" + "AbCdEf01" * 8,
    ],
)
def test_digest_pinned_refs_recognized(ref: str) -> None:
    assert _is_oci_ref_digest_pinned(ref), (
        f"ADR-0100 §rule 2 form (name@sha256:<64hex>) MUST be recognized as pinned. "
        f"Got ref={ref!r}"
    )


@pytest.mark.parametrize(
    "ref",
    [
        "python:3.11-slim",                                 # mutable tag
        "registry.example.com/foo:v1",                      # tagged, no digest
        "registry.example.com/foo",                         # no tag, no digest
        "registry.example.com/foo@sha256:" + "a" * 63,      # short digest
        "registry.example.com/foo@sha256:" + "a" * 65,      # long digest
        "registry.example.com/foo@sha256:" + "g" * 64,      # non-hex
        "registry.example.com/foo@md5:" + "a" * 32,         # wrong algo
    ],
)
def test_non_digest_refs_rejected(ref: str) -> None:
    assert not _is_oci_ref_digest_pinned(ref), (
        f"Mutable / malformed ref MUST NOT be treated as digest-pinned. Got ref={ref!r}"
    )


def test_empty_ref_treated_as_pinned() -> None:
    """Empty / unset refs are *not* the enforcement target — a tier-only
    bundle has nothing to pin and the worker_exec merge path handles
    absence according to backend rules. The enforcement helper
    short-circuits on empty too (separate test below)."""
    assert _is_oci_ref_digest_pinned("") is True
    assert _is_oci_ref_digest_pinned("   ") is True  # treated as empty after strip


# ---------------------------------------------------------------------------
# Env gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("val", ["true", "True", "1", "yes", "on", "TRUE"])
def test_env_gate_truthy(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("MOTET_REQUIRE_DIGEST_PINNED_PUBLISH", val)
    assert _digest_pinning_enforced() is True


@pytest.mark.parametrize("val", ["false", "0", "no", "off", "", "garbage"])
def test_env_gate_falsy(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("MOTET_REQUIRE_DIGEST_PINNED_PUBLISH", val)
    assert _digest_pinning_enforced() is False


def test_env_gate_unset_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default is OFF so non-prod / dev installs aren't suddenly
    rejecting publishes after an upgrade."""
    monkeypatch.delenv("MOTET_REQUIRE_DIGEST_PINNED_PUBLISH", raising=False)
    assert _digest_pinning_enforced() is False


# ---------------------------------------------------------------------------
# Enforcement helper (the function publish_bundle actually calls)
# ---------------------------------------------------------------------------


def test_enforcement_off_allows_mutable_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the gate is off, mutable refs MUST pass through unchanged —
    this is the dev / early-stage default path."""
    monkeypatch.delenv("MOTET_REQUIRE_DIGEST_PINNED_PUBLISH", raising=False)
    _enforce_publish_digest_pinning("acme.demo", {"oci_image_ref": "python:3.11-slim"})


def test_enforcement_on_allows_digest_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_REQUIRE_DIGEST_PINNED_PUBLISH", "true")
    _enforce_publish_digest_pinning(
        "acme.demo",
        {"oci_image_ref": "registry.example.com/acme/demo@sha256:" + "a" * 64},
    )


def test_enforcement_on_allows_empty_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier-only bundles (no concrete oci_image_ref) MUST NOT be rejected
    by this check — they have nothing to pin and the worker_exec merge
    layer is the right place to enforce backend-specific rules."""
    monkeypatch.setenv("MOTET_REQUIRE_DIGEST_PINNED_PUBLISH", "true")
    _enforce_publish_digest_pinning("acme.demo", {})
    _enforce_publish_digest_pinning("acme.demo", {"oci_image_ref": ""})
    _enforce_publish_digest_pinning("acme.demo", {"base_image_stack": "python-minimal"})


def test_enforcement_on_rejects_mutable_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_REQUIRE_DIGEST_PINNED_PUBLISH", "true")
    with pytest.raises(BundleDeployError) as exc:
        _enforce_publish_digest_pinning(
            "acme.demo",
            {"oci_image_ref": "python:3.11-slim"},
        )
    msg = str(exc.value)
    assert "digest-pinned" in msg, msg
    # The error MUST carry actionable detail so operators can fix it.
    assert exc.value.details.get("bundle_id") == "acme.demo"
    assert exc.value.details.get("oci_image_ref") == "python:3.11-slim"
    assert exc.value.details.get("rule") == "ADR-0100 §rule 2"


def test_enforcement_on_rejects_tagged_ref_without_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tagged-but-not-digested ref like ``registry/foo:v1`` is the most
    common mistake — verify it's caught (not just bare image names)."""
    monkeypatch.setenv("MOTET_REQUIRE_DIGEST_PINNED_PUBLISH", "true")
    with pytest.raises(BundleDeployError):
        _enforce_publish_digest_pinning(
            "acme.demo",
            {"oci_image_ref": "registry.example.com/acme/demo:v1"},
        )
