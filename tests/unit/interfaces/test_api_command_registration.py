"""
Motet - API startup registers core command types

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-04

Description:
    Regression guard for a cold API process 404ing on core command types that
    workers can execute. Command types register as a side effect of importing
    their modules, and the API never imported them; the only reason
    /api/v1/commands worked was that some other request path happened to pull
    them in first. Startup now registers them explicitly.

    Bundle commands are deliberately not covered: they register only in workers
    (load_bundles_on_startup / core.reload_bundle), and the command endpoints
    resolve them through the Redis bundle catalog instead.

Dependencies:
    - pytest
    - motet.interfaces.http (lifespan)

Usage:
    pytest tests/unit/interfaces/test_api_command_registration.py -v
"""

from __future__ import annotations

import pytest

from motet.core.commands.command_type_registry import command_type_registry


# Types the API must be able to resolve locally: the turn entry point Chat
# Explorer and the CLI both invoke, plus a couple of core commands from other
# registration modules so a partial import is caught too.
REQUIRED_CORE_TYPES = (
    "core.agent_turn",
    "core.finalize_turn",
    "core.model_inference",
    "core.tool_execution",
)


def test_ensure_commands_registered_covers_core_turn_types() -> None:
    from motet.core.commands.distributed import DistributedCommand

    DistributedCommand._ensure_commands_registered()

    missing = [t for t in REQUIRED_CORE_TYPES if command_type_registry.get(t) is None]
    assert not missing, f"core command types not registered: {missing}"


def test_ensure_commands_registered_is_idempotent() -> None:
    """Startup may run in a process that already registered (e.g. tests, reload)."""
    from motet.core.commands.distributed import DistributedCommand

    DistributedCommand._ensure_commands_registered()
    first = command_type_registry.get("core.agent_turn")
    DistributedCommand._ensure_commands_registered()
    second = command_type_registry.get("core.agent_turn")

    assert first is not None
    assert second is not None


@pytest.mark.asyncio
async def test_api_lifespan_registers_core_commands(monkeypatch) -> None:
    """The registration must happen at startup, not lazily per request.

    Everything else in the lifespan is stubbed: this asserts the registration
    step runs and is ordered before the app serves traffic, without standing up
    Redis, event observers, or the OAuth refresher.
    """
    from motet.interfaces import http as http_mod

    calls: list[str] = []

    async def _noop_observers() -> None:
        calls.append("observers")

    async def _noop_refresher() -> None:
        calls.append("refresher")

    monkeypatch.setattr(
        "motet.core.workers.start_event_observers", _noop_observers, raising=False
    )
    monkeypatch.setattr(
        "motet.core.workers.stop_event_observers", _noop_observers, raising=False
    )
    monkeypatch.setattr(
        "motet.core.security.vault_cache_observer.register_vault_cache_observer",
        lambda: calls.append("vault"),
        raising=False,
    )
    monkeypatch.setattr(
        "motet.core.security.oauth_token_refresher.start_token_refresher",
        _noop_refresher,
        raising=False,
    )
    monkeypatch.setattr(
        "motet.core.security.oauth_token_refresher.stop_token_refresher",
        _noop_refresher,
        raising=False,
    )

    registered: list[bool] = []

    class _SpyCommand:
        @staticmethod
        def _ensure_commands_registered() -> None:
            registered.append(True)

    monkeypatch.setattr(
        "motet.core.commands.distributed.DistributedCommand",
        _SpyCommand,
        raising=False,
    )

    async with http_mod._lifespan(object()):  # type: ignore[arg-type]
        assert registered == [True], "lifespan did not register core command types"
