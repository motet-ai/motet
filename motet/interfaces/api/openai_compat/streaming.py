"""
Motet - OpenAI Compatible Streaming

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Server-Sent Event plumbing for the OpenAI-compatible facade.

    Provides the SSE frame format OpenAI clients expect, keepalive comments so
    long agent turns do not trip client idle timeouts, a mid-stream error frame
    (the OpenAI wire has no clean error event, so the facade emits a documented
    error chunk and withholds the terminal sentinel), and a consumer for Motet's
    encrypted Redis task stream.

    The task-stream consumer mirrors the orchestrator's reader: token frames are
    the text deltas an OpenAI client renders, and every other Motet event stays
    server-side where it remains fully visible in command events and traces.

Dependencies:
    - motet.core.distributed.redis_manager: async Redis client for XREAD
    - motet.core.security.envelope_decode_helpers: envelope decryption

Usage:
    from motet.interfaces.api.openai_compat import streaming

    async for text in streaming.consume_task_tokens(task_id, command_task):
        yield streaming.sse_data(chunk)

Notes:
    - Frames are ``data: {json}`` lines terminated by ``data: [DONE]``
    - Keepalives are SSE comments, which clients ignore but proxies count as traffic
    - Decrypt failures skip the frame rather than terminating the stream
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncGenerator, Dict, Optional

import structlog

from .errors import error_payload

logger = structlog.get_logger(__name__)

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

DONE_SENTINEL = b"data: [DONE]\n\n"


def sse_data(payload: Dict[str, Any]) -> bytes:
    """Render one SSE data frame."""
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


def sse_named(event: str, payload: Dict[str, Any]) -> bytes:
    """Render one named SSE frame, as the Responses API uses."""
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")


def sse_keepalive() -> bytes:
    """Render an SSE comment that keeps intermediaries from idling out."""
    return b": keepalive\n\n"


def sse_error(message: str, *, error_type: str = "api_error", code: Optional[str] = None) -> bytes:
    """Render a mid-stream error frame.

    OpenAI's stream format has no error event, so clients differ in how they
    surface this. The facade emits the error body and then withholds
    ``[DONE]`` so a client that ignores the frame still sees an aborted stream
    rather than a silently truncated success.
    """
    return sse_data(error_payload(message, error_type=error_type, code=code))


async def with_keepalive(
    source: AsyncGenerator[bytes, None],
    interval_seconds: float,
) -> AsyncGenerator[bytes, None]:
    """Interleave keepalive comments into an SSE body during quiet periods.

    Agent turns can spend a minute in tool execution without emitting a token.
    Without traffic, proxies and client HTTP stacks close the connection, so the
    facade emits comment frames while it waits (ADR-0125 §5f).
    """
    if interval_seconds <= 0:
        async for item in source:
            yield item
        return

    queue: "asyncio.Queue[Any]" = asyncio.Queue()
    sentinel = object()

    async def pump() -> None:
        try:
            async for item in source:
                await queue.put(item)
        except Exception as exc:  # forwarded to the consumer below
            await queue.put(exc)
        finally:
            await queue.put(sentinel)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                yield sse_keepalive()
                continue
            if item is sentinel:
                break
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        pump_task.cancel()


def _field_str(fields: Any, key: str) -> str:
    """Read a Redis stream field as text regardless of byte/str decoding."""
    if not isinstance(fields, dict):
        return ""
    value = fields.get(key)
    if value is None:
        value = fields.get(key.encode() if isinstance(key, str) else key)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value) if value is not None else ""


async def consume_task_events(
    task_id: str,
    command_task: "asyncio.Task[Any]",
    *,
    idle_timeout_seconds: float = 300.0,
    tenant_id: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Yield decoded events from a command's Redis task stream.

    Reads until the command task finishes and the stream drains, or until no
    frame arrives for ``idle_timeout_seconds``. Each yielded item is
    ``{"event": str, "payload": dict}``.
    """
    from motet.core.distributed.tenant_keys import task_response_stream

    stream_key = task_response_stream(tenant_id, task_id)
    from ....core.distributed.redis_manager import get_redis_client

    redis_client = get_redis_client()
    last_id = "0"
    last_activity = time.monotonic()

    while True:
        try:
            streams = await redis_client.xread({stream_key: last_id}, count=25, block=1000)
        except Exception as exc:
            logger.error(
                "openai_compat_stream_read_failed",
                task_id=task_id,
                error=str(exc),
                exc_info=True,
            )
            break

        if not streams:
            if command_task.done():
                break
            if time.monotonic() - last_activity > idle_timeout_seconds:
                logger.warning("openai_compat_stream_idle_timeout", task_id=task_id)
                break
            continue

        last_activity = time.monotonic()
        for _stream_name, entries in streams:
            for message_id, fields in entries:
                last_id = message_id
                event = _field_str(fields, "event")
                envelope = _field_str(fields, "_envelope")

                payload: Dict[str, Any] = {}
                if envelope:
                    try:
                        from ....core.security.envelope_decode_helpers import (
                            decode_command_stream_envelope,
                        )

                        payload = decode_command_stream_envelope(
                            envelope_json=envelope,
                            stream_key=stream_key,
                            event=event,
                            task_id=task_id,
                            command_id=_field_str(fields, "command_id"),
                            tenant_id=_field_str(fields, "tenant_id"),
                            motet_id=_field_str(fields, "motet_id") or "default",
                        )
                    except Exception as exc:
                        logger.error(
                            "openai_compat_stream_decrypt_failed",
                            task_id=task_id,
                            event=event,
                            error=str(exc),
                            exc_info=True,
                        )
                        continue

                yield {"event": event, "payload": payload}

                if event in ("stream_complete", "end"):
                    return


async def consume_task_tokens(
    task_id: str,
    command_task: "asyncio.Task[Any]",
    *,
    tenant_id: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Yield only assistant text deltas from a command's task stream."""
    async for item in consume_task_events(
        task_id, command_task, tenant_id=tenant_id
    ):
        if item["event"] == "token":
            data = item["payload"].get("data", "")
            if data:
                yield str(data)


__all__ = [
    "DONE_SENTINEL",
    "SSE_HEADERS",
    "consume_task_events",
    "consume_task_tokens",
    "sse_data",
    "sse_error",
    "sse_keepalive",
    "sse_named",
    "with_keepalive",
]
