"""
Motet — unit test fixtures for motet.core

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-07

Description:
    Autouse helpers so docker-engine unit tests that mock ``docker_request`` can run
    in environments (e.g. CI test-runner) where the default Unix socket path does
    not exist. Without this, ``run_docker`` exits before the mock is invoked.

Dependencies:
    - pytest
    - motet.core.execution.docker_client

Usage:
    Collected automatically under ``tests/unit/core/`` — no imports required.

Notes:
    Scoped to ``test_execution_docker_backend.py`` and docker-related cases in
    ``test_worker_exec.py`` only.
"""

from __future__ import annotations

import os

import pytest

from motet.core.execution import docker_client


@pytest.fixture(autouse=True)
def _pretend_docker_unix_socket_for_mocked_engine_tests(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodeid = request.node.nodeid
    if "test_execution_docker_backend.py" not in nodeid:
        if "test_worker_exec.py" not in nodeid or "docker" not in nodeid.lower():
            return

    sock, err = docker_client.docker_socket_path()
    if err or not sock:
        return

    sock_norm = os.path.normpath(sock)
    orig_exists = os.path.exists

    def exists(path: str | os.PathLike[str]) -> bool:
        try:
            if os.path.normpath(str(path)) == sock_norm:
                return True
        except (OSError, TypeError, ValueError):
            pass
        return orig_exists(path)

    monkeypatch.setattr(os.path, "exists", exists)
