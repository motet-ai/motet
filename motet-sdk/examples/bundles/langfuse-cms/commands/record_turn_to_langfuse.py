"""
Motet SDK - Langfuse CMS: After-Finalize Generation Push

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-05

Description:
``after_finalize`` turn hook that pushes this agent's completed turn
usage and cost to Langfuse Cloud as a generation span (OTLP). Fail-soft:
missing credentials or API errors return ok=false and never break Chat
Explorer. The Motet conversation id becomes the Langfuse session id, so a
conversation's turns group together rather than appearing as unrelated traces.

Dependencies:
- motet_sdk: @motet.command, MotetContext, BaseCommandData
- ._langfuse: resolve_credentials, record_generation

Usage:
Wired in agents/agents.yaml::

  turn_hooks:
    after_finalize: ["langfuse-cms.record_turn_to_langfuse"]

Notes:
- Motet ADR-0018 cost tracking remains the platform source of truth.
- Cost lands on the generation's own cost field (via ``gen_ai.usage.cost``),
  so it shows in Langfuse cost columns rather than only in metadata.
- Look for a trace named ``langfuse-cms.agent_turn`` under Tracing.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, motet

from . import _langfuse as lf


class RecordTurnToLangfuseData(BaseCommandData):
    """Input for record_turn_to_langfuse (after_finalize hook contract)."""

    messages: List[Any] = Field(
        default_factory=list,
        description="Turn messages (system/user/assistant) for Langfuse input",
    )
    assistant_response: str = Field(
        default="",
        description="Final assistant text for this turn",
    )
    agent_id: Optional[str] = Field(
        default=None,
        description="Qualified agent id that produced the turn",
    )
    usage: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Token usage (prompt_tokens, completion_tokens, total_tokens)",
    )
    cost_usd: Optional[float] = Field(
        default=None,
        description="Optional Motet-estimated cost in USD when available",
    )
    model: Optional[str] = Field(
        default=None,
        description="Model id (provider/name) when known",
    )
    context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Turn context (may include langfuse_prompt_* from inject)",
    )


def _as_message_dicts(messages: List[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for msg in messages or []:
        if isinstance(msg, dict):
            out.append({"role": msg.get("role"), "content": msg.get("content")})
        else:
            out.append(
                {
                    "role": getattr(msg, "role", None),
                    "content": getattr(msg, "content", None),
                }
            )
    return out


@motet.command(timeout_seconds=30)
def record_turn_to_langfuse(
    data: RecordTurnToLangfuseData,
    motet: MotetContext,
) -> Dict[str, Any]:
    """
    Push this completed turn's usage to Langfuse Cloud (fail-soft).

    Invoked via turn_hooks.after_finalize after core.finalize_turn.
    """
    ctx = data.context if isinstance(data.context, dict) else {}
    try:
        creds = lf.resolve_credentials(motet, require_host=True)
    except lf.LangfuseConfigError as exc:
        return {
            "ok": False,
            "skipped": True,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }

    prompt_name = ctx.get("langfuse_prompt_name") or lf.DEFAULT_PROMPT_NAME
    prompt_version = ctx.get("langfuse_prompt_version")
    prompt_source = ctx.get("langfuse_prompt_source")

    metadata: Dict[str, Any] = {
        "bundle": "langfuse-cms",
        "agent_id": data.agent_id,
        "prompt_source": prompt_source,
        "prompt_label": ctx.get("langfuse_prompt_label") or lf.DEFAULT_LABEL,
    }
    if ctx.get("langfuse_prompt_fallback_reason"):
        metadata["prompt_fallback_reason"] = ctx.get("langfuse_prompt_fallback_reason")

    model = (data.model or "").strip() or "unknown"
    try:
        push = lf.record_generation(
            creds,
            model=model,
            input_messages=_as_message_dicts(list(data.messages or [])),
            output=data.assistant_response or "",
            usage=dict(data.usage or {}),
            cost_usd=data.cost_usd,
            name="langfuse-cms.agent_turn",
            metadata=metadata,
            # Group a conversation's turns into one Langfuse session.
            session_id=motet.conversation_id,
            user_id=motet.principal_id,
            # Only claim a prompt link when the turn actually ran on the Cloud
            # prompt — on fallback the version is unknown and would misattribute.
            prompt_name=prompt_name if prompt_source == "langfuse" else None,
            prompt_version=prompt_version if prompt_source == "langfuse" else None,
        )
        return {
            "ok": True,
            "trace_id": push.get("trace_id"),
            "observation_id": push.get("observation_id"),
            "model": model,
            "usage": data.usage,
            "cost_usd": data.cost_usd,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
