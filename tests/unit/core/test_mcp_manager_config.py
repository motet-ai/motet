"""
Motet - MCP Manager Config Plumbing Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

ADR-0105 milestone 0 — config plumbing tests.

Asserts that ``Config.mcp_manager_endpoint`` and ``Config.mcp_manager_id``
behave as the contract documented in ADR-0105 §R2/§R3:

- Both default to ``None``: MCP itself is opt-in (``mcp_enabled=False``), so
  the manager wiring is only required when MCP is enabled. The hard-fail when
  ``mcp_enabled=True`` and either is unset lives in worker startup (M1), not
  in the schema.
- Endpoint and id are independent fields (not derived from each other) so
  a DNS rename does not break dashboard/metrics continuity (§R3).
- Environment variables ``MOTET_MCP_MANAGER_ENDPOINT`` and
  ``MOTET_MCP_MANAGER_ID`` are the operator-facing knobs (Helm chart and
  reference compose templates set them explicitly).

These tests guard the scaffold for milestones 1, 4, 4.5, and 5 — code in
those milestones reads ``config.mcp_manager_endpoint`` /
``config.mcp_manager_id`` and must keep working as that wiring lands.
"""

import pytest

from motet.core.config import Config


def test_manager_fields_default_to_none(monkeypatch):
    """Both fields are Optional and default to None per ADR-0105 §R0/§R2/§R3."""
    monkeypatch.delenv("MOTET_MCP_MANAGER_ENDPOINT", raising=False)
    monkeypatch.delenv("MOTET_MCP_MANAGER_ID", raising=False)

    config = Config()

    assert config.mcp_manager_endpoint is None
    assert config.mcp_manager_id is None


@pytest.mark.parametrize(
    "endpoint,manager_id",
    [
        ("mcp-manager.cloud-default.svc.cluster.local", "mcp-cloud-worker1"),
        ("mcp-manager", "mcp-edge_deviceA"),
        ("mcp-manager.tenant-acme.svc.cluster.local", "mcp-cloud-tenant-acme"),
    ],
    ids=["cloud-shape-A-prime", "edge-shape-A", "cloud-shape-B-future"],
)
def test_manager_fields_round_trip(monkeypatch, endpoint, manager_id):
    """Endpoint + id round-trip through env for the three documented shapes."""
    monkeypatch.setenv("MOTET_MCP_MANAGER_ENDPOINT", endpoint)
    monkeypatch.setenv("MOTET_MCP_MANAGER_ID", manager_id)

    config = Config()

    assert config.mcp_manager_endpoint == endpoint
    assert config.mcp_manager_id == manager_id


def test_manager_id_is_independent_of_endpoint(monkeypatch):
    """ADR-0105 §R3: identity must not be derived from endpoint.

    Setting only one field must leave the other untouched, so a DNS rename
    (endpoint change) does not implicitly mutate the manager's identity in
    the status surface and metrics.
    """
    monkeypatch.setenv("MOTET_MCP_MANAGER_ENDPOINT", "mcp-manager.new-dns.svc")
    monkeypatch.delenv("MOTET_MCP_MANAGER_ID", raising=False)

    config = Config()

    assert config.mcp_manager_endpoint == "mcp-manager.new-dns.svc"
    assert config.mcp_manager_id is None
