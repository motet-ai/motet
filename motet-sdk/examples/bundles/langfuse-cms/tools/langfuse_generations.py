"""
Motet SDK - Langfuse CMS: Generation Recording Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-05

Description:
Optional tool to push a single generation (usage/cost metadata) to Langfuse
Cloud for this demo agent only. Motet platform cost tracking (ADR-0018)
remains the source of truth; this is a side-channel for operators who want
the same turn visible in Langfuse Cloud.

Dependencies:
- motet_sdk: @motet.tool, get_motet_context
- pydantic: tool parameter schema
- commands/_langfuse: shared ingestion helper

Usage:
  langfuse-cms.record_generation(
      model="openai/gpt-4o-mini",
      output="…",
      prompt_tokens=100,
      completion_tokens=50,
      cost_usd=0.001,
  )

Notes:
- Failures return ok=false; callers should not treat this as fatal.
- The wrapper command also records generations when record_to_langfuse=true.
"""

from __future__ import annotations

import importlib
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from motet_sdk import get_motet_context, motet

_HELPER_MODULE = "bundle.langfuse-cms.commands._langfuse"


def _lf() -> Any:
    return importlib.import_module(_HELPER_MODULE)


class RecordGenerationParams(BaseModel):
    """Input for record_generation."""

    model: str = Field(..., description="Model id as shown to Langfuse (provider/name)")
    output: str = Field(..., description="Model output text")
    input_messages: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Optional chat messages sent to the model",
    )
    prompt_tokens: Optional[int] = Field(default=None, description="Input token count")
    completion_tokens: Optional[int] = Field(default=None, description="Output token count")
    total_tokens: Optional[int] = Field(default=None, description="Total token count")
    cost_usd: Optional[float] = Field(default=None, description="Estimated cost in USD")
    name: str = Field(
        default="langfuse-cms.turn",
        description="Langfuse generation/observation name",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Extra metadata (prompt name/label/source, etc.)",
    )


def _fmt(res: Dict[str, Any]) -> str:
    if not res.get("ok"):
        return f"record_generation(ok=false, error={res.get('error')!r})"
    return f"record_generation(ok=true, trace_id={res.get('trace_id')!r})"


@motet.tool(
    description=(
        "Record one generation (usage/cost) to Langfuse Cloud for this demo "
        "agent. Failures are non-fatal — Motet cost tracking is unchanged."
    ),
    name="record_generation",
    schema=RecordGenerationParams,
    observation_formatter=_fmt,
    category="langfuse-cms",
    cost_class="low",
    keywords=["langfuse", "generation", "cost", "usage", "cloud"],
)
def record_generation(params: Dict[str, Any]) -> Dict[str, Any]:
    """Push a generation event to Langfuse Cloud ingestion."""
    parsed = RecordGenerationParams(**(params or {}))
    helper = _lf()
    try:
        creds = helper.resolve_credentials(get_motet_context(), require_host=True)
        usage = {
            "prompt_tokens": parsed.prompt_tokens,
            "completion_tokens": parsed.completion_tokens,
            "total_tokens": parsed.total_tokens,
        }
        result = helper.record_generation(
            creds,
            model=parsed.model,
            input_messages=list(parsed.input_messages or []),
            output=parsed.output,
            usage=usage,
            cost_usd=parsed.cost_usd,
            name=parsed.name,
            metadata=dict(parsed.metadata or {}),
        )
        return {"ok": True, **result}
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
