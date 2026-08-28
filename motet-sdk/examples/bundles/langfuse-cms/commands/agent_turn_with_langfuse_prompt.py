"""
Motet SDK - Langfuse Agent: Standalone Turn With Cloud Prompt

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-05

Description:
CLI/demo wrapper that fetches the demo agent's system prompt from Langfuse
Cloud, runs ``models.infer``, and optionally records usage/cost. Chat
Explorer turns use ``inject_langfuse_prompt`` via context_inject instead;
this command remains for explicit one-shot demos outside the agent loop.

Dependencies:
- motet_sdk: @motet.command, MotetContext, BaseCommandData, WorkerCapability
- pydantic: command input schema
- ._langfuse: Langfuse Cloud HTTP helpers

Usage:
  motet-cli commands run langfuse-cms.agent_turn_with_langfuse_prompt \\
    --data '{"message":"Hello","provider":"openai","model_name":"gpt-4o-mini"}'

Notes:
- Prompt source is reported in the response (langfuse | fallback).
- Generation push is fail-soft when record_to_langfuse=true.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, WorkerCapability, motet

from . import _langfuse as lf


class AgentTurnWithLangfusePromptData(BaseCommandData):
    """Input for agent_turn_with_langfuse_prompt."""

    message: str = Field(..., description="User message for this turn")
    provider: str = Field(default="openai", description="Model provider id")
    model_name: str = Field(default="gpt-4o-mini", description="Model name")
    prompt_name: str = Field(
        default=lf.DEFAULT_PROMPT_NAME,
        description="Langfuse prompt name to fetch",
    )
    prompt_label: str = Field(
        default=lf.DEFAULT_LABEL,
        description="Langfuse prompt label (e.g. production)",
    )
    temperature: float = Field(default=0.7, description="Sampling temperature")
    max_tokens: int = Field(default=1024, ge=1, description="Max completion tokens")
    record_to_langfuse: bool = Field(
        default=True,
        description="If true, best-effort POST usage/cost to Langfuse Cloud",
    )
    vault_key: str = Field(
        default=lf.DEFAULT_VAULT_KEY,
        description="Vault credential key holding Langfuse Cloud keys + host",
    )


@motet.command(
    timeout_seconds=120,
    required_capabilities=[WorkerCapability.MODEL_INFERENCE],
)
def agent_turn_with_langfuse_prompt(
    data: AgentTurnWithLangfusePromptData,
    motet: MotetContext,
) -> Dict[str, Any]:
    """
    Run one model turn using a Langfuse Cloud system prompt when available.

    Falls back to the bundle static system prompt if credentials are missing,
    Cloud is unreachable, or the prompt is empty. Optionally records this
    turn's usage/cost to Langfuse Cloud (fail-soft).
    """
    resolved = lf.resolve_turn_system_prompt(
        motet,
        prompt_name=data.prompt_name,
        prompt_label=data.prompt_label,
        vault_key=data.vault_key,
    )
    system_prompt = resolved["system_prompt"]
    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": data.message},
    ]

    result = motet.models.infer(
        provider=data.provider,
        model_name=data.model_name,
        messages=messages,
        temperature=data.temperature,
        max_tokens=data.max_tokens,
    )

    content = ""
    usage: Dict[str, Any] = {}
    cost_usd: Optional[float] = None
    if isinstance(result, dict):
        content = result.get("content") or result.get("response") or ""
        usage = {
            "prompt_tokens": result.get("prompt_tokens"),
            "completion_tokens": result.get("completion_tokens"),
            "total_tokens": result.get("total_tokens"),
        }
        raw_cost = result.get("cost_usd")
        if isinstance(raw_cost, (int, float)):
            cost_usd = float(raw_cost)
    else:
        content = str(result)

    generation_status = "skipped"
    generation_error = None
    generation_ids: Dict[str, Any] = {}
    creds = resolved.get("creds")
    if data.record_to_langfuse and creds:
        try:
            meta = {
                "prompt_name": data.prompt_name,
                "prompt_label": data.prompt_label,
                "prompt_source": resolved["prompt_source"],
                "bundle": "langfuse-cms",
            }
            if isinstance(resolved.get("prompt_meta"), dict):
                meta["langfuse_prompt_version"] = resolved["prompt_meta"].get("version")
            push = lf.record_generation(
                creds,
                model=f"{data.provider}/{data.model_name}",
                input_messages=messages,
                output=content if isinstance(content, str) else str(content),
                usage=usage,
                cost_usd=cost_usd,
                metadata=meta,
            )
            generation_status = "recorded"
            generation_ids = {
                "trace_id": push.get("trace_id"),
                "observation_id": push.get("observation_id"),
            }
        except Exception as exc:
            generation_status = "error"
            generation_error = f"{type(exc).__name__}: {exc}"
    elif data.record_to_langfuse and not creds:
        generation_status = "skipped_no_credentials"

    return {
        "content": content,
        "prompt_source": resolved["prompt_source"],
        "fallback_reason": resolved["fallback_reason"],
        "prompt_name": data.prompt_name,
        "prompt_label": data.prompt_label,
        "prompt_meta": resolved.get("prompt_meta"),
        "provider": data.provider,
        "model_name": data.model_name,
        "usage": usage,
        "cost_usd": cost_usd,
        "langfuse_generation": {
            "status": generation_status,
            "error": generation_error,
            **generation_ids,
        },
    }
