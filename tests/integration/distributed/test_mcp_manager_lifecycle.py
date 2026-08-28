"""
Motet - MCP Manager Lifecycle Restart-Preservation Integration Test

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-14

ADR-0105 §M6: end-to-end restart-preservation for the sibling MCPInstanceManager.

The architectural payoff of hoisting the manager out of the worker process
is that an MCP instance with ``LifecycleDuration.CONVERSATION`` (or longer)
survives a worker restart — the manager keeps running, its docker-spawned
MCP server children stay up, and the worker simply re-binds and re-discovers
tools when it comes back.

This test asserts that contract against a running compose stack:

  1. Snapshot the ``mcp`` manager's identity (``manager_id`` / ``pid`` /
     ``uptime_seconds`` / ``instances.total``) via the
     ``/api/v1/workers/managers/status`` endpoint.
  2. ``docker compose restart`` one of the workers it serves.
  3. Wait for that worker to come back ``ready`` with MCP tools registered.
  4. Re-snapshot the manager and assert:
       - ``manager_id`` unchanged
       - ``pid`` unchanged (the manager process was NOT restarted)
       - ``uptime_seconds`` has grown (still the same process)
       - ``instances.total`` is preserved (children survived)
       - the restarted worker is back in ``served_workers``

The test skips unless ``EXTERNAL_SERVICES_AVAILABLE=true`` AND the API
endpoint at ``MOTET_DISTRIBUTED_STACK_HTTP_URL`` (default
``http://localhost:8000``) is reachable. Compose project / file / service
names are also env-overridable so the same test can run against the dev
stack (``docker-compose.distributed.yml``, project ``motet_dev``) or any
other deployment that has the sibling-manager shape.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any, Dict, List, Optional

import pytest
import requests

pytestmark = [
    pytest.mark.integration,
    pytest.mark.distributed,
    pytest.mark.mcp,
    pytest.mark.requires_external,
    pytest.mark.slow,
]


API_BASE = os.environ.get(
    "MOTET_DISTRIBUTED_STACK_HTTP_URL", "http://localhost:8000"
).rstrip("/")
COMPOSE_FILE = os.environ.get(
    "MOTET_TEST_COMPOSE_FILE", "docker-compose.distributed.yml"
)
COMPOSE_PROJECT = os.environ.get("MOTET_TEST_COMPOSE_PROJECT", "motet_dev")
WORKER_SERVICE = os.environ.get("MOTET_TEST_WORKER_SERVICE", "worker-1")
WORKER_ID = os.environ.get("MOTET_TEST_WORKER_ID", "cloud_worker1")

READY_TIMEOUT_SECONDS = int(os.environ.get("MOTET_TEST_READY_TIMEOUT", "180"))
READY_POLL_INTERVAL = 3.0


def _api_headers() -> Dict[str, str]:
    """Auth for /managers/status (ADR-0066). Prefer JWT; fall back to dev headers."""
    headers: Dict[str, str] = {}
    jwt = os.environ.get("MOTET_TEST_JWT")
    if jwt:
        headers["Authorization"] = f"Bearer {jwt}"
    api_key = os.environ.get("MOTET_API_KEY")
    if api_key:
        headers["X-API-Key"] = api_key
    headers.setdefault("X-Principal-Id", os.environ.get("MOTET_TEST_PRINCIPAL_ID", "test-ops"))
    headers.setdefault("X-Tenant-Id", os.environ.get("MOTET_TEST_TENANT_ID", "motet-global"))
    headers.setdefault("X-Roles", os.environ.get("MOTET_TEST_ROLES", "admin"))
    return headers


def _get_managers() -> Dict[str, Any]:
    resp = requests.get(
        f"{API_BASE}/api/v1/workers/managers/status",
        headers=_api_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["managers"]


def _get_workers() -> Dict[str, Any]:
    resp = requests.get(f"{API_BASE}/api/v1/workers/readiness", timeout=10)
    resp.raise_for_status()
    return resp.json()["workers"]


def _find_mcp_manager(managers: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the first MCP-typed manager record, or None."""
    for m in managers.values():
        if m.get("type") == "mcp" and m.get("status") == "running":
            return m
    return None


def _wait_for_worker_restarted(
    worker_id: str,
    pre_restart_uptime: float,
    min_mcp_tools: int,
    timeout: float,
) -> Dict[str, Any]:
    """Poll readiness until the worker is *demonstrably* a fresh process.

    A successful restart shows up as ``uptime_seconds`` strictly less than
    the value we saw before the restart (worker boot time resets to ~0).
    Polling for ``state == ready`` alone isn't sufficient because the old
    Redis entry can linger for several seconds before the new process
    overwrites it, which would let the loop exit on stale data.
    """
    deadline = time.time() + timeout
    last_state: Dict[str, Any] = {}
    while time.time() < deadline:
        try:
            workers = _get_workers()
            w = workers.get(worker_id) or {}
            last_state = w
            uptime = float(w.get("uptime_seconds") or 0.0)
            if (
                w.get("state") == "ready"
                and uptime < pre_restart_uptime
                and int(w.get("mcp_tool_count", 0)) >= min_mcp_tools
            ):
                return w
        except Exception:
            pass
        time.sleep(READY_POLL_INTERVAL)
    raise AssertionError(
        f"worker {worker_id!r} did not come back as a fresh ready process "
        f"with >= {min_mcp_tools} MCP tools within {timeout:.0f}s "
        f"(pre_restart_uptime={pre_restart_uptime:.1f}s). "
        f"Last observed: {last_state}"
    )


def _api_reachable() -> bool:
    try:
        r = requests.get(f"{API_BASE}/api/v1/workers/readiness", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _docker_compose(*args: str) -> subprocess.CompletedProcess:
    """Run a docker compose command against the test project."""
    cmd = [
        "docker",
        "compose",
        "-f",
        COMPOSE_FILE,
        "-p",
        COMPOSE_PROJECT,
        *args,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


@pytest.fixture(scope="module")
def live_stack() -> Dict[str, Any]:
    """Skip the whole module unless a stack with a running MCP manager is reachable."""
    if not _api_reachable():
        pytest.skip(
            f"Stack API not reachable at {API_BASE}; "
            "start with `motet-cli local up` or set MOTET_DISTRIBUTED_STACK_HTTP_URL"
        )

    managers = _get_managers()
    mcp_mgr = _find_mcp_manager(managers)
    if not mcp_mgr:
        pytest.skip(
            "No running MCP-typed manager found in /api/v1/workers/managers/status"
        )

    served: List[str] = list(mcp_mgr.get("served_workers") or [])
    if WORKER_ID not in served:
        pytest.skip(
            f"Configured WORKER_ID={WORKER_ID!r} is not in this manager's "
            f"served_workers={served!r}; set MOTET_TEST_WORKER_ID accordingly"
        )

    return {"manager": mcp_mgr, "served": served}


def test_mcp_manager_survives_worker_restart(live_stack: Dict[str, Any]) -> None:
    """ADR-0105 §M6: the sibling MCP manager must outlive a worker restart."""
    before = live_stack["manager"]
    before_pid = before["pid"]
    before_manager_id = before["manager_id"]
    before_total = before["instances"]["total"]
    before_uptime = before["stats"]["uptime_seconds"]

    workers_before = _get_workers()
    worker_before = workers_before.get(WORKER_ID, {})
    pre_mcp_tool_count = int(worker_before.get("mcp_tool_count", 0))
    pre_uptime = float(worker_before.get("uptime_seconds") or 0.0)
    assert pre_mcp_tool_count > 0, (
        f"Pre-restart sanity: worker {WORKER_ID!r} should already have MCP tools, "
        f"got {pre_mcp_tool_count}"
    )
    assert pre_uptime > 0.0, (
        f"Pre-restart sanity: worker {WORKER_ID!r} should report uptime, "
        f"got {pre_uptime}"
    )

    # --- Restart ---
    result = _docker_compose("restart", WORKER_SERVICE)
    assert result.returncode == 0, (
        f"`docker compose restart {WORKER_SERVICE}` failed:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )

    # --- Wait for a verifiably fresh worker process to come back ready ---
    worker_after = _wait_for_worker_restarted(
        WORKER_ID,
        pre_restart_uptime=pre_uptime,
        min_mcp_tools=pre_mcp_tool_count,
        timeout=READY_TIMEOUT_SECONDS,
    )
    assert worker_after["state"] == "ready"
    assert float(worker_after["uptime_seconds"]) < pre_uptime, (
        "post-restart worker uptime did not reset — restart did not actually happen"
    )

    # --- Re-snapshot the manager ---
    after = _find_mcp_manager(_get_managers())
    assert after is not None, "MCP manager disappeared after worker restart"

    # --- Identity / process preservation: hoisting paid off ---
    assert after["manager_id"] == before_manager_id, (
        "manager_id changed across worker restart — manager was reaped"
    )
    assert after["pid"] == before_pid, (
        f"manager PID changed ({before_pid} → {after['pid']}); "
        "the manager process restarted, defeating ADR-0105's hoisting"
    )
    assert after["stats"]["uptime_seconds"] >= before_uptime, (
        "uptime regressed — manager process was restarted"
    )

    # --- Instance preservation: children survived ---
    after_total = after["instances"]["total"]
    assert after_total >= before_total, (
        f"MCP instance pool shrank across worker restart "
        f"({before_total} → {after_total})"
    )

    # --- Worker re-bound to the same manager ---
    served_after: List[str] = list(after.get("served_workers") or [])
    assert WORKER_ID in served_after, (
        f"worker {WORKER_ID!r} did not re-bind to manager {before_manager_id!r} "
        f"after restart; current served_workers={served_after!r}"
    )
