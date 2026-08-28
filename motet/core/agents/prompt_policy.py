"""
Motet - Agent Turn Prompt Policy

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Prompt-assembly policies for ``core.agent_turn``, selected from
    ``AgentConfig.metadata.prompt_policy``. The default
    ``motet_system_primary`` prepends the agent's configured ``system_prompt``
    ahead of inbound messages (Motet-native chat). ``client_system_primary``
    keeps inbound ``role=system`` messages first (e.g. Cursor's IDE harness —
    what other models receive) and appends the agent ``system_prompt`` as a
Motet capability appendix. Used by the ``cursor.backend`` OpenAI-compat
    example bundle.

Dependencies:
    - motet.core.types.Message: canonical message type

Usage:
    from motet.core.agents.prompt_policy import (
        assemble_turn_history,
        prompt_policy_from_agent,
        ensure_protected_system_prefix,
        is_prompt_policy_protected,
    )

    policy = prompt_policy_from_agent(agent_config)
    history = assemble_turn_history(inbound_messages, agent_system_prompt, policy)

Notes:
    - Lives under ``core.agents`` because the policy is AgentConfig metadata /
      system_prompt configuration, consumed by agent_turn and
      TokenBudgetProvider.
    - Protected system messages carry metadata so TokenBudgetProvider and
      post-prepare_context re-merge can keep the client system + appendix intact.
    - Normative contract: §5c.3 (Cursor / IDE backend) and
      (``metadata.prompt_policy``).
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence

from motet.core.types import Message

PROMPT_POLICY_MOTET_SYSTEM_PRIMARY = "motet_system_primary"
PROMPT_POLICY_CLIENT_SYSTEM_PRIMARY = "client_system_primary"
PROMPT_POLICY_DEFAULT = PROMPT_POLICY_MOTET_SYSTEM_PRIMARY

_CLIENT_SYSTEM_SOURCE = "client_system"
_AGENT_APPENDIX_SOURCE = "agent_system_appendix"


def prompt_policy_from_agent(agent_config: Any) -> str:
    """Return the prompt policy name from agent config metadata (default: motet_system_primary)."""
    metadata = getattr(agent_config, "metadata", None) or {}
    if not isinstance(metadata, dict):
        return PROMPT_POLICY_DEFAULT
    raw = metadata.get("prompt_policy")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return PROMPT_POLICY_DEFAULT


def is_client_system_primary(policy: Optional[str]) -> bool:
    """Whether ``policy`` selects inbound client system messages as primary."""
    return (policy or "").strip() == PROMPT_POLICY_CLIENT_SYSTEM_PRIMARY


def is_prompt_policy_protected(msg: Any) -> bool:
    """True when a message must survive token-budget trimming for prompt policy."""
    metadata = getattr(msg, "metadata", None) or {}
    if not isinstance(metadata, dict):
        return False
    if metadata.get("prompt_policy_protect"):
        return True
    source = metadata.get("source")
    return source in {_CLIENT_SYSTEM_SOURCE, _AGENT_APPENDIX_SOURCE}


def _with_protect_metadata(msg: Message, *, source: str) -> Message:
    """Return a copy of ``msg`` with prompt-policy protection metadata set."""
    metadata = dict(msg.metadata or {})
    metadata.setdefault("source", source)
    metadata["prompt_policy_protect"] = True
    return Message(
        role=msg.role,
        content=msg.content,
        content_parts=getattr(msg, "content_parts", None),
        name=getattr(msg, "name", None),
        metadata=metadata,
        tool_calls_canonical=getattr(msg, "tool_calls_canonical", None),
        tool_call_id=getattr(msg, "tool_call_id", None),
        attachments=getattr(msg, "attachments", None),
        reasoning_content=getattr(msg, "reasoning_content", None),
        reasoning_blocks=getattr(msg, "reasoning_blocks", None),
    )


def assemble_turn_history(
    inbound_messages: Sequence[Message],
    agent_system_prompt: str,
    prompt_policy: Optional[str] = None,
) -> List[Message]:
    """
    Assemble the turn message list under the given prompt policy.

    - ``motet_system_primary`` (default): agent system prompt first, then all inbound.
    - ``client_system_primary``: inbound system messages first (protected),
      then agent system prompt as appendix (protected), then remaining inbound.
    """
    inbound = list(inbound_messages or [])
    agent_prompt = (agent_system_prompt or "").strip()
    policy = (prompt_policy or PROMPT_POLICY_DEFAULT).strip() or PROMPT_POLICY_DEFAULT

    if not is_client_system_primary(policy):
        history: List[Message] = []
        if agent_prompt:
            history.append(Message(role="system", content=agent_prompt, metadata={}))
        history.extend(inbound)
        return history

    client_systems: List[Message] = []
    remainder: List[Message] = []
    for msg in inbound:
        if getattr(msg, "role", None) == "system":
            client_systems.append(_with_protect_metadata(msg, source=_CLIENT_SYSTEM_SOURCE))
        else:
            remainder.append(msg)

    history = list(client_systems)
    if agent_prompt:
        history.append(
            Message(
                role="system",
                content=agent_prompt,
                metadata={
                    "source": _AGENT_APPENDIX_SOURCE,
                    "prompt_policy_protect": True,
                    # Appendix is stable for a given agent config / handback set;
                    # leave cache_volatile unset so adapters may cache it with the
                    # client system when the provider supports multi-block prefixes.
                },
            )
        )
    history.extend(remainder)
    return history


def ensure_protected_system_prefix(
    history: Sequence[Message],
    protected_prefix: Sequence[Message],
) -> List[Message]:
    """
    Re-attach ``protected_prefix`` system messages if prepare_context trimmed them.

    Matching is by ``(role, source, content)``. Non-matching history messages
    follow the restored prefix in their relative order.
    """
    current = list(history or [])
    prefix = [m for m in (protected_prefix or []) if is_prompt_policy_protected(m)]
    if not prefix:
        return current

    def _key(msg: Any) -> tuple:
        metadata = getattr(msg, "metadata", None) or {}
        source = metadata.get("source") if isinstance(metadata, dict) else None
        return (
            getattr(msg, "role", None),
            source,
            getattr(msg, "content", None) or "",
        )

    present = {_key(m) for m in current if is_prompt_policy_protected(m)}
    missing = [m for m in prefix if _key(m) not in present]
    if not missing:
        # Still enforce prefix order: protected messages first in declared order.
        protected_keys = {_key(m) for m in prefix}
        ordered_prefix = list(prefix)
        rest = [m for m in current if _key(m) not in protected_keys]
        return ordered_prefix + rest

    protected_keys = {_key(m) for m in prefix}
    rest = [m for m in current if _key(m) not in protected_keys]
    return list(prefix) + rest


def extract_protected_prefix(history: Sequence[Message]) -> List[Message]:
    """Return protected prompt-policy system messages from ``history`` (stable order)."""
    return [m for m in (history or []) if is_prompt_policy_protected(m)]
