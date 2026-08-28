"""
Motet SDK - Langfuse Agent: Live System Prompt Inject Hook

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-05

Description:
``context_inject`` turn hook for ``langfuse-cms.prompt-manager``. On every
``agent_turn`` (including Chat Explorer), fetches the agent's system prompt
from Langfuse Cloud and returns it as an additive system message. On any
credential/network/empty-prompt failure, returns the bundle static fallback
instead — never aborts the turn.

Dependencies:
- motet_sdk: @motet.command, MotetContext, BaseCommandData
- ._langfuse: resolve_turn_system_prompt

Usage:
Wired in agents/agents.yaml::

  turn_hooks:
    context_inject: ["langfuse-cms.inject_langfuse_prompt"]

Optional turn context overrides:
  langfuse_prompt_name, langfuse_prompt_label, langfuse_vault_key

Notes:
- Motet context_inject is additive only; agents.yaml keeps system_prompt
  empty so the injected message is the sole system prompt.
- Fail-soft: always returns system_messages; never raises to agent_turn.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, motet

from . import _langfuse as lf


class InjectLangfusePromptData(BaseCommandData):
    """Input for inject_langfuse_prompt (context_inject hook contract)."""

    messages: List[Any] = Field(
        default_factory=list,
        description="Current turn messages (unused; accepted for hook data build)",
    )
    context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Turn context; may override prompt name/label/vault key",
    )


@motet.command(timeout_seconds=30)
def inject_langfuse_prompt(
    data: InjectLangfusePromptData,
    motet: MotetContext,
) -> Dict[str, Any]:
    """
    Inject Langfuse Cloud system prompt (or static fallback) for this turn.

    Used via turn_hooks.context_inject so Chat Explorer / agent_turn paths
    pick up CMS edits without a special wrapper command.
    """
    ctx = data.context if isinstance(data.context, dict) else {}
    prompt_name = str(ctx.get("langfuse_prompt_name") or lf.DEFAULT_PROMPT_NAME).strip()
    prompt_label = str(ctx.get("langfuse_prompt_label") or lf.DEFAULT_LABEL).strip()
    vault_key = str(ctx.get("langfuse_vault_key") or lf.DEFAULT_VAULT_KEY).strip()

    resolved = lf.resolve_turn_system_prompt(
        motet,
        prompt_name=prompt_name or lf.DEFAULT_PROMPT_NAME,
        prompt_label=prompt_label or lf.DEFAULT_LABEL,
        vault_key=vault_key or lf.DEFAULT_VAULT_KEY,
    )

    patch: Dict[str, Any] = {
        "langfuse_prompt_source": resolved["prompt_source"],
        "langfuse_prompt_name": prompt_name or lf.DEFAULT_PROMPT_NAME,
        "langfuse_prompt_label": prompt_label or lf.DEFAULT_LABEL,
    }
    if resolved.get("fallback_reason"):
        patch["langfuse_prompt_fallback_reason"] = resolved["fallback_reason"]
    meta = resolved.get("prompt_meta")
    if isinstance(meta, dict) and meta.get("version") is not None:
        patch["langfuse_prompt_version"] = meta.get("version")

    return {
        "system_messages": [resolved["system_prompt"]],
        "context_patch": patch,
    }
