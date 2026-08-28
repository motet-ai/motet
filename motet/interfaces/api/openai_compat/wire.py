"""
Motet - OpenAI Compatible Wire Models

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Inbound request models for the OpenAI-compatible facade, plus id
    and timestamp helpers for building OpenAI-shaped responses.

    These models describe the OpenAI wire contract only. They exist at the HTTP
    edge and are translated to canonical Motet types before any orchestration
    runs, preserving the boundary. Models accept unknown fields because
    third-party clients routinely send provider-specific extras that must not
    fail the request.

Dependencies:
    - pydantic: request validation with permissive extras
    - motet.core.security.facade_policy: mode extension parsing

Usage:
    from motet.interfaces.api.openai_compat.wire import ChatCompletionRequest

    req = ChatCompletionRequest.model_validate(body)
    if req.is_responses_shaped():
        req = req.normalize_responses_shape()

Notes:
    - Cursor may post Responses-shaped bodies to /chat/completions
    - Unsupported parameters are rejected explicitly in translation, not ignored
    - Motet extensions are namespaced with a motet_ prefix to avoid wire collisions
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


def new_completion_id() -> str:
    """Generate an OpenAI-style chat completion id."""
    return f"chatcmpl-{uuid.uuid4().hex}"


def new_response_id() -> str:
    """Generate an OpenAI-style Responses API id."""
    return f"resp_{uuid.uuid4().hex}"


def new_message_id() -> str:
    """Generate an OpenAI-style output message item id."""
    return f"msg_{uuid.uuid4().hex}"


def new_call_id() -> str:
    """Generate an OpenAI-style tool call id."""
    return f"call_{uuid.uuid4().hex[:24]}"


def now_ts() -> int:
    """Current unix timestamp in seconds."""
    return int(time.time())


class _WireModel(BaseModel):
    """Base for inbound wire models: tolerate unknown client fields."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class StreamOptions(_WireModel):
    """OpenAI stream_options object."""

    include_usage: bool = Field(
        default=False,
        description="Emit a final chunk carrying token usage before the terminal sentinel.",
        json_schema_extra={"example": True},
    )


class ChatCompletionRequest(_WireModel):
    """OpenAI Chat Completions request body.

    Also accepts Responses-shaped bodies (an ``input`` field instead of
    ``messages``) because Cursor sends them to this path (ADR-0125 §9).
    """

    model: str = Field(
        ...,
        description="Facade model id, normally 'provider/registry_key'.",
        json_schema_extra={"example": "openai/gpt-4o-mini"},
    )
    messages: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Chat messages in OpenAI format.",
        json_schema_extra={"example": [{"role": "user", "content": "Hello"}]},
    )
    input: Optional[Union[str, List[Any]]] = Field(
        default=None,
        description="Responses-API style input; accepted for Cursor compatibility.",
        json_schema_extra={"example": "Hello"},
    )
    instructions: Optional[str] = Field(
        default=None,
        description="Responses-API style system instructions.",
        json_schema_extra={"example": "You are a helpful assistant."},
    )
    stream: bool = Field(
        default=False,
        description="Stream the response as Server-Sent Events.",
        json_schema_extra={"example": False},
    )
    stream_options: Optional[StreamOptions] = Field(
        default=None,
        description="Streaming behavior options such as include_usage.",
    )
    temperature: Optional[float] = Field(
        default=None,
        description="Sampling temperature forwarded to the resolved model.",
        json_schema_extra={"example": 0.7},
    )
    top_p: Optional[float] = Field(
        default=None,
        description="Nucleus sampling parameter; forwarded but not honored by all adapters.",
        json_schema_extra={"example": 1.0},
    )
    max_tokens: Optional[int] = Field(
        default=None,
        description="Maximum output tokens (legacy field name).",
        json_schema_extra={"example": 1024},
    )
    max_completion_tokens: Optional[int] = Field(
        default=None,
        description="Maximum output tokens (current field name); takes precedence over max_tokens.",
        json_schema_extra={"example": 1024},
    )
    max_output_tokens: Optional[int] = Field(
        default=None,
        description="Responses-API maximum output tokens.",
        json_schema_extra={"example": 1024},
    )
    n: Optional[int] = Field(
        default=None,
        description="Number of choices. Only 1 is supported; larger values are rejected.",
        json_schema_extra={"example": 1},
    )
    stop: Optional[Union[str, List[str]]] = Field(
        default=None,
        description="Stop sequences; forwarded but not honored by all adapters.",
    )
    seed: Optional[int] = Field(
        default=None,
        description="Sampling seed; forwarded but not honored by all adapters.",
    )
    logprobs: Optional[bool] = Field(
        default=None,
        description="Log probabilities are not produced by Motet and are rejected when requested.",
    )
    top_logprobs: Optional[int] = Field(
        default=None,
        description="Log probabilities are not produced by Motet and are rejected when requested.",
    )
    tools: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Client-declared tools in OpenAI function format.",
    )
    tool_choice: Optional[Union[str, Dict[str, Any]]] = Field(
        default=None,
        description="Tool selection hint. 'none' suppresses tools; other values are advisory.",
    )
    parallel_tool_calls: Optional[bool] = Field(
        default=None,
        description="Whether the model may emit multiple tool calls per turn.",
    )
    response_format: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Structured output request mapped to a Motet OutputContract.",
        json_schema_extra={"example": {"type": "json_object"}},
    )
    text: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Responses-API text.format structured output request.",
    )
    user: Optional[str] = Field(
        default=None,
        description="Opaque end-user identifier supplied by the client.",
    )
    conversation: Optional[Union[str, Dict[str, Any]]] = Field(
        default=None,
        description="OpenAI conversation id mapped to a Motet conversation.",
        json_schema_extra={"example": "conv_abc123"},
    )
    previous_response_id: Optional[str] = Field(
        default=None,
        description="Prior response id used to continue a Motet conversation."
    )
    motet_mode: Optional[str] = Field(
        default=None,
        description=(
            "Motet extension: requested facade mode. Only honored when request overrides are "
            "enabled, and never above the credential's bound mode."
        ),
        json_schema_extra={"example": "agent"},
    )
    motet_conversation_id: Optional[str] = Field(
        default=None,
        description="Motet extension: explicit Motet conversation id.",
    )
    motet_agent_id: Optional[str] = Field(
        default=None,
        description="Motet extension: agent id to run in agent mode.",
        json_schema_extra={"example": "core.default"},
    )

    def is_responses_shaped(self) -> bool:
        """Whether this body arrived in Responses API shape."""
        return not self.messages and self.input is not None

    def effective_max_output_tokens(self) -> Optional[int]:
        """Resolve the output token cap across the three spellings clients use."""
        return self.max_completion_tokens or self.max_output_tokens or self.max_tokens


class ResponsesRequest(ChatCompletionRequest):
    """OpenAI Responses API request body.

    Shares the Chat Completions field set: the facade normalizes both shapes to
    canonical messages, and clients mix fields across the two APIs in practice.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)


__all__ = [
    "ChatCompletionRequest",
    "ResponsesRequest",
    "StreamOptions",
    "new_call_id",
    "new_completion_id",
    "new_message_id",
    "new_response_id",
    "now_ts",
]
