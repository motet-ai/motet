"""
Motet - Prompt Policy Unit Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-28

Description:
    Unit tests for ``motet.core.agents.prompt_policy`` — assembly order for
    ``client_system_primary`` vs default ``motet_system_primary``, protected-
    prefix re-merge after budgeting, and TokenBudgetProvider protection of
    client system messages.

Dependencies:
    - pytest
    - motet.core.agents.prompt_policy
    - motet.core.orchestration.context.token_budget.TokenBudgetProvider

Usage:
    pytest tests/unit/core/agents/test_prompt_policy.py -q
"""

from types import SimpleNamespace

from motet.core.agents.prompt_policy import (
    PROMPT_POLICY_CLIENT_SYSTEM_PRIMARY,
    PROMPT_POLICY_MOTET_SYSTEM_PRIMARY,
    assemble_turn_history,
    ensure_protected_system_prefix,
    extract_protected_prefix,
    is_prompt_policy_protected,
    prompt_policy_from_agent,
)
from motet.core.orchestration.context.token_budget import TokenBudgetProvider
from motet.core.orchestration.context.types import ContextPipelineState
from motet.core.types import Message


def test_prompt_policy_from_agent_reads_metadata() -> None:
    agent = SimpleNamespace(metadata={"prompt_policy": "client_system_primary"})
    assert prompt_policy_from_agent(agent) == PROMPT_POLICY_CLIENT_SYSTEM_PRIMARY
    assert prompt_policy_from_agent(SimpleNamespace(metadata=None)) == PROMPT_POLICY_MOTET_SYSTEM_PRIMARY


def test_assemble_motet_system_primary_prepends_agent_system() -> None:
    inbound = [
        Message(role="system", content="client system"),
        Message(role="user", content="hi"),
    ]
    history = assemble_turn_history(inbound, "You are Motet.", PROMPT_POLICY_MOTET_SYSTEM_PRIMARY)
    assert [m.role for m in history] == ["system", "system", "user"]
    assert history[0].content == "You are Motet."
    assert history[1].content == "client system"


def test_assemble_client_system_primary_keeps_client_first() -> None:
    inbound = [
        Message(role="system", content="You operate in Cursor."),
        Message(role="user", content="fix the bug"),
    ]
    appendix = "Additional capabilities from this backend (Motet): ..."
    history = assemble_turn_history(
        inbound, appendix, PROMPT_POLICY_CLIENT_SYSTEM_PRIMARY
    )
    assert [m.role for m in history] == ["system", "system", "user"]
    assert history[0].content == "You operate in Cursor."
    assert history[0].metadata.get("source") == "client_system"
    assert history[0].metadata.get("prompt_policy_protect") is True
    assert history[1].content == appendix
    assert history[1].metadata.get("source") == "agent_system_appendix"
    assert history[2].content == "fix the bug"


def test_ensure_protected_prefix_restores_trimmed_client_system() -> None:
    full = assemble_turn_history(
        [
            Message(role="system", content="CURSOR_SYSTEM " + ("word " * 100)),
            Message(role="user", content="latest"),
        ],
        "Motet appendix",
        PROMPT_POLICY_CLIENT_SYSTEM_PRIMARY,
    )
    prefix = extract_protected_prefix(full)
    trimmed = [Message(role="user", content="latest")]
    restored = ensure_protected_system_prefix(trimmed, prefix)
    assert len(restored) == 3
    assert restored[0].content.startswith("CURSOR_SYSTEM")
    assert restored[1].content == "Motet appendix"
    assert restored[2].content == "latest"


def test_token_budget_protects_prompt_policy_system_messages() -> None:
    client_system = Message(
        role="system",
        content=" ".join(["harness"] * 4000),
        metadata={"source": "client_system", "prompt_policy_protect": True},
    )
    appendix = Message(
        role="system",
        content="Motet appendix short",
        metadata={"source": "agent_system_appendix", "prompt_policy_protect": True},
    )
    user = Message(role="user", content=" ".join(["user"] * 10))
    state = ContextPipelineState(messages=[client_system, appendix, user])
    data = SimpleNamespace(max_context_tokens=50)
    motet = SimpleNamespace(
        conversation_id="c1",
        stack=SimpleNamespace(config=SimpleNamespace(token_budget=50)),
    )
    logger = SimpleNamespace(
        warning=lambda *a, **k: None,
        info=lambda *a, **k: None,
    )
    out = TokenBudgetProvider().apply(state, data=data, motet=motet, logger=logger)
    roles_contents = [(m.role, (m.content or "")[:20]) for m in out.messages]
    assert any(r == "system" and c.startswith("harness") for r, c in roles_contents)
    assert any(r == "system" and "appendix" in c for r, c in roles_contents)
    assert is_prompt_policy_protected(client_system)
