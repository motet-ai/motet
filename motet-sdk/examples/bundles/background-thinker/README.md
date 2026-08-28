# background-thinker

Autonomous background thinking bundle demonstrating scheduled commands, recurring reflection, memory-persisted insights, and schedule lifecycle management.

## How to use

### 1. Deploy

With the local stack running (`motet-cli local up`) and auth configured:

```bash
motet-cli bundle lint motet-sdk/examples/bundles/background-thinker
motet-cli deploy dir-deploy motet-sdk/examples/bundles/background-thinker
```

Confirm the bundle is loaded:

```bash
motet-cli deploy list
motet-cli workflows list   # should include background-thinker.background_reflection
motet-cli tools list       # should include background-thinker.recall_insights
```

### 2. Chat demo (recommended)

Open **http://localhost:5173/chat-explorer/** (or `/chat-explorer/` via the API), authenticate, then try these prompts.

**A. Reflect once and summarize (fastest smoke test)**

> Please run the background-thinker background_reflection workflow on topic "edge computing tradeoffs". Present the summary when done.

The agent typically discovers the workflow via `core.tools_search`, then executes it through `core.tool_call`. Watch the Steps panel for:

`reflect` → `check_insights`

**B. Recall stored insights**

After a successful reflection (workflow or scheduled):

> Use the background-thinker.recall_insights tool with topic "edge computing tradeoffs" and limit 5. Quote result_count and recall_path, then summarize the top insight.

**C. Start ongoing background thinking**

> Start background thinking on topic "distributed consensus" every 60 minutes using background-thinker.start_thinking_tool. Report the schedule_id and status.

**D. Stop background thinking**

> Cancel background thinking for topic "distributed consensus" using background-thinker.stop_thinking_tool. Quote the status.

Chat agents can only invoke tools, so use `stop_thinking_tool` here; the `stop_thinking` command is for API/CLI callers.

Shorter natural-language prompts also work once the bundle is deployed, for example:

> Think about durable workflow design and tell me what you've got  
> What have you been thinking about edge computing?

### 3. CLI — run the reflection workflow

```bash
motet-cli command run core.workflow_execution --timeout 300 --data '{
  "workflow_id": "background-thinker.background_reflection",
  "workflow_name": "Background Reflection",
  "context": {
    "topic": "edge computing tradeoffs",
    "provider": "openai",
    "model_name": "gpt-4o-mini"
  }
}'
```

### 4. CLI — reflect once / recall / schedule

```bash
# One reflection cycle
motet-cli command run background-thinker.reflect --timeout 120 --data '{
  "topic": "edge computing tradeoffs",
  "provider": "openai",
  "model_name": "gpt-4o-mini"
}'

# Recall insights via tool
motet-cli command run core.tool_execution --timeout 60 --data '{
  "tool_name": "background-thinker.recall_insights",
  "parameters": {"topic": "edge computing tradeoffs", "limit": 5}
}'

# Start a recurring schedule (then cancel when done testing)
motet-cli command run background-thinker.start_thinking --timeout 60 --data '{
  "topic": "distributed consensus",
  "mode": "recurring",
  "interval_seconds": 3600,
  "max_reflections": 3
}'
```

### 5. Wire / tool name (for agents)

| Kind | Internal id | Typical agent tool name |
|---|---|---|
| Workflow | `background-thinker.background_reflection` | `workflow_background-thinker__background_reflection` |
| Tool | `background-thinker.recall_insights` | `background-thinker.recall_insights` (via `core.tool_call`) |
| Tool | `background-thinker.start_thinking_tool` | `background-thinker.start_thinking_tool` (via `core.tool_call`) |
| Tool | `background-thinker.stop_thinking_tool` | `background-thinker.stop_thinking_tool` (via `core.tool_call`) |

## What it showcases

| Capability | Where demonstrated |
|---|---|
| **Scheduled commands** (`motet.schedules.create`) | `start_thinking` — creates recurring or delayed schedules |
| **Recurring schedules** (interval + cron) | `start_thinking` — both `interval_seconds` and `cron_expression` modes |
| **Delayed one-shot schedules** | `start_thinking` — `mode="delayed"` with `delay_seconds` |
| **Schedule lifecycle** (cancel/suspend/resume) | `stop_thinking` — discovers schedules by topic and manages them |
| **Memory as a knowledge loop** (`motet.memory`) | `reflect` — reads prior insights, writes new ones each cycle |
| **LLM-powered reasoning** (`motet.models.infer`) | `reflect`, `check_insights` — autonomous thinking and synthesis |
| **Schedule creation from tools** (`ctx.schedules.create`) | `start_thinking_tool` — creates schedules from `get_motet_context()` |
| **Memory recall from tools** (`ctx.memory` / principal scope) | `recall_insights` — retrieves insights across conversations |
| **Workflow orchestration** | `background_reflection.yaml` — reflect-then-summarize pipeline |
| **Built-in tool composition** (`motet.tools.execute`) | `stop_thinking`, `stop_thinking_tool` — call `core.manage_schedule` for lifecycle ops |
| **Memory-based schedule discovery** | `stop_thinking` — looks up schedule IDs stored in memory by topic |

## How it works

The bundle implements a "background subconscious" — an agent that keeps thinking about a topic between conversations, building progressively deeper understanding over time.

```
start_thinking
     │
     ▼
  [schedule created]
     │
     ▼ (every N minutes / cron tick)
  reflect ◄──── principal/tagged recall (prior insights)
     │
     ├── motet.models.infer (generate new insight)
     │
     └── motet.memory.store (principal-scoped insight)
             │
             ▼ (on demand)
      check_insights ──── motet.models.infer (synthesize all insights)
             │
             ▼
      recall_insights ──── raw memory retrieval for LLM consumption
```

Each reflection cycle reads what the thinker previously concluded, then uses the LLM to go deeper — identifying contradictions, forming hypotheses, or exploring new angles. Insights are stored **principal-scoped** so they can be recalled in later chat sessions.

## Commands

### start_thinking

Creates a recurring or delayed schedule targeting the `reflect` command.

**Scheduling patterns demonstrated:**

```python
# Recurring with interval (reflect every 30 minutes)
background-thinker.start_thinking(
    topic="quantum computing",
    interval_seconds=1800
)

# Recurring with cron (reflect at 9 AM on weekdays)
background-thinker.start_thinking(
    topic="market trends",
    cron_expression="0 9 * * MON-FRI"
)

# Delayed one-shot (reflect once in 2 hours)
background-thinker.start_thinking(
    topic="project retrospective",
    mode="delayed",
    delay_seconds=7200
)
```

Stores schedule metadata in principal-scoped memory so `stop_thinking` can find schedules by topic.

### reflect

The core scheduled command. Each time the schedule fires:

1. **Recalls** up to 5 prior insights from principal-scoped memory
2. **Generates** a new, deeper insight via `motet.models.infer()` with a reflection prompt
3. **Stores** the new insight via `motet.memory.store(..., scope_type="principal")` with iteration tracking

The reflection prompt specifically instructs the LLM to build on prior thinking rather than repeat it — identifying patterns, contradictions, gaps, and non-obvious connections.

### check_insights

Retrieves all accumulated insights for a topic and synthesizes them with an LLM into a structured summary covering key conclusions, evolving themes, open questions, and connections.

Set `summarize=False` to get raw insights without LLM synthesis.

### stop_thinking

Manages schedule lifecycle by topic or schedule ID:

- **Cancel** — permanently remove the schedule
- **Suspend** — pause (can be resumed later)
- **Resume** — reactivate a suspended schedule

When given a topic instead of a schedule ID, looks up the schedule metadata stored by `start_thinking` (principal-scoped) and calls the `core.manage_schedule` built-in tool, which enforces tenant/principal ownership.

## Tools

### recall_insights

LLM-callable tool for retrieving background insights during conversation. Prefers principal-scoped recall (same scope `reflect` writes), then tagged hybrid recall. Returns raw memory items (content, iteration, timestamps) without LLM synthesis. Use when the user asks "what have you been thinking about X?"

### start_thinking_tool

LLM-callable tool for creating a recurring thinking schedule directly from conversation. Demonstrates `get_motet_context().schedules.create()` for scheduling from a tool context. Use when the agent decides a topic deserves ongoing autonomous reflection.

### stop_thinking_tool

LLM-callable counterpart for cancelling, suspending, or resuming a schedule from conversation. Resolves the `schedule_id` from principal-scoped memory by topic, then delegates to `core.manage_schedule`. Needed because chat agents can only call tools, not commands.

## Workflow

### background_reflection

Two-step pipeline for on-demand "reflect and report":

```yaml
reflect → check_insights
```

1. Generate a new insight (building on prior thinking)
2. Synthesize all accumulated insights into a structured summary

See [How to use](#how-to-use) for chat and CLI examples. Wire name for agents:

```
workflow_background-thinker__background_reflection(topic="distributed systems")
```

## Configuration

| Parameter | Default | Description |
|---|---|---|
| `topic` | *(required)* | Topic to think about |
| `mode` | `recurring` | Schedule mode: `recurring` or `delayed` |
| `interval_seconds` | 1800 | Seconds between reflections (recurring) |
| `cron_expression` | None | Cron schedule (overrides interval if set) |
| `delay_seconds` | 3600 | Seconds before one-shot reflection (delayed) |
| `max_reflections` | None | Max reflection cycles (None = unlimited) |
| `provider` | openai | LLM provider |
| `model_name` | gpt-4o-mini | LLM model |

## What this teaches that other bundles don't

This is the only example bundle that demonstrates:

- **`motet.schedules.create()`** — creating schedules from commands and tools
- **Recurring and delayed schedule types** — cron, interval, and one-shot patterns
- **Schedule lifecycle management** — cancel, suspend, resume
- **Memory-based schedule discovery** — storing/retrieving schedule metadata by topic
- **Persistent knowledge loops** — write → schedule tick → read → think → write
- **Autonomous agent behavior** — proactive work without user prompting
