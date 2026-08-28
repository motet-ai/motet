"""
Motet - MCP proxy creation observer unit tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Guards the observer create path: asyncio must be imported at module
    scope so wait_for around create_instance cannot NameError (that left
    MotetMCPClient waiting 30s with no Playwright child).

Dependencies:
    - pytest / asyncio
    - MCPProxyCreationObserver

Usage:
    pytest tests/unit/tools/mcp_motet/proxy/test_mcp_proxy_observer.py
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from motet.core.tools.mcp_motet.proxy.mcp_proxy_observer import MCPProxyCreationObserver


@pytest.mark.asyncio
async def test_create_proxy_async_uses_module_asyncio() -> None:
    manager = SimpleNamespace(
        create_instance=AsyncMock(return_value=MagicMock(instance_id="pw-1", transport=None, process=None)),
        _create_timeout_seconds=lambda: 5.0,
    )
    observer = MCPProxyCreationObserver(manager)
    await observer._create_proxy_async(
        {
            "service_id": "playwright",
            "context_id": "playwright:t:m:u:conversation:c1",
            "conversation_id": "c1",
            "task_id": "t1",
        }
    )
    manager.create_instance.assert_awaited_once()
