"""
Motet - Distributed Orchestrator

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-29

Description:
    Distributed orchestrator entrypoint used by the stack and chat API. It builds
    turn commands, executes them through the distributed invoker, and streams
    events/tokens back to callers via a dedicated Redis stream consumer.
    Forwards the terminal `suspended` event and ends
    the stream on it, mirroring `end`/`error`. Forwards ``usage`` after each
    priced model fold (running token envelope plus optional ``cost_usd``).

Dependencies:
    - Distributed command system and global invoker
    - Redis-backed task stream consumption
    - Event observers and global event bus
    - Encrypted stream envelopes for payload security

Usage:
    from motet.core.orchestration.orchestrator import DistributedOrchestrator

    orchestrator = DistributedOrchestrator()
    response = await orchestrator.run(stack, messages)

Notes:
    - All execution goes through distributed command dispatch
    - Stream consumer (_consume_command_stream) handles Redis XREAD, envelope
      decryption, and event-type dispatch — extracted for testability
    - Provides async response and streaming interfaces (run, stream_events, stream)
"""

import asyncio
import uuid
from typing import Any, Dict, List, Optional, AsyncGenerator, cast
from pydantic import BaseModel
from enum import Enum
import structlog

from motet.core.reasoning.react.loop_results import priced_cost_usd
from motet.core.types import Message
from motet.core.constants import DEFAULT_REDIS_URL
from motet.core.commands.distributed import DistributedCommand
# Turn state and orchestrator config are defined locally for this module.
class TurnState(Enum):
    """Turn execution states"""
    PREPARING = "PREPARING"
    THINKING = "THINKING"
    RESPONDING = "RESPONDING"
    COMPLETING = "COMPLETING"

class OrchestrationConfig(BaseModel):
    """Configuration for orchestration behavior"""
    enable_observers: bool = True

from ..workers import global_invoker

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Module-level helpers for Redis stream field parsing (used by the stream
# consumer inside DistributedOrchestrator).
# ---------------------------------------------------------------------------

def _field_value(fields_map: Any, key: str) -> Any:
    """Get a Redis stream field value supporting str/bytes keys."""
    if not isinstance(fields_map, dict):
        return None
    v = fields_map.get(key)
    if v is None:
        v = fields_map.get(key.encode("utf-8"))
    return v


def _field_str(fields_map: Any, key: str) -> str:
    """Get a Redis stream field value as a decoded str."""
    v = _field_value(fields_map, key)
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", errors="ignore")
    return str(v)


def _with_stream_agent_id(event_dict: Dict[str, Any], fields_map: Any) -> Dict[str, Any]:
    """Attach plaintext agent_id / parent_agent_id from Redis fields when present."""
    aid = _field_str(fields_map, "agent_id")
    parent = _field_str(fields_map, "parent_agent_id")
    if not aid and not parent:
        return event_dict
    out = dict(event_dict)
    if aid:
        out["agent_id"] = aid
    if parent:
        out["parent_agent_id"] = parent
    return out


def _event_json_data(payload_obj: Dict[str, Any], fields_map: Any) -> Dict[str, Any]:
    """Extract the structured ``data`` body from a stream event.

    Prefers the decrypted *payload_obj*; falls back to the plaintext ``data``
    field in *fields_map* for legacy messages.
    """
    from motet.core.security.envelope_decode_helpers import parse_json_maybe

    if payload_obj:
        data_v = payload_obj.get("data")
        if isinstance(data_v, dict):
            return data_v
        if isinstance(data_v, str):
            return parse_json_maybe(data_v)
        return {}
    return parse_json_maybe(_field_str(fields_map, "data"))


class DistributedOrchestrator:
    """
    Main orchestrator for distributed AI stack operations.
    
    Responsibilities:
    - Build and dispatch turn commands via the distributed invoker
    - Consume the Redis task stream and yield decoded events
    - Provide convenience facades (run, stream_events, stream)
    - Initialize event-bus observers for cross-cutting concerns
    """
    
    def __init__(
        self,
        redis_url: str = DEFAULT_REDIS_URL,
        config: Optional[OrchestrationConfig] = None,
    ):
        self.redis_url = redis_url
        self.config = config or OrchestrationConfig()
        
        # Request-scoped stack reference (identity/config source for a turn).
        self.local_stack = None
        
        # Core orchestration patterns - using pure distributed invoker
        self.command_invoker = global_invoker
        
        # Observers
        self.observers = []
        
        # Turn-based state management
        self.state: TurnState = TurnState.PREPARING
        self._role: str | None = None
    
    async def initialize(self, local_stack=None):
        """Initialize the distributed orchestrator."""
        self.local_stack = local_stack
        
        # Initialize observers for event bus integration.
        await self._initialize_observers(local_stack)
        logger.debug("distributed_orchestrator_initialized")
    
    async def _maybe_publish_event(self, payload: Dict[str, Any]) -> None:
        """Publish event to global bus for /events endpoint compatibility."""
        try:
            from ..workers import global_bus
            global_bus.publish(payload)
        except Exception as e:
            logger.debug("event_publish_failed", kind=payload.get("kind"), error=str(e), exc_info=True)
            # Don't fail on event publishing
    
    def execute_command(self, command: DistributedCommand) -> Dict[str, Any]:
        """Execute distributed command via pure distributed invoker with full observability."""
        return self.command_invoker.execute_command(command)
    
    # ------------------------------------------------------------------
    # Stream consumer — reads the Redis task stream, decrypts envelopes,
    # dispatches typed events, and yields them to callers.
    # ------------------------------------------------------------------

    async def _consume_command_stream(
        self,
        task_id: str,
        conversation_id: str,
        turn_task: asyncio.Task,
        tenant_id: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Read the Redis task stream for *task_id* and yield decoded events.

        Handles encrypted envelope decryption (ADR-0050) and translates
        stream entries into the standard event dicts consumed by callers
        of ``stream_events`` and ``run``.
        """
        from motet.core.distributed.tenant_keys import task_response_stream

        stream_key = task_response_stream(tenant_id, task_id)
        from ..distributed.redis_manager import get_redis_client
        redis_client = get_redis_client()

        last_id = "0"
        stream_ended = False
        current_state = TurnState.PREPARING

        try:
            while not stream_ended:
                streams = await redis_client.xread({stream_key: last_id}, count=25, block=1000)

                if not streams:
                    if turn_task.done():
                        break
                    continue

                for _stream_name, messages_list in streams:
                    for message_id, fields in messages_list:
                        last_id = message_id

                        event = _field_str(fields, "event")
                        envelope_str = _field_str(fields, "_envelope")

                        command_id_norm = _field_str(fields, "command_id")
                        motet_id_norm = _field_str(fields, "motet_id") or "default"
                        tenant_id_norm = _field_str(fields, "tenant_id") or ""

                        payload: Dict[str, Any] = {}
                        if envelope_str:
                            try:
                                from motet.core.security.envelope_decode_helpers import (
                                    decode_command_stream_envelope,
                                )
                                payload = decode_command_stream_envelope(
                                    envelope_json=envelope_str,
                                    stream_key=stream_key,
                                    event=event,
                                    task_id=task_id,
                                    command_id=command_id_norm,
                                    tenant_id=tenant_id_norm,
                                    motet_id=motet_id_norm,
                                )
                            except Exception as e:
                                logger.error(
                                    "command_stream_decrypt_failed",
                                    error=str(e),
                                    event=event,
                                    task_id=task_id,
                                    command_id=command_id_norm,
                                    tenant_id=tenant_id_norm,
                                    motet_id=motet_id_norm,
                                    available_fields=list(fields.keys()) if isinstance(fields, dict) else [],
                                    exc_info=True,
                                )
                                continue

                        if event == "turn":
                            state = payload.get("state", "")
                            if state == "PREPARING":
                                current_state = TurnState.PREPARING
                            elif state == "THINKING":
                                current_state = TurnState.THINKING
                            elif state == "RESPONDING":
                                current_state = TurnState.RESPONDING
                            elif state == "COMPLETING":
                                current_state = TurnState.COMPLETING
                            self.state = current_state

                            await self._maybe_publish_event({
                                "kind": "task_state_changed",
                                "source": "distributed_orchestrator",
                                "task_id": task_id,
                                "new_state": state,
                                "conversation_id": conversation_id,
                            })
                            yield _with_stream_agent_id({"event": "turn", "state": state}, fields)

                        elif event == "token":
                            yield _with_stream_agent_id(
                                {"event": "token", "data": payload.get("data", "")},
                                fields,
                            )

                        elif event == "end":
                            content = payload.get("content", "") or ""
                            await self._maybe_publish_event({
                                "kind": "task_completed",
                                "source": "distributed_orchestrator",
                                "task_id": task_id,
                                "final_state": "completed",
                                "conversation_id": conversation_id,
                                "response_length": len(content),
                            })
                            end_event = {
                                "event": "end",
                                **{
                                    k: v
                                    for k, v in payload.items()
                                    if k not in {"event"}
                                },
                                "content": content,
                            }
                            yield _with_stream_agent_id(end_event, fields)
                            stream_ended = True
                            break

                        elif event in (
                            "conversation_analyzed", "reasoning", "reasoning_meta",
                            "reasoning_step", "workflow_step",
                            "tool_execution_started", "tool_execution_completed",
                            "tool_execution_failed",
                        ):
                            yield _with_stream_agent_id(
                                {"event": event, "data": _event_json_data(payload, fields)},
                                fields,
                            )

                        elif event == "thinking":
                            text = payload.get("text", "") if payload else _field_str(fields, "text")
                            is_complete = payload.get("is_complete", False) if payload else False
                            yield _with_stream_agent_id(
                                {"event": "thinking", "text": text, "is_complete": is_complete},
                                fields,
                            )

                        elif event == "usage":
                            usage_data: Dict[str, Any] = dict(payload or {})
                            if not usage_data and isinstance(fields, dict):
                                for key, value in fields.items():
                                    if key in ("event", b"event", "_envelope", b"_envelope"):
                                        continue
                                    decoded_key = key.decode("utf-8") if isinstance(key, bytes) else str(key)
                                    decoded_value = value.decode("utf-8") if isinstance(value, bytes) else value
                                    usage_data[decoded_key] = decoded_value
                            cost = priced_cost_usd(usage_data.get("cost_usd"))
                            if cost is not None:
                                usage_data["cost_usd"] = cost
                            else:
                                usage_data.pop("cost_usd", None)
                            yield _with_stream_agent_id({"event": "usage", **usage_data}, fields)

                        elif event == "tool_call_delta":
                            # Argument fragments for a tool call still being generated.
                            # Progress only — the authoritative call arrives with the
                            # loop's result, so consumers may ignore these entirely.
                            yield _with_stream_agent_id(
                                {
                                    "event": "tool_call_delta",
                                    "call_id": payload.get("call_id", "") if payload else "",
                                    "tool_name": payload.get("tool_name") if payload else None,
                                    "arguments_delta": (
                                        payload.get("arguments_delta") if payload else None
                                    ),
                                },
                                fields,
                            )

                        elif event == "suspended":
                            # ADR-0127: turn suspended on externally-owned tool
                            # calls — terminal for this stream (like `end`), but
                            # the turn resumes later via resume_turn.
                            suspend_data: Dict[str, Any] = dict(payload or {})
                            if not suspend_data and isinstance(fields, dict):
                                for key, value in fields.items():
                                    if key in ("event", b"event", "_envelope", b"_envelope"):
                                        continue
                                    decoded_key = key.decode("utf-8") if isinstance(key, bytes) else str(key)
                                    decoded_value = value.decode("utf-8") if isinstance(value, bytes) else value
                                    suspend_data[decoded_key] = decoded_value
                            await self._maybe_publish_event({
                                "kind": "task_suspended",
                                "source": "distributed_orchestrator",
                                "task_id": task_id,
                                "final_state": "suspended",
                                "conversation_id": conversation_id,
                                "checkpoint_id": str(suspend_data.get("checkpoint_id") or ""),
                            })
                            yield _with_stream_agent_id({"event": "suspended", **suspend_data}, fields)
                            stream_ended = True
                            break

                        elif event == "auth_required":
                            auth_data: Dict[str, Any] = {}
                            if payload:
                                auth_data = payload
                            else:
                                for key, value in fields.items():
                                    if key in ("event", "event".encode(), "_envelope", "_envelope".encode()):
                                        continue
                                    decoded_key = key.decode("utf-8") if isinstance(key, bytes) else key
                                    decoded_value = value.decode("utf-8") if isinstance(value, bytes) else value
                                    auth_data[decoded_key] = decoded_value
                            yield _with_stream_agent_id({"event": "auth_required", **auth_data}, fields)

                        elif event == "error":
                            error = ""
                            if payload:
                                error = str(payload.get("error") or payload.get("message") or "")
                            if not error:
                                error = _field_str(fields, "error")
                            yield _with_stream_agent_id({"event": "error", "error": error}, fields)
                            stream_ended = True
                            break

                        else:
                            event_data: Dict[str, Any] = dict(payload or {})
                            if isinstance(fields, dict):
                                for key, value in fields.items():
                                    if key in ("event", b"event", "_envelope", b"_envelope"):
                                        continue
                                    decoded_key = key.decode("utf-8") if isinstance(key, bytes) else str(key)
                                    decoded_value = value.decode("utf-8") if isinstance(value, bytes) else value
                                    event_data.setdefault(decoded_key, decoded_value)
                            yield {"event": event, **event_data}

            await turn_task
        finally:
            await redis_client.close()

    def set_role(self, role: str | None) -> None:
        """Set role hint for the current orchestrator session."""
        self._role = role
    
    async def run(self, stack, messages: List[Message], context: Optional[dict] = None):
        """Run a turn and aggregate streamed tokens into a final response (ADR-0078: uses stream_events path)."""
        task_id = getattr(stack, '_current_trace_id', None) or str(uuid.uuid4())

        final_text_parts: List[str] = []
        final_metadata: Dict[str, Any] = {}
        try:
            async for ev in self.stream_events(stack, messages, context=context):
                if ev.get("event") == "token":
                    data = ev.get("data")
                    if isinstance(data, str):
                        final_text_parts.append(data)
                elif ev.get("event") == "end":
                    # We can also get the final content from the end event
                    final_content = ev.get("content")
                    if final_content and not final_text_parts:
                        final_text_parts.append(final_content)
                    final_metadata = {
                        k: v
                        for k, v in ev.items()
                        if k not in {"event", "content"}
                    }
                elif ev.get("event") == "suspended":
                    # ADR-0127: terminal handback — keep checkpoint_id and
                    # handed_back_tool_calls in raw metadata so non-streaming
                    # callers (e.g. the OpenAI facade) can return tool_calls.
                    final_content = ev.get("content")
                    if final_content and not final_text_parts:
                        final_text_parts.append(final_content)
                    final_metadata = {
                        k: v
                        for k, v in ev.items()
                        if k not in {"event", "content"}
                    }
                    final_metadata["suspended"] = True
                elif ev.get("event") == "error":
                    err = ev.get("error", "Unknown streaming error")
                    from motet.core.conversations.ownership import (
                        ConversationAccessDenied,
                        is_conversation_access_denied,
                    )

                    if is_conversation_access_denied(err):
                        raise ConversationAccessDenied(str(err))
                    raise Exception(err)
            
            final_response = "".join(final_text_parts)

            # Prefer the turn-aggregated usage envelope from agent_turn's terminal
            # end event (ADR-0125). Fall back to leaving usage unset when absent.
            usage = final_metadata.get("usage")
            usage_in: Optional[int] = None
            usage_out: Optional[int] = None
            if isinstance(usage, dict):
                if usage.get("prompt_tokens") is not None:
                    usage_in = int(usage.get("prompt_tokens") or 0)
                if usage.get("completion_tokens") is not None:
                    usage_out = int(usage.get("completion_tokens") or 0)

            # Return final response
            from motet.core.types import Response
            return Response(
                content=final_response,
                usage_tokens_input=usage_in,
                usage_tokens_output=usage_out,
                raw={"task_id": task_id, "streaming_path": True, **final_metadata},
            )

        except Exception as e:
            from motet.core.conversations.ownership import (
                ConversationAccessDenied,
                is_conversation_access_denied,
            )

            # Issue #139: never soft-swallow ownership denials into a 200 chat reply.
            if isinstance(e, ConversationAccessDenied) or is_conversation_access_denied(e):
                raise ConversationAccessDenied(str(e)) from e

            logger.error(
                "distributed_orchestrator_run_failed",
                error=str(e),
                error_type=type(e).__name__,
                task_id=task_id,
                exc_info=True,
            )
            
            # Return error response
            from motet.core.types import Response
            return Response(
                content=f"I apologize, but I encountered an error while processing your request: {str(e)}",
                raw={"error": str(e), "task_id": task_id}
            )
    
    async def stream_events(self, stack, messages: List[Message], context: Optional[dict] = None):
        """Stream events from distributed processing (ADR-0078: unified path via agent_config in context)."""
        try:
            self.local_stack = stack
            task_id = getattr(stack, "_current_trace_id", None) or str(uuid.uuid4())
            ctx = context or {}
            conversation_id = ctx.get("conversation_id", "") or getattr(stack, "_current_conversation_id", "") or ""

            # Phase 2+: always route chat turns through core.agent_turn root command.
            # Resolve empty/alias agent_id to a qualified registry key before dispatch so
            # command Inputs (and conversation registry) never show agent_id=null.
            from ..agents import resolve_agent_id

            agent_id = ctx.get("agent_id")
            if not agent_id:
                agent_cfg = ctx.get("agent_config")
                if agent_cfg and getattr(agent_cfg, "agent_id", None):
                    bundle_id = getattr(agent_cfg, "bundle_id", None)
                    if bundle_id:
                        agent_id = f"{bundle_id}.{getattr(agent_cfg, 'agent_id', '')}"
                    else:
                        agent_id = f"core.{getattr(agent_cfg, 'agent_id', '')}"
            agent_id = resolve_agent_id(agent_id)
            if ctx.get("agent_id") != agent_id:
                ctx = dict(ctx)
                ctx["agent_id"] = agent_id

            async for event in self._stream_agent_command(
                task_id=task_id,
                conversation_id=conversation_id,
                agent_id=agent_id,
                messages=messages,
                context=ctx,
            ):
                yield event
        except Exception as e:
            logger.error(
                "distributed_orchestrator_stream_events_failed",
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            yield {"event": "error", "data": {"error": str(e)}}

    async def _stream_agent_command(
        self,
        *,
        task_id: str,
        conversation_id: str,
        agent_id: Optional[str],
        messages: List[Message],
        context: dict,
    ):
        """Run core.agent_turn and consume its task stream."""
        from motet.core.orchestration.turn import agent_turn
        from motet.core.commands.command_data_classes import AgentTurnData

        principal = getattr(self.local_stack, "_principal", None)
        principal_id = getattr(principal, "id", "") or ""
        tenant_id = getattr(principal, "tenant_id", "") or ""
        motet_id = getattr(principal, "motet_id", "") or ""

        cmd_context = {
            k: v
            for k, v in (context or {}).items()
            if k not in {"agent_config", "resolved_tools"}
        }
        run_cmd = cast(Any, agent_turn)(
            data=AgentTurnData(
                agent_id=agent_id,
                messages=messages,
                context=cmd_context,
                output_contract=cmd_context.get("output_contract"),
            ),
            task_id=task_id,
            conversation_id=conversation_id,
            principal_id=principal_id,
            tenant_id=tenant_id,
            motet_id=motet_id,
        )

        turn_task = asyncio.create_task(asyncio.to_thread(self.execute_command, run_cmd))
        async for event in self._consume_command_stream(
            task_id,
            conversation_id,
            turn_task,
            tenant_id=getattr(run_cmd.distributed_context, "tenant_id", None),
        ):
            yield event

        # Surface ADR-0029 command failures that completed without a stream error event
        # (e.g. early ConversationAccessDenied before turn hooks emit).
        if turn_task.done():
            try:
                command_result = turn_task.result()
            except Exception as e:
                from motet.core.conversations.ownership import (
                    ConversationAccessDenied,
                    is_conversation_access_denied,
                )

                if is_conversation_access_denied(e):
                    raise ConversationAccessDenied(str(e)) from e
                raise
            if isinstance(command_result, dict) and command_result.get("status") == "error":
                err = command_result.get("error") or {}
                msg = (
                    err.get("message", "Command execution failed")
                    if isinstance(err, dict)
                    else str(err)
                )
                from motet.core.conversations.ownership import (
                    ConversationAccessDenied,
                    is_conversation_access_denied,
                )

                if is_conversation_access_denied(msg):
                    raise ConversationAccessDenied(msg)
                yield {"event": "error", "error": msg}
    
    async def stream(self, stack, messages: List[Message]) -> AsyncGenerator[str, None]:
        """Stream text tokens (compatibility with regular orchestrator)."""
        async for ev in self.stream_events(stack, messages):
            if ev.get("event") == "token":
                yield ev.get("data", "")
    
    async def _initialize_observers(self, stack=None):
        """Initialize observers with event bus for distributed architecture."""
        try:
            from ..workers.event_observer_manager import register_event_observer
            # Import all observers from consolidated eventing package
            from ..workers import (
                MemoryModuleObserver, PerformanceObserver,
                WorkerObserver, CommandRoutingObserver, DistributedExecutionObserver
            )
            
            # === CORE OBSERVERS (adapted for distributed) ===
            
            # Memory observer - works with distributed memory operations
            memory_manager = getattr(stack, 'memory_manager', None) or getattr(stack, 'memory', None)
            if memory_manager:
                memory_observer = MemoryModuleObserver(memory_manager)
                register_event_observer(memory_observer)
                self.observers.append(memory_observer)
            
            # Performance observer - enhanced for distributed monitoring
            performance_observer = PerformanceObserver()
            register_event_observer(performance_observer)
            self.observers.append(performance_observer)
            
            # === DISTRIBUTED-SPECIFIC OBSERVERS ===
            
            # Worker health and performance observer
            worker_observer = WorkerObserver()
            register_event_observer(worker_observer)
            self.observers.append(worker_observer)
            
            # Command routing and load balancing observer
            routing_observer = CommandRoutingObserver()
            register_event_observer(routing_observer)
            self.observers.append(routing_observer)
            
            # Distributed execution telemetry observer
            execution_observer = DistributedExecutionObserver()
            register_event_observer(execution_observer)
            self.observers.append(execution_observer)
            logger.debug("distributed_orchestrator_observers_initialized", observer_count=len(self.observers))
            
        except Exception as e:
            logger.warning("initialize_observers_failed", error=str(e), exc_info=True)
    
__all__ = ['DistributedOrchestrator']
