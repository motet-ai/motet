import asyncio

from motet.core import MotetStack, Message, Config
from motet.core.tools import registry


def test_contextualize_observation_policy_blocks_context():
    async def _run():
        # Tool that returns something but should not be contextualized
        async def nop_context(params: dict) -> dict:
            return {"result": "invisible"}

        registry.register(
            name="no_context_tool",
            description="should not inject into context",
            func=nop_context,
            schema=None,
            triggers=["no_context:"],
            priority=2,
            category="test",
            contextualize_observation=False,
        )

        cfg = Config()
        stack = MotetStack(cfg)
        registry.set_runtime_stack(stack)

        # Run a non-streaming chat; orchestrator will execute action loop
        resp = await stack.orchestrator.run(stack, [Message(role="user", content="no_context:")])

        # The formatted observation text should be absent when contextualization is disabled
        assert "[observation:no_context_tool]" not in (resp.content or "")

    asyncio.run(_run())


