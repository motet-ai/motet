# Tool Ecosystem

Tools come from three places — 47 built-ins under `core.*`, MCP servers under `mcp.server.tool`, and anything you ship in a bundle under `{bundle_id}.*` — and look identical to the model once registered. This page covers what ships, how tools are discovered and called, and how to write your own.

## Tool names: use canonical (namespaced) names

When you call `motet.tools.execute()` or pass `tool_name` to `ToolExecutionData`, **always use the canonical tool name** — the same name the tool is registered under in the registry. The registry does not convert names; if you use the wrong format, you get "tool not found".

| Source | Canonical name format | Example |
|--------|------------------------|--------|
| **Built-in** | `core.<tool_name>` | `core.web_search`, `core.http_get`, `core.memory_recall` |
| **MCP** | `mcp.<server_id>.<tool_name>` | `mcp.playwright.browser_navigate`, `mcp.google_workspace.list_docs_in_folder` |
| **Bundle** | `{bundle_id}.<tool_name>` | `my_bundle.lookup_customer` |
| **Motet admin** | `motet_admin.<tool_name>` | `motet_admin.get_worker_summary` |

**Wire format** (e.g. `core__web_search`, `mcp__playwright__navigate` with double underscores) is used only at the LLM provider boundary so tool names satisfy `^[a-zA-Z0-9_-]+$`. When writing Motet code, use **canonical** names with dots. The `tool_execution` command normalizes wire→canonical when the name comes from the LLM; `motet.tools.execute()` does not.

## Built-in Tools

Motet includes a comprehensive set of built-in tools for common operations.

### HTTP Tools

**Purpose**: Web requests and API calls with enhanced features.

**Available Tools** (canonical names):
- `core.http_get`: GET requests for APIs and simple static content
- `core.http_post`: POST requests with JSON/form data
- `core.http_get_browser`: Browser-based GET with JavaScript execution (recommended for web pages). `main_content` keeps up to 80k characters; `content_length` is the full extract and `truncated` is set when that rail hits. The live observation may still be clipped, with the rest on the tool artifact.

A successful fetch from `core.http_get`, `core.http_get_browser`, or `core.web_search` may return `cache_control` (`same-turn` when the page or results are usable, `no-store` on errors and empty shells). If the model asks for the same tool with the same arguments while that result is still fresh, Motet replays a short cached notice instead of fetching again. Other tools default to `no-store` (a file read after an edit still runs). Bundle tools can opt in with `ok(..., cache_control="same-turn")`.

**Example**:
```python
# Basic HTTP GET (use canonical name core.http_get)
result = motet.tools.execute(
    "core.http_get",
    {"url": "https://api.example.com/data"}
)

# Browser-based GET (executes JavaScript; preferred for most websites)
result = motet.tools.execute(
    "core.http_get_browser",
    {"url": "https://spa.example.com", "wait_for": ".content-loaded"}
)
```

**Features**:
- Automatic content extraction
- Content truncation for large responses
- Browser automation support
- Error handling and retries

### File Tools

**Purpose**: File system operations with security controls on the **local device worker** (registered paths and allowlists). These tools are not routed to datacenter workers. To attach a device worker to a deployment from your machine, use **`motet-cli device`**; see [Local development setup](./14-local-development-setup.md#option-3-edge-worker-for-a-remote-motet-deployment).

**Available Tools** (canonical names):
- `core.file_read`: Read files (with allowlist)
- `core.file_write`: Write or append UTF-8 text under an allowlist
- `core.file_search`: Search for paths under an allowlisted root

**Example**:
```python
# Read file (must be in allowlist); use canonical name core.file_read
result = motet.tools.execute(
    "core.file_read",
    {"path": "/allowed/path/file.txt"}
)
```

**Security**:
- File path allowlist: `MOTET_FILE_READ_ALLOWLIST=/safe/dir1,/safe/dir2`
- Max file size: `MOTET_FILE_READ_MAX_BYTES=65536`
- Automatic path validation

### Tool Outputs and Artifacts

Tool execution can produce large or binary outputs. Motet stores these as **artifacts** (by `artifact_id`) outside the hot conversation path; conversation memory holds **ToolInvocation** records that reference artifacts for schema-correct transcript reconstruction. User file uploads are also stored as artifacts, with optional derived artifacts (extracted text, page images) for multimodal context. See [Artifacts and Multimodal Context](./20a-artifacts-and-multimodal-context.md) for the full model and API.

### Memory Tools

**Purpose**: Memory operations for context management.

**Available Tools** (canonical names):
- `core.memory_store`: Remember a fact (conversation-scoped, long-term by default)
- `core.memory_recall`: Look up memories by natural-language query (hybrid/semantic)
- `core.memory_tag`: Add/remove/set tags on existing items (requires ids or a conversation filter)
- `core.memory_forget`: Delete targeted memories from KV and the vector index (same selectors as tag)

Chat already injects hybrid recall each turn; use `core.memory_store` when the user says “remember this,” and `core.memory_recall` when automatic injection is not enough. Use `core.memory_forget` when the user asks to forget a specific fact. `core.note` does not persist. HTTP find/tag go through `MemoryManager`, not a find-by-tag tool. Operator clear is not an agent tool.

**Example**:
```python
# Store memory (use canonical name core.memory_store)
result = motet.tools.execute(
    "core.memory_store",
    {"content": "Important information", "tags": ["important", "knowledge"]}
)

# Recall by meaning
result = motet.tools.execute(
    "core.memory_recall",
    {"query": "customer feedback", "limit": 10}
)

# Tag specific memories
result = motet.tools.execute(
    "core.memory_tag",
    {"memory_ids": ["abc123"], "tags": ["customer", "priority"], "op": "add"}
)

# Forget specific memories
result = motet.tools.execute(
    "core.memory_forget",
    {"memory_ids": ["abc123"]}
)
```

### Browser Tools (Playwright)

**Purpose**: Web automation with Playwright MCP server.

**Available Tools** (via MCP; canonical names already use `mcp.<server_id>.<tool_name>`):
- `mcp.playwright.browser_navigate`: Navigate to a URL
- `mcp.playwright.browser_snapshot`: Accessibility snapshot — the usual way to
  read page content, and what you pass refs from
- `mcp.playwright.browser_take_screenshot`: Capture an image
- `mcp.playwright.browser_click`: Click an element
- `mcp.playwright.browser_type`: Type into a field
- `mcp.playwright.browser_wait_for`: Wait for text or a condition

The server exposes more than these; check what your configured version actually
registers with `motet.tools.list()` rather than assuming. Note the `browser_`
prefix on every one — the shorter names you might guess at (`navigate`,
`screenshot`) are not what the server registers.

**Example**:
```python
# Navigate (MCP tools use canonical mcp.server_id.tool_name)
result = motet.tools.execute(
    "mcp.playwright.browser_navigate",
    {"url": "https://example.com"}
)

# Take screenshot
result = motet.tools.execute(
    "mcp.playwright.browser_take_screenshot",
    {"full_page": True}
)

# Read the page as an accessibility snapshot
result = motet.tools.execute(
    "mcp.playwright.browser_snapshot",
    {}
)
```

**Note**: Browser tools require MCP Playwright server configured in `mcp_instance_manager.yaml`.

### Utility Tools

**Purpose**: Utility operations for common tasks.

**Available Tools** (canonical names):
- `core.math_eval`: Evaluate mathematical expressions
- `core.note`: Attach a comment to this turn only (does not persist memory; use `core.memory_store`)
- `core.tools_list`: List all available tools
- `core.tools_search`: Search the live catalog by task intent. Always searches tools and workflows; returns top matching tools plus up to 3 workflows (omitted when they have no signal versus the tools, or when the agent's tool filter disables workflows). Pair it with `core.tool_call` — see [How an agent finds a tool at runtime](#how-an-agent-finds-a-tool-at-runtime).
- `core.tool_call`: Invoke any tool or workflow by canonical name, whether or not it was sent with the request. Takes `tool_name` and `parameters`.
- `core.tool_describe`: Get detailed tool information
- `core.docs_read`: Read curated developer documentation (workflow YAML contract and related how-to). Omit `doc_id` to list the catalog; pass `11-workflow-system` / `17-building-workflows` and an optional heading (`YAML structure`) to read a page.

**Example**:
```python
# Math evaluation (use canonical name core.math_eval)
result = motet.tools.execute(
    "core.math_eval",
    {"expression": "2 + 2 * 3"}
)

# List all tools
result = motet.tools.execute(
    "core.tools_list",
    {"limit": 50}
)

# Search tools
result = motet.tools.execute(
    "core.tools_search",
    {"query": "http", "limit": 10}
)

# Describe tool (use canonical name for the tool being described)
result = motet.tools.execute(
    "core.tool_describe",
    {"name": "core.http_get"}
)

# Read curated developer docs (workflow YAML contract)
result = motet.tools.execute(
    "core.docs_read",
    {"doc_id": "11-workflow-system", "section": "YAML structure"}
)
```

## How Motet finds tools

This section is about how the **registry** gets populated — what exists and where
it came from. How an *agent* reaches one of those tools mid-conversation is a
different question, answered in
[How an agent finds a tool at runtime](#how-an-agent-finds-a-tool-at-runtime).

### Automatic Discovery

Tools are automatically discovered from multiple sources:

1. **Built-in tools** — registered at startup under the `core.` namespace
2. **MCP servers** — discovered from the servers configured in
   `config/mcp_instance_manager.yaml`, named `mcp.<server_id>.<tool_name>`
3. **Bundle tools** — anything decorated with `@motet.tool` in a loaded bundle,
   registered as `{bundle_id}.{name}`

```python
# List all available tools: a dict of canonical name -> registered tool
tools = motet.tools.list()

# Filter by prefix, since names are canonical
mcp_tools = {n: t for n, t in tools.items() if n.startswith("mcp.")}

# Get a single tool by canonical name
tool_info = motet.tools.get("core.http_get")
```

### MCP Tool Discovery

MCP tools are automatically discovered from configured servers:

```python
# MCP tools are discovered at startup
# Tools are named: mcp.{server_id}.{tool_name}

# The tool_name half is whatever the server registers, so read it from the
# registry rather than guessing:
# - mcp.playwright.browser_navigate
# - mcp.playwright.browser_take_screenshot
# - mcp.google_workspace.list_docs_in_folder

# List MCP tool names
mcp_tools = [n for n in motet.tools.list() if n.startswith("mcp.")]
```

**MCP Configuration**:
```yaml
# config/mcp_instance_manager.yaml
services:
  - service_id: "playwright"
    transport: "stdio"
    command: "npx"
    args: ["-y", "@playwright/mcp"]

    # Isolation + lifecycle
    state_model: "stateful"
    credential_scope: "motet"
    visibility: "motet"
    lifecycle_duration: "permanent"
    instances: 1
```

### Native Function Calling Discovery

Tools can be discovered via native LLM function calling.

Important distinction:

- `model_inference` / `model_stream` can return **tool call proposals** (`tool_calls_canonical`).
- Automatic **tool execution** happens in the agent/orchestration loop (e.g. ReAct/agent command), not in a plain one-shot `model_inference` call.

```python
# One-shot model inference can propose tool calls
from motet.core.commands.builtin.model import model_inference
from motet.core.commands.command_data_classes import ModelInferenceData
response = motet.do(model_inference, data=ModelInferenceData(
    messages=[{"role": "user", "content": "Navigate to example.com and take a screenshot"}]
))

# Inspect proposed tool calls (if any)
tool_calls = response.get("tool_calls_canonical") or []
if tool_calls:
    # In one-shot usage, you execute tool calls yourself or pass through an agent loop.
    print("Model proposed tool calls:", tool_calls)
```

Motet's part in this is narrow: it converts each tool's schema into the
provider's function format and, in an agent loop, executes whatever the model
proposes as a distributed command and feeds the result back. The choosing and
the argument-filling are the model's, which is why
[the schema is the whole story](#how-tool-parameters-get-filled).

### Semantic Tool Discovery

Motet supports **embedding-first** semantic search for tool discovery (`FunctionDiscoveryVectorStore`). The agentic loop uses the same store to pre-filter schemas before the main model call. Single-shot callers use the `tool_discovery` command, which returns ranked `ToolCandidate` payloads (no separate native-function-calling discovery service).

```python
from motet.core.commands.builtin.tool import tool_discovery, ToolDiscoveryData

# Discover tools via embedding search (worker-side ToolDiscoveryService.discover_tools)
result = motet.do(
    tool_discovery,
    data=ToolDiscoveryData(
        content="I need to search the web and take a screenshot",
        max_tools=5
    )
)
# Returns tools ranked by relevance, under their canonical names:
# - core.web_search (high relevance)
# - mcp.playwright.browser_take_screenshot (high relevance)
# - core.http_get (medium relevance)
```

**Features**:
- **Semantic Matching**: Finds tools by meaning via embeddings, not just keywords
- **Context-Aware**: Considers conversation context in the search query
- **Ranked Results**: Returns tools sorted by relevance / confidence
- **Agentic loop**: Embedding prefilter + main `model_stream` (parameter extraction on the same call)

### Descriptions are indexed for discovery

Tool discovery (and help) uses a shared semantic search index. What you write in descriptions directly affects how well your tools and commands are found when users ask in natural language.

**Tools** — The following are indexed for each tool:

- Tool name (normalized for keyword matching)
- **Description** — the `description` you pass when registering the tool
- **Keywords** — optional list you can pass at registration
- Parameter names and **parameter descriptions** from the tool schema

**Commands** — For each distributed command type, the index includes:

- Command type name (normalized)
- **Command description** — registered at command definition time (same idea as a tool’s `description`). By default this is the first line of the command **function** docstring (or the command class docstring for class-based commands). You can also pass an explicit `description=` on `@motet.command(...)`. Field/data-class docs are still indexed, but they are not the primary command summary.
- **Field names** and **field descriptions** — from your Pydantic model (`Field(..., description="...")`)

**Workflows** — Workflow name and **description** are indexed.

**Recommendation**: Write a clear, task-oriented command function docstring (or pass `description=`), and keep `Field(..., description="...")` accurate, so help and discovery can surface your commands for the right user intents. The index is refreshed when bundles are loaded or reloaded.

## How an agent finds a tool at runtime

A model can only call a tool whose schema was sent with the request. That single
constraint shapes everything below.

The obvious approach is to send every tool the agent might need. It does not
scale: a realistic agent was carrying 18–25 schemas per model call, around 6,900
tokens, on **every** call — and that still left most of the catalog unreachable,
because MCP servers and tenant bundle tools are not known until runtime and
cannot be listed in advance.

Worse, discovery without invocation is a dead end. An earlier version let the
model *look up* tool names, and the numbers were stark: `core.help` was offered
542 times and invoked **zero** times. The model could learn a tool's name and
then had nowhere to go with it — the tool was not in the request, and prompts
correctly forbid inventing names. Looking could not lead to calling.

### Disclosure and invocation are separate

The fix is to stop treating "the model knows about this tool" and "the model can
call this tool" as the same thing:

| Concern | Mechanism |
|---------|-----------|
| Learning a capability exists | `core.tools_search` — returns matches **with their full JSON schemas** |
| Invoking it without it being in the request | `core.tool_call` — dispatch by canonical name |

This works because of how provider prompt caching behaves. Caches match an exact
prefix of `tools → system → messages`. A schema parked in the tools array is a
permanent per-call tax and a cache invalidation risk; the same schema arriving in
a tool observation is paid once and leaves the cached prefix untouched. So
disclosure belongs in the message tail, and only a small invocation surface needs
to be resident.

```python
# 1. Search by what you are trying to do, not by keywords
result = motet.tools.execute("core.tools_search", {
    "query": "navigate to a website and take a screenshot",
    "limit": 5,
})
# Each hit comes back with its JSON schema and a similarity score

# 2. Call one by the canonical name the search returned
result = motet.tools.execute("core.tool_call", {
    "tool_name": "mcp.playwright.browser_navigate",
    "parameters": {"url": "https://example.com"},
})
```

Phrase the query as an intent — `"navigate to a website and take a screenshot"`
rather than `"browse website fetch URL read web page"`. Ranking is semantic when
the discovery index is available and falls back to a lexical scan when it is not,
so a keyword pile reads as a worse sentence rather than a better query.

### Workflows are callable the same way

A workflow is not a second kind of thing an agent has to reach for differently.
It is exposed under the name `workflow_<workflow_id>`, it is returned by
`core.tools_search` alongside tools, and it is a valid `core.tool_call` target:

```python
result = motet.tools.execute("core.tool_call", {
    "tool_name": "workflow_web_research",
    "parameters": {"query": "recent developments in ..."},
})
```

Behind that name the call is dispatched as a workflow execution rather than a
single tool, but the agent does not need to know or care. Workflows also appear
directly in an agent's resident tool list when its filter selects them, with
schemas exported the same way tools are — so the same multi-step process is
reachable both as a resident function and through search, without being declared
twice.

### Discovery mode

An agent configured with `tool_filter.mode: discovery` gets a deliberately small
resident set instead of a broad one:

```yaml
tool_filter:
  mode: discovery
  required_tools:
    - core.tools_search
    - core.tool_call
```

That set is the three always-sticky meta tools — `core.help`,
`core.tools_search`, `core.tool_call` — plus any keyword pins and anything the
filter marks as required. Everything else is reached through search. If you cap
the resident set with `max_tools`, leave room for those three plus the largest
pin group, or truncation will remove the very tools the agent needs to find
anything.

Two shipped example bundles use this mode; `motet-sdk/examples/bundles/cursor`
is a good one to read.

### The filter still applies

Generic dispatch is an obvious way to smuggle past a carefully built shortlist,
so it does not get to:

- **Both search and call enforce the calling agent's tool filter**, which means
  search cannot be used to enumerate tools the agent is not allowed to invoke.
- **Tools registered with `expose_to_agents=False` are denied**, in dispatch as
  well as in search. Applying it in only one place would make generic invocation
  a bypass for hidden tools.
- **`core.tool_call` refuses to call itself.**
- **Validation errors echo the schema back**, so the model can repair the call
  from the observation rather than guessing.

## How tool parameters get filled

When an agent calls a tool, the **model** fills in the LLM-visible arguments. Motet exports each tool's JSON schema — name, description, parameter types, and which are required — and the provider's native function calling does the rest. There is no separate layer in Motet that parses intent and infers arguments.

Some arguments are **not** for the model. Identity, tenant, and credentials are injected after the model chooses the tool. See [Context and credential parameters](#context-and-credential-parameters).

That makes the schema and the description the whole story for the arguments the model is allowed to supply:

- **Descriptions are prompt text.** The model reads them to decide whether a tool applies at all. "Fetches a URL" is much weaker than a sentence saying when to reach for it and what comes back.
- **Types and constraints do real work.** A parameter typed as an enum gets a valid value far more reliably than a free-form string.
- **Required versus optional is a forcing function.** Marking a field required means the model must supply it or not call the tool.

Because tools are Python functions with type hints, the schema is generated from the signature — so tightening the annotation is usually the fix when a tool keeps getting called with bad arguments. Adding a post-processing step to repair arguments is treating the symptom.

## Building Custom Tools

Motet makes it easy to build and register custom tools.

### Create a tool

Tools are **functions**, not classes. Write one in your bundle's `tools/`
directory and decorate it; the bundle loader registers it as
`{bundle_id}.{name}` when the bundle is loaded.

```python
from motet_sdk import motet
from pydantic import BaseModel, Field

class MyCustomToolParams(BaseModel):
    """Parameters for my custom tool."""
    param1: str = Field(..., description="First parameter")
    param2: int = Field(default=10, description="Second parameter (optional)")

@motet.tool(
    description="Performs custom operation with param1 and param2",
    schema=MyCustomToolParams,
)
def my_custom_tool(params: dict) -> dict:
    validated = MyCustomToolParams(**params)
    return {"result": f"Processed {validated.param1} with {validated.param2}"}
```

The decorator is covered in full under
[Custom tools via bundles](#custom-tools-via-bundles) below. Registering
directly against `ToolRegistry.register()` is a runtime-internal path that
takes a name and a callable (not a tool instance) — bundle authors should
not need it.

### Use Tool

```python
# Tool automatically available after registration (use namespaced name, e.g. bundle_id.tool_name)
result = motet.tools.execute(
    "my_bundle.my_custom_tool",
    {"param1": "value1", "param2": 42}
)

# Or via command
from motet.core.commands.builtin.tool import tool_execution, ToolExecutionData

result = motet.do(
    tool_execution,
    data=ToolExecutionData(
        tool_name="my_bundle.my_custom_tool",
        parameters={"param1": "value1", "param2": 42}
    )
)
```

### Tool Schema

Tools automatically generate schemas for LLM function calling:

```python
# Tool schema is automatically generated from:
# - name: registered tool name
# - description: the decorator's description
# - schema: the Pydantic model passed as schema=

# Example generated schema (name in registry is namespaced, e.g. my_bundle.my_custom_tool):
{
    "name": "my_bundle.my_custom_tool",
    "description": "Performs custom operation with param1 and param2",
    "parameters": {
        "type": "object",
        "properties": {
            "param1": {"type": "string", "description": "First parameter"},
            "param2": {"type": "integer", "description": "Second parameter (optional)", "default": 10}
        },
        "required": ["param1"]
    }
}
```

### Context and credential parameters

Do not put secrets or tenant identity on the schema as ordinary required LLM fields. Motet classifies those parameters, shows the model a token such as `__CTX_api_key__` instead of the value, and replaces the token at execute time. The model cannot override an injected value.

**Name conventions run automatically** — including on MCP tools — even if you never mark the field. These names are classified today:

| Names | Injected from |
|---|---|
| `user_email`, `user_google_email`, `authenticated_user_email` | Authenticated user email |
| `user_id`, `username`, `principal_id` | Principal id |
| `access_token`, `auth_token`, `api_key`, `oauth_token`, `bearer_token`, `google_access_token` | Vault / credential store |
| `task_id`, `conversation_id`, `tenant_id`, `motet_id`, `trace_id`, `command_id`, `parent_command_id` | Command context |

If you name a field `api_key` or `tenant_id` because that is what the upstream API calls it, Motet still injects it. Pick a different name when the model must supply the value.

**`ContextParam` is the explicit mark** when the name is not conventional. Import it from the runtime (it is not on the SDK surface):

```python
from pydantic import BaseModel, Field
from motet.core.tools.parameter_sources import ContextParam, ParameterSource

class SendNoticeParams(BaseModel):
    to: str = Field(..., description="Recipient email")
    subject: str = Field(..., description="Subject")
    body: str = Field(..., description="Body")
    sender: str = ContextParam(
        description="Authenticated sender email",
        source=ParameterSource.USER_CONTEXT,
        context_key="authenticated_user_email",
    )
```

`source` is `USER_CONTEXT`, `CREDENTIAL`, or `SYSTEM`. `context_key` is the lookup key (defaults to the field name). The exported schema default is `__CTX_authenticated_user_email__`; injection replaces that token before the tool runs.

## Custom tools via bundles

Custom tools are delivered by **bundles**. Add Python modules under `tools/` in your bundle; each module registers tools with the tool registry using namespaced names (`{bundle_id}.tool_name`). Deploy the bundle via `POST /api/v1/deploy`; workers load bundle tools when they run `core.reload_bundle`. See the bundle deployment docs for the full flow.

### Preferred pattern: `@motet.tool` in bundle `tools/*.py`

For bundle tool authoring, prefer the SDK decorator:

```python
from motet_sdk import motet
from pydantic import BaseModel, Field

class LookupParams(BaseModel):
    customer_id: str = Field(..., description="Customer identifier")

@motet.tool(
    description="Lookup customer profile by id",
    name="lookup_customer",          # optional; defaults to function name
    schema=LookupParams,             # recommended for parameter validation/discovery
    category="crm",
    keywords=["customer", "lookup", "profile"],
    cost_class="low",
)
def lookup_customer(params: dict) -> dict:
    return {"customer_id": params["customer_id"], "found": True}
```

Guidelines:

- Use `from motet_sdk import motet` in bundles.
- Do not include bundle prefix in `name` (use `lookup_customer`, not `my_bundle.lookup_customer`).
- Provide accurate `description` and `schema` to improve discovery and usability.
- Keep outputs deterministic where possible; use `observation_formatter` if you need concise observation text.

See [Your first bundle](./15a-your-first-bundle.md#21-using-motettool-effectively) for a compact starter recipe and pitfalls checklist.

## Tool Execution via Commands

All tool execution happens via distributed commands:

```python
from motet.core.commands.builtin.tool import tool_execution, ToolExecutionData

# Execute tool via command (use canonical tool name)
result = motet.do(
    tool_execution,
    data=ToolExecutionData(
        tool_name="core.http_get",
        parameters={"url": "https://api.example.com"}
    )
)
```

**Benefits**:
- **Distributed Execution**: Tools execute on appropriate workers
- **Automatic Routing**: Workers selected based on capabilities
- **Error Handling**: Automatic retries and error handling
- **Observability**: Full tracing and metrics

## Best Practices

### Use canonical, namespaced names

```python
# ✅ CORRECT: Use canonical names (core.* for built-ins, mcp.* for MCP)
result = motet.tools.execute(
    "core.http_get",
    {"url": "https://api.example.com"}
)
result = motet.tools.execute(
    "mcp.playwright.browser_navigate",
    {"url": "https://example.com"}
)

# ❌ WRONG: Unnamespaced or wire-format names (registry lookup will fail)
# motet.tools.execute("http_get", ...)       # use core.http_get
# motet.tools.execute("core__web_search", ...)  # wire format; use core.web_search
```

### Match the tool to the page

```python
# ✅ CORRECT: Use appropriate tool for task
result = motet.tools.execute("core.http_get", {"url": "https://api.example.com"})

# ✅ CORRECT: Use MCP/browser tools for JavaScript-heavy sites
result = motet.tools.execute("mcp.playwright.browser_navigate", {"url": "https://example.com"})

# ❌ WRONG: Don't use core.http_get for JavaScript-heavy sites
# Use core.http_get_browser or playwright tools instead
```

### Handle execution errors

```python
# ✅ CORRECT: Handle tool execution errors
from motet_sdk import CommandExecutionError

try:
    result = motet.do(
        tool_execution,
        data=ToolExecutionData(tool_name="tool_name", parameters={...})
    )
except CommandExecutionError as e:
    logger.error(
        "Tool execution failed",
        tool_name="core.web_search",
        error=str(e),
        error_type=e.error_type
    )
    # Handle error appropriately
    raise
```

### Write descriptions the model can act on

```python
# ✅ CORRECT: says when to use it and what comes back
@motet.tool(description=(
    "Look up current stock price for a ticker symbol. "
    "Returns price, currency, and timestamp. Use for equities only."
))
def stock_price(ticker: str) -> dict:
    ...

# ❌ WRONG: the model cannot tell when this applies
@motet.tool(description="Gets a price")
def stock_price(ticker: str) -> dict:
    ...
```

### Know when parameters are validated

Validation is **not** unconditional. The registry validates only when the tool
registered a Pydantic model as its schema; a tool registered with a plain JSON
Schema dict, or none at all, is called with whatever it was given:

```python
from pydantic import BaseModel, Field

class FetchParams(BaseModel):
    url: str = Field(..., description="URL to fetch")
    timeout: int = Field(default=10, ge=1, le=60)

# Registered as the tool's schema, so these bounds are enforced at call time
```

When validation does fail it **returns rather than raises** — you get
`{"status": "validation_error", "error": ...}` back from `execute`. Code that
only catches exceptions will sail past it and treat the error dict as a result,
so check `status` on the way out:

```python
result = motet.tools.execute("core.http_get", {"url": "https://api.example.com"})
if result.get("status") == "validation_error":
    logger.warning("bad tool parameters", error=result["error"])
```

### Set timeouts in the right place

```python
# ✅ CORRECT: params go in a dict; timeout is a keyword-only argument
result = motet.tools.execute(
    "core.http_get",
    {"url": "https://api.example.com"},
    timeout=30  # 30 second timeout
)

# Or via command - the timeout belongs to the command, not the data
result = motet.do(
    tool_execution,
    data=ToolExecutionData(
        tool_name="core.http_get",
        parameters={"url": "https://api.example.com"},
    ),
    timeout_seconds=30
)
```

## Common Patterns

### Pattern 1: Tool Chain

```python
@motet.command()
def tool_chain(data: ChainData, motet: MotetContext) -> Dict[str, Any]:
    """Execute tools in sequence."""
    # Step 1: Search (canonical name core.web_search)
    search_result = motet.do(
        tool_execution,
        data=ToolExecutionData(
            tool_name="core.web_search",
            parameters={"query": data.query}
        )
    )
    
    # Step 2: Navigate to first result
    navigate_result = motet.do(
        tool_execution,
        data=ToolExecutionData(
            tool_name="mcp.playwright.browser_navigate",
            parameters={"url": search_result["urls"][0]}
        )
    )
    
    # Step 3: Take screenshot
    screenshot = motet.do(
        tool_execution,
        data=ToolExecutionData(
            tool_name="mcp.playwright.browser_take_screenshot"
        )
    )
    
    return {
        "search": search_result,
        "navigation": navigate_result,
        "screenshot": screenshot
    }
```

### Pattern 2: Tool with Fallback

```python
@motet.command()
def tool_with_fallback(data: FallbackData, motet: MotetContext) -> Dict[str, Any]:
    """Try tool with fallback."""
    # Try primary tool (canonical names)
    result, error = motet.maybe(
        tool_execution,
        data=ToolExecutionData(
            tool_name="core.http_get",
            parameters={"url": data.url}
        )
    )
    
    if error:
        # Fallback to browser-based tool
        logger.warning("Primary tool failed, using fallback")
        result = motet.do(
            tool_execution,
            data=ToolExecutionData(
                tool_name="core.http_get_browser",
                parameters={"url": data.url}
            )
        )
    
    return {"result": result}
```

### Pattern 3: Parallel Tool Execution

```python
@motet.command()
def parallel_tools(data: ParallelData, motet: MotetContext) -> Dict[str, Any]:
    """Execute multiple tools in parallel."""
    results = motet.join([
        (
            tool_execution,
            ToolExecutionData(
                tool_name="core.http_get",
                parameters={"url": url}
            )
        )
        for url in data.urls
    ])
    
    return {"results": results}
```

## Troubleshooting

### Tool Not Found

**Problem**: Tool execution fails with "Tool not found"

**Solutions**:
1. **Use canonical (namespaced) names**: Built-ins are `core.<name>` (e.g. `core.web_search`), MCP tools are `mcp.<server_id>.<tool_name>`. Do not use wire format (`core__web_search`) or unnamespaced names when calling `motet.tools.execute()` or `ToolExecutionData(tool_name=...)`.
2. Check tool registration: `motet.tools.list()` (or equivalent) to see exact registered names.
3. Verify tool name: Exact name, case-sensitive; must match registry.
4. Check MCP servers: Verify MCP servers are running.
5. Review logs: `motet-cli local logs | grep tool`

### Tool Execution Timeout

**Problem**: Tool execution times out

**Solutions**:
1. Increase timeout: Set `timeout_seconds` parameter
2. Check network: Verify network connectivity
3. Check tool health: Verify tool/MCP server is healthy
4. Use faster tool: Consider alternative tools

### Tool called with wrong arguments

**Problem**: The model calls the right tool but fills parameters badly

**Solutions**:
1. Tighten the type hints — enums and constrained types beat free strings
2. Rewrite the description to say *when* to use the tool, not just what it does
3. Mark genuinely required parameters as required so the model cannot omit them
4. Check the exported schema with `core.tool_describe` to see what the model actually sees
5. If a field is named like `api_key` or `tenant_id` and the model sees `__CTX_…__`, that is injection — rename the field or mark it with `ContextParam`. See [Context and credential parameters](#context-and-credential-parameters).

## Next Steps

- **[Security & Multi-Tenancy](./22-security-multi-tenancy.md)** - Understand security
- **[Observability & Debugging](./23-observability-debugging.md)** - Learn debugging
- **[Common Patterns](./25-common-patterns.md)** - Learn patterns

## Navigation

- **[← Back to Documentation Home](./00-landing-page.md)** - Main documentation hub

---

**Last Updated**: 2026-08-27
