"""
Motet - Agent Turn Prepare Helpers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-29

Description:
    Prepare-phase helpers for `agent_turn` (GitHub issue #147 factorization).
    Covers message normalization, model/tool policy resolution, input-text
    extraction, and tool-schema ensure helpers used before/during turn hooks.
    Extracted from turn/command.py with no behavior change.

Dependencies:
    - motet.core.types.Message: Canonical message model
    - motet.core.types reasoning-effort helpers: Normalize effort ladder values
    - structlog: Structured warning logs for unrecognized overrides

Usage:
    from motet.core.orchestration.turn.prepare import (
        extract_turn_input_text,
        resolve_turn_model_policy,
        sanitize_message_attachments,
        to_turn_messages,
        ensure_tool_schema,
        ensure_required_tool,
    )

    history = to_turn_messages(raw_messages)
    policy = resolve_turn_model_policy(effective_context, agent_config, stack_cfg)
    input_text = extract_turn_input_text(history)

Notes:
    - Agent resolve / conversation prefix / authorize / registry touch stay in
      agent_turn; this module only shrinks the prepare body.
    - Tool-schema helpers are imported by ``turn/hooks.py`` so hooks can pin
      skill/pending tools without a turn↔hooks cycle.
    - `_coerce_reasoning_effort` is re-exported via turn/__init__.py / orchestration.py
      for existing test patch/import points.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import structlog

from motet.core.types import (
    REASONING_EFFORT_LADDER,
    Message,
    ReasoningEffort,
    normalize_reasoning_effort,
    tool_schema_name,
)
from motet.core.models.adapters.tool_call_codec import tool_calls_from_message
from motet.core.reasoning.react.agent_data import (
    DEFAULT_MODEL_NAME,
    DEFAULT_MODEL_PROVIDER,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class TurnModelPolicy:
    """Resolved provider/model/thinking policy for one agent turn."""

    provider: Any
    model_name: Any
    model_profile_name: Any
    enable_thinking: bool
    reasoning_effort: ReasoningEffort


def coerce_reasoning_effort(value: Any) -> ReasoningEffort:
    """
    Normalize config/context values to the canonical reasoning-effort ladder.

    Request-context overrides are not schema-validated, so an unusable value still has
    to resolve to something. It is logged rather than silently swallowed: falling back
    to the default can move effort in either direction, which is confusing to debug
    from the outside when a caller asked for more reasoning and observed less.
    """
    if value is None:
        return "medium"
    effort = normalize_reasoning_effort(value, default="medium")
    if not (isinstance(value, str) and value.strip().lower() == effort):
        logger.warning(
            "reasoning_effort_override_unrecognized",
            requested=str(value)[:32],
            resolved=effort,
            supported=list(REASONING_EFFORT_LADDER),
        )
    return effort


# Compatibility alias — tests and orchestration re-export the underscored name.
_coerce_reasoning_effort = coerce_reasoning_effort


def sanitize_message_attachments(value: Any) -> Optional[List[Dict[str, Any]]]:
    """Coerce message attachments to a list of dicts or None.

    Workflow templates that reference an optional step (e.g.
    ``{{ingest_refs.attachments}}`` with continue_on_failure) can arrive
    as an unsubstituted literal string or other junk; a hard pydantic
    failure here would take down the whole agent turn.
    """
    if value is None:
        return None
    if isinstance(value, list):
        items = [item for item in value if isinstance(item, dict)]
        return items or None
    logger.warning(
        "agent_turn_dropping_non_list_attachments",
        attachments_type=type(value).__name__,
        attachments_preview=str(value)[:120],
    )
    return None


def _metadata_without_display_thinking(metadata: Any) -> Dict[str, Any]:
    """Copy message metadata without conversation-reload display fields."""
    if not isinstance(metadata, dict):
        return {}
    cleaned = dict(metadata)
    cleaned.pop("thinking_text", None)
    cleaned.pop("tool_summaries", None)
    cleaned.pop("cost_usd", None)
    cleaned.pop("spawn_children", None)
    return cleaned


def to_turn_messages(raw_messages: List[Any]) -> List[Message]:
    """Normalize heterogeneous inbound messages to canonical ``Message`` models."""
    normalized: List[Message] = []
    for msg in raw_messages or []:
        if isinstance(msg, Message):
            normalized.append(
                Message(
                    role=msg.role,
                    content=msg.content,
                    content_parts=getattr(msg, "content_parts", None),
                    name=getattr(msg, "name", None),
                    metadata=_metadata_without_display_thinking(getattr(msg, "metadata", None)),
                    tool_calls_canonical=tool_calls_from_message(msg) or None,
                    tool_call_id=getattr(msg, "tool_call_id", None),
                    attachments=getattr(msg, "attachments", None),
                    reasoning_content=getattr(msg, "reasoning_content", None),
                    reasoning_blocks=getattr(msg, "reasoning_blocks", None),
                )
            )
            continue
        if isinstance(msg, dict):
            normalized.append(
                Message(
                    role=str(msg.get("role", "user")),
                    content=str(msg.get("content", "")),
                    content_parts=msg.get("content_parts"),
                    name=msg.get("name"),
                    metadata=_metadata_without_display_thinking(msg.get("metadata")),
                    tool_calls_canonical=tool_calls_from_message(msg) or None,
                    tool_call_id=msg.get("tool_call_id"),
                    attachments=sanitize_message_attachments(msg.get("attachments")),
                    reasoning_content=msg.get("reasoning_content"),
                    reasoning_blocks=msg.get("reasoning_blocks"),
                )
            )
            continue
        normalized.append(
            Message(
                role=getattr(msg, "role", "user"),
                content=getattr(msg, "content", ""),
                content_parts=getattr(msg, "content_parts", None),
                name=getattr(msg, "name", None),
                metadata=_metadata_without_display_thinking(getattr(msg, "metadata", None)),
                tool_calls_canonical=tool_calls_from_message(msg) or None,
                tool_call_id=getattr(msg, "tool_call_id", None),
                attachments=sanitize_message_attachments(getattr(msg, "attachments", None)),
                reasoning_content=getattr(msg, "reasoning_content", None),
                reasoning_blocks=getattr(msg, "reasoning_blocks", None),
            )
        )
    return normalized


def resolve_turn_model_policy(
    effective_context: Dict[str, Any],
    agent_config: Any,
    stack_cfg: Any,
) -> TurnModelPolicy:
    """Build model policy from request context over config defaults over stack defaults."""
    provider = (
        effective_context.get("model_provider")
        or getattr(agent_config, "model_provider", None)
        or getattr(stack_cfg, "model_provider", None)
        or DEFAULT_MODEL_PROVIDER
    )
    model_name = (
        effective_context.get("model_name")
        or getattr(agent_config, "model_name", None)
        or getattr(stack_cfg, "model_name", None)
        or DEFAULT_MODEL_NAME
    )
    # Test stacks often set MOTET_MODEL_PROVIDER=mock while leaving model_name
    # at the OpenAI default; pair with the registered mock ModelSpec.
    if provider == "mock" and model_name:
        name = str(model_name).strip()
        if not name or name.startswith("gpt-") or name.startswith("o1") or name.startswith("o3"):
            model_name = "mock-small"
    model_profile_name = (
        effective_context.get("model_profile_name")
        or getattr(agent_config, "model_profile_name", None)
        or getattr(stack_cfg, "model_profile_name", None)
    )
    enable_thinking = bool(
        effective_context.get("enable_thinking")
        if "enable_thinking" in effective_context
        else getattr(agent_config, "enable_thinking", False)
    )
    reasoning_effort = coerce_reasoning_effort(
        effective_context.get("reasoning_effort")
        or getattr(agent_config, "reasoning_effort", "medium")
        or "medium"
    )
    return TurnModelPolicy(
        provider=provider,
        model_name=model_name,
        model_profile_name=model_profile_name,
        enable_thinking=enable_thinking,
        reasoning_effort=reasoning_effort,
    )


def extract_turn_input_text(history: Sequence[Message]) -> str:
    """Determine current turn input from the last user message (else last message)."""
    input_text = ""
    for msg in reversed(history):
        if msg.role == "user":
            input_text = msg.content or ""
            break
    if not input_text and history:
        input_text = history[-1].content or ""
    return input_text


def ensure_tool_schema(
    schemas: Optional[List[Any]],
    tool_name: str,
    schema_exporter: Any,
) -> Optional[List[Any]]:
    """Append a tool schema if missing; ``None`` schemas stay ``None`` (unbounded)."""
    if schemas is None:
        return None
    if any(tool_schema_name(s) == tool_name for s in schemas):
        return schemas
    extra = schema_exporter.export_canonical(preselected_tools=[tool_name], max_tools=1)
    if extra:
        return list(schemas) + list(extra)
    return schemas


def ensure_required_tool(metadata_block: Dict[str, Any], tool_name: str) -> Dict[str, Any]:
    """Ensure ``tool_name`` appears in discovery-filter ``required_tools``."""
    out = dict(metadata_block or {})
    required = list(out.get("required_tools") or [])
    if tool_name not in required:
        required.append(tool_name)
    out["required_tools"] = required
    return out


__all__ = [
    "TurnModelPolicy",
    "coerce_reasoning_effort",
    "_coerce_reasoning_effort",
    "sanitize_message_attachments",
    "to_turn_messages",
    "resolve_turn_model_policy",
    "extract_turn_input_text",
    "tool_schema_name",
    "ensure_tool_schema",
    "ensure_required_tool",
]
