# Streaming Responses

Motet streams command execution over Server-Sent Events, so a UI can show tokens, reasoning steps, tool calls, and workflow progress as they happen rather than after the fact.

Two halves to this: emitting events from a command, and consuming them from a client. They meet at a stream key.

## Server-Sent Events

SSE is plain HTTP with `Content-Type: text/event-stream`. It is one-way (server to client), text-only, and browsers reconnect automatically. Each message is a named event plus a JSON payload, terminated by a blank line:

```
event: <event_name>
data: <json_payload>

```

The blank line is what marks the end of a message — a proxy that strips or buffers it will break the stream, which is the cause of most "no events arriving" reports.

## Emitting events from a command

Commands stream through the `motet` context. Nothing needs enabling — every command can emit.

```python
from motet import motet
from motet.core.commands.decorator import MotetContext

@motet.command()
def process_data(data: ProcessData, motet: MotetContext) -> Dict[str, Any]:
    motet.stream_event("start", command_type="process_data")

    for i, item in enumerate(data.items):
        motet.stream_event(
            "progress",
            step=i + 1,
            total=len(data.items),
            message=f"Processing {item}",
        )

    motet.stream_event("end", status="success")
    return {"processed": len(data.items)}
```

`stream_event(event_type, stream_key=None, **fields)` takes arbitrary keyword fields, so custom domain events cost nothing:

```python
motet.stream_event("data_validated", records_valid=100, records_invalid=5)
motet.stream_event("export_complete", destination="s3://bucket/data.csv", row_count=1000)
```

`stream_token(token)` is shorthand for the token event:

```python
for token in llm.generate_stream(data.prompt):
    motet.stream_token(token)
```

Every event automatically carries `event`, `timestamp`, `command_id`, and `task_id`, so a consumer can correlate without you passing identifiers by hand.

### Stream keys

`motet.stream_key` resolves in priority order:

1. `data.stream_key` at runtime (highest)
2. `@motet.command(stream_key="...")` on the decorator
3. The unified task stream for the turn (default, tenant-scoped when the command has a tenant)

The default is the one that matters, because it means **child commands write to the parent's stream automatically**:

```python
@motet.command()
def parent_command(data: ParentData, motet: MotetContext) -> Dict[str, Any]:
    motet.stream_event("parent_start")

    # Both children emit onto the same stream the parent is using
    result1 = motet.do(child_command_1, data=Data1())
    result2 = motet.do(child_command_2, data=Data2())

    motet.stream_event("parent_complete")
    return {"ok": True}
```

The client sees one interleaved stream, in execution order:

```
event: parent_start
data: {...}

event: child1_progress
data: {"message": "Processing..."}

event: child2_progress
data: {"message": "Processing..."}

event: parent_complete
data: {...}
```

Override the key only when you want a command's events kept *off* the shared turn stream — a background job whose progress should not appear in a chat UI, for example.

`ensure_stream(ttl_seconds=3600)` sets the stream's TTL and is safe to call repeatedly:

```python
motet.ensure_stream(ttl_seconds=3600)
```

Errors are worth streaming as well as raising, since a raised exception reaches the caller but not the UI mid-turn:

```python
try:
    motet.stream_event("start")
    ...
    motet.stream_event("end", status="success")
except Exception as e:
    motet.stream_event("error", error=str(e), error_type=type(e).__name__)
    raise
```

## Endpoints

| Endpoint | Transport | Use |
|----------|-----------|-----|
| `POST /api/v1/chat` with `stream: true` | SSE | Chat completions — tokens, reasoning, tool events |
| `WS /api/v1/chat/ws` | WebSocket | Same content, bidirectional |
| `GET /api/v1/events` | SSE | Tenant command and turn events (event bus) |

`GET /api/v1/events` is an observability stream, not a chat stream — tokens still come from `POST /api/v1/chat`. Frames without a tenant (some `task_state_changed` and `task_completed` payloads) are not streamed.

## Per-agent attribution

When several agents participate in one turn, events carry `agent_id` (a qualified id such as `expert-panel.researcher`) so a client can route each event to the right pane. Attribution appears on token, turn, tool, reasoning, and thinking events alike. Nested loops (for example a `core.spawn_agents` child) also carry `parent_agent_id` — the agent that started that loop. The conversation-primary agent omits it. Clients that ignore either field still work — they just interleave every agent into one transcript.

## Event types

### Content

**`token`** — one token of the response. Accumulate to build the full text.

```json
{"t": "quantum", "agent_id": "core.default"}
```

A spawn child token names both ids:

```json
{"t": "Price is $12.", "agent_id": "core.default.spawn-1", "parent_agent_id": "core.default"}
```

**`thinking`** — extended thinking traces from capable models. Accumulate `text` until `is_complete` is true.

```json
{"text": "Let me analyze this step by step...", "is_complete": false, "agent_id": "core.default"}
```

This is raw model reasoning. It is *not* the same as `reasoning_step`, which is structured thought/action/observation.

**`turn`** — turn state: `PREPARING`, `THINKING`, `RESPONDING`, `COMPLETING`.

```json
{"state": "THINKING", "agent_id": "core.default"}
```

### Tool execution

**`tool_execution_started`**

```json
{"tool_name": "core.web_search", "tool_call_id": "call-abc123", "agent_id": "core.default"}
```

**`tool_execution_completed`**

```json
{"tool_name": "core.web_search", "status": "success", "preview": "Found 5 results for...", "duration_ms": 1250}
```

**`tool_execution_failed`**

```json
{"tool_name": "core.web_search", "error": "Connection timeout", "duration_ms": 30000}
```

### Reasoning

**`step`** — general execution step.

```json
{"step_name": "Analyze Query", "status": "completed", "content": "...", "observation": "..."}
```

**`reasoning_step`** — structured ReAct step.

```json
{"step": 1, "thought": "First, understand what quantum computing is", "action": "research", "observation": "...", "status": "completed"}
```

**`reasoning_meta`** — strategy and complexity for the run.

```json
{"strategy": "agentic", "complexity": "medium", "estimated_steps": 3}
```

**`reasoning`** — final reasoning result, with `success`, `final_answer`, and `reasoning_trace`.

**`conversation_analyzed`** — intent classification.

```json
{"intent": "informational", "complexity": "medium", "tone": "neutral"}
```

### Workflow

**`workflow_step`**

```json
{"step_id": "step-123", "step_name": "extract_data", "workflow_id": "my_workflow", "status": "in_progress", "duration_ms": null}
```

### Multi-agent lifecycle

**`agent_turn_start`** / **`agent_turn_complete`** — a sub-agent began or finished its turn.

```json
{"agent_id": "expert-panel.researcher", "task_id": "task-xyz"}
```

### Authentication

**`auth_required`** — an MCP tool needs OAuth. Prompt the user with `auth_url`.

```json
{"provider": "google", "auth_url": "https://accounts.google.com/o/oauth2/auth?...", "mcp_server_id": "google_workspace"}
```

Worth handling early: without it, a tool that needs authorization simply appears to hang.

### Lifecycle

**`end`** — execution complete. Carries `task_id`, plus `content` when tokens were not streamed.

```json
{"task_id": "task-xyz789", "content": "Final response text"}
```

**`error`**

```json
{"error": "Model inference failed", "agent_id": "core.default"}
```

### Event bus

**`event_bus`** — a command or turn event for the caller's tenant, via `GET /api/v1/events`.

```json
{
  "kind": "command_started",
  "source": "distributed_command_invoker",
  "data": {
    "event_type": "command_started",
    "command_id": "9162fa6c-d018-4c5a-9e2a-0b1c2d3e4f5a",
    "command_type": "core.agent_turn",
    "tenant_id": "acme",
    "conversation_id": "conv-42",
    "task_id": "task-123"
  }
}
```

## Clients

### TypeScript and React

Use `@motet/ui-common`. It ships a framework-agnostic SSE reducer that already handles every event above, plus per-agent attribution, thinking traces, and tool state — so hand-rolling a `switch` over event names is re-implementing a maintained component.

```typescript
import { parseSseBuffer, reduceChatEvent, type ChatMessage } from "@motet/ui-common";

async function streamChat(messages: Array<{role: string; content: string}>, signal?: AbortSignal) {
  const response = await fetch("/api/v1/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
    body: JSON.stringify({ messages, stream: true }),
    signal,
  });

  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let currentMessage: ChatMessage = { role: "assistant", content: "" };
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const events = parseSseBuffer(buffer);
    buffer = "";

    for (const evt of events) {
      const { message, agentKey } = reduceChatEvent(currentMessage, evt);
      currentMessage = message;
      // agentKey identifies which agent produced this event
    }
  }

  return currentMessage;
}
```

In React, drive that from an effect and abort on unmount. `useThrottle` from the same package handles the render-rate problem described below:

```tsx
import { useState, useEffect } from "react";
import { useThrottle } from "@motet/ui-common";

function Chat({ messages }: { messages: ChatMessage[] }) {
  const [current, setCurrent] = useState<ChatMessage>({ role: "assistant", content: "" });
  const throttled = useThrottle(current, 50);

  useEffect(() => {
    const controller = new AbortController();
    streamChat(messages, controller.signal).then(setCurrent);
    return () => controller.abort();   // stop streaming when unmounted
  }, [messages]);

  return <div>{throttled.content}</div>;
}
```

See [Chat Explorer & Shared UI Library](./36-chat-explorer.md) for the reducer internals, per-agent state, and the rest of the hooks.

### Python

```python
import requests
import json

def stream_chat(messages: list, api_url: str = "http://localhost:8000"):
    """Stream chat responses from Motet."""
    response = requests.post(
        f"{api_url}/api/v1/chat",
        json={"messages": messages, "stream": True},
        headers={"Accept": "text/event-stream", "Content-Type": "application/json"},
        stream=True,
    )

    event_type = "message"
    for line in response.iter_lines():
        if not line:
            continue
        line = line.decode("utf-8")

        if line.startswith("event:"):
            event_type = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data = line.split(":", 1)[1].strip()
            try:
                yield event_type, json.loads(data)
            except json.JSONDecodeError:
                yield event_type, data

for event_type, data in stream_chat([{"role": "user", "content": "Hello"}]):
    if event_type == "token":
        print(data.get("t", ""), end="", flush=True)
    elif event_type == "tool_execution_started":
        print(f"\n> Calling {data.get('tool_name')}...")
    elif event_type == "reasoning_step":
        print(f"\n[Step {data.get('step')}]: {data.get('thought')}")
    elif event_type == "auth_required":
        print(f"\nAuthorize {data.get('provider')}: {data.get('auth_url')}")
    elif event_type == "end":
        print(f"\n\nComplete. Task: {data.get('task_id')}")
    elif event_type == "error":
        print(f"\nError: {data.get('error')}")
```

### WebSocket

Same content, different framing: tokens arrive as `{"token": "..."}` and everything else as `{"event": ..., "data": ...}`.

```javascript
const ws = new WebSocket('ws://localhost:8000/api/v1/chat/ws');

ws.onopen = () => ws.send(JSON.stringify({
  messages: [{ role: 'user', content: 'Hello' }],
  stream: true,
  conversation_id: 'conv-abc',
}));

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  if (data.token !== undefined) appendToken(data.token);
  else if (data.event === 'end') onComplete(data);
  else if (data.error) onError(data.error);
};
```

### curl

```bash
# Stream chat via SSE (-N disables buffering)
curl -N -X POST http://localhost:8000/api/v1/chat \
  -H "Authorization: Bearer <token>" \
  -H "Accept: text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hello"}],"stream":true}'

# Tenant event bus
curl -N -H "Authorization: Bearer <token>" http://localhost:8000/api/v1/events

# WebSocket
echo '{"messages":[{"role":"user","content":"Hello"}],"stream":true}' \
  | websocat ws://localhost:8000/api/v1/chat/ws
```

Swap `Authorization: Bearer <token>` for `X-API-Key: <key>` if you are using API-key auth.

### A note on EventSource

The browser's built-in `EventSource` is GET-only and cannot send an `Authorization` header, so it does not work for `POST /api/v1/chat` and does not work for `GET /api/v1/events` either, since that endpoint requires a token. Use `fetch()` with a `ReadableStream`, as above.

## Building a good streaming UI

**Throttle renders, do not render per token.** A token event can arrive every few milliseconds; re-rendering on each one causes visible jank. Batch on a ~50ms timer — `useThrottle` from `@motet/ui-common` does this, and the accumulate-then-flush pattern is easy to write by hand if you are not on React.

**Always abort on unmount.** Hold an `AbortController`, pass `signal` into `fetch`, and call `controller.abort()` in the effect cleanup. Without it, the stream keeps running and writing into a component that is gone — the usual cause of a "memory leak" during streaming.

**Show what is happening, not just tokens.** The `turn` event carries state (`THINKING`, `RESPONDING`), `reasoning_step` carries the current thought, and the tool events carry the tool name. A UI that renders only tokens leaves the user staring at nothing during a 30-second tool call.

**Handle `auth_required` and `error` explicitly.** Both are terminal from the user's point of view, and both look identical to a hang if unhandled.

**Retry with backoff, but only the connection.** Wrap the stream call and retry on connection failure with increasing delay. Do not retry after `end` — that re-runs the turn.

**Bound retained history.** If you keep an event log for debugging, cap it (`events.slice(-500)`), because a long agentic turn can emit thousands of events.

## Troubleshooting

**No events at all.** Confirm `stream: true` is in the body and `Accept: text/event-stream` in the headers, then reproduce with `curl -N` — if curl streams and the browser does not, the problem is CORS or a proxy rather than Motet. Check that the response really carries `Content-Type: text/event-stream`.

**Events arrive in bursts.** Something is buffering. In nginx set `proxy_buffering off` and `proxy_cache off`, or send `X-Accel-Buffering: no`. This is by far the most common streaming complaint in production and is almost never in application code — a stream that is smooth against `localhost:8000` and bursty through a load balancer is diagnostic on its own.

**Text appears jumbled.** Something is processing token events concurrently. Tokens carry no sequence number, so ordering comes from the single-consumer assumption; parallelizing the handler or attaching two consumers to one stream will interleave them.

**Memory grows during long turns.** Usually an unbounded event array or a stream that outlived its component. Cap retained history and check that every stream path aborts on unmount.

## Next steps

- **[Chat Explorer & Shared UI Library](./36-chat-explorer.md)** — the reducer and hooks in depth
- **[Building Your First Command](./15-building-your-first-command.md)** — emit events from your own command
- **[Observability & Debugging](./23-observability-debugging.md)** — tracing a turn end to end

## Navigation

- **[← Back to Documentation Home](./00-landing-page.md)**

---

**Last Updated**: 2026-08-21
