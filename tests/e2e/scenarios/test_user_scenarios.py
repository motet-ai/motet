import asyncio
from unittest.mock import AsyncMock, patch

from motet.core import MotetStack, Message
from motet.core.types import Response


def test_mvp_chat_mock_response():
    async def _run():
        stack = MotetStack()
        stack.orchestrator.run = AsyncMock(
            return_value=Response(content="You said: ping")
        )
        resp = await stack.chat([Message(role="user", content="ping")])
        assert "You said: ping" == resp.content

    asyncio.run(_run())
