# deep-research

Multi-step research bundle demonstrating LLM-powered planning, parallel execution, content analysis, and memory persistence.

## How to use

### 1. Deploy

With the local stack running (`motet-cli local up`) and auth configured:

```bash
motet-cli bundle lint motet-sdk/examples/bundles/deep-research
motet-cli deploy dir-deploy motet-sdk/examples/bundles/deep-research
```

Confirm the bundle is loaded:

```bash
motet-cli deploy list
motet-cli workflows list   # should include deep-research.research
```

### 2. Chat demo (recommended)

Open **http://localhost:5173/chat-explorer/** (or `/chat-explorer/` via the API), authenticate, then send:

**Run the research workflow:**

> Please run the deep-research research workflow on topic "Python programming language history and current status" with num_queries=2, max_results_per_query=3, max_pages=2. Present the final report when done.

The agent typically discovers the workflow via `core.tools_search`, then executes it through `core.tool_call`. Watch the Steps panel for:

`plan_queries` → `gather_sources` (`core.web_search`) → `analyze_sources` (`core.http_get_browser`) → `synthesize`

**Recall a stored report** (after a successful run with `store_in_memory=true`):

> Use the deep-research.recall_research tool to recall any previously stored deep research reports about Python programming. Summarize what you find from memory.

Shorter natural-language prompts also work, for example:

> Research quantum computing breakthroughs in 2026

Use smaller `num_queries` / `max_pages` for faster smoke tests; defaults are larger (see Configuration).

### 3. CLI — run the workflow

```bash
motet-cli command run core.workflow_execution --timeout 600 --data '{
  "workflow_id": "deep-research.research",
  "workflow_name": "Deep Research",
  "context": {
    "topic": "Python programming language history and current status",
    "num_queries": 2,
    "max_results_per_query": 3,
    "max_pages": 2,
    "provider": "openai",
    "model_name": "gpt-4o-mini",
    "store_in_memory": true
  }
}'
```

### 4. CLI — run a single command

```bash
motet-cli command run deep-research.plan_queries --data '{
  "topic": "Python programming language",
  "num_queries": 2,
  "provider": "openai",
  "model_name": "gpt-4o-mini"
}'
```

Recall a stored report and check which scope answered:

```bash
motet-cli command run core.tool_execution --timeout 60 --data '{
  "tool_name": "deep-research.recall_research",
  "parameters": {"topic": "Python programming language", "limit": 3}
}'
```

`recall_path` is `principal` when the principal-scoped read found the report, `hybrid_tagged` when it fell back to tagged recall.

### 5. Wire / tool name (for agents)

| Kind | Internal id | Typical agent tool name |
|---|---|---|
| Workflow | `deep-research.research` | `workflow_deep-research__research` |
| Tool | `deep-research.recall_research` | `deep-research.recall_research` (via `core.tool_call`) |

## What it showcases

| Capability | Where demonstrated |
|---|---|
| **LLM planning** (`motet.models.infer`) | `plan_queries` — decomposes a topic into search queries |
| **Parallel fan-out** (`motet.apply`) | `gather_sources` — searches all queries concurrently |
| **Tool + LLM composition** | `extract_findings` — browser fetch + LLM extraction in one command |
| **Nested parallelism** | `analyze_sources` — applies `extract_findings` across pages in parallel |
| **Memory persistence** (`motet.memory`) | `synthesize` — stores the final report for later recall |
| **Custom tool** (`@motet.tool`) | `recall_research` — retrieve past reports from memory |
| **Workflow orchestration** | `research.yaml` — 4-step pipeline with data flow between steps |
| **Reading tool output** | `search_source` — normalizes context-processed keys and scalars |
| **Testing bundles** (`MockMotetContext`) | [`tests/unit/bundles/test_deep_research_bundle.py`](../../../../tests/unit/bundles/test_deep_research_bundle.py) |

## Pipeline

```
plan_queries ──▶ gather_sources ──▶ analyze_sources ──▶ synthesize
   (LLM)         (parallel search)   (parallel fetch     (LLM synthesis
                                      + LLM extract)      + memory store)
```

## Commands

### plan_queries
Calls `motet.models.infer()` to generate 3–10 diverse search queries from a broad topic. Returns `planning_status`: `planned`, or `fallback_topic_only` when the model's JSON can't be parsed and the bare topic is searched instead — so a thin run is distinguishable from a topic that genuinely produced one query.

### search_source
Executes a single `core.web_search` query. Used as the worker-level command fanned out by `gather_sources`.

Also the reference for **reading tool output**: results come back context-processed, which namespaces a tool's keys and label-prefixes its scalars, so `core.web_search` returns its items under `web_search.results` and its backend as `"web_search_path: ddgs"`. The `_search_items` / `_search_path` helpers show that normalization; copy them when your bundle reads any tool's output.

### gather_sources
Uses `motet.apply(search_source, ...)` to run all search queries in parallel, then deduplicates by URL while keeping each query's search order (`analyze_sources` applies the `max_pages` cap that bounds fetch cost). Forwards `provider` / `model_name` so `core.web_search` can try native LLM search; URL-bearing `ddgs` results remain the reliable fallback for long research queries.

### extract_findings
Fetches one page with `core.http_get_browser` (page text arrives as `main_content`), then uses `motet.models.infer()` to extract structured findings (facts, relevance rating, summary). A page with no extractable text is reported as low relevance instead of being sent to the LLM.

### analyze_sources
Uses `motet.apply(extract_findings, ...)` to fetch and analyze up to `max_pages` sources in parallel. Results are sorted by relevance.

### synthesize
Calls `motet.models.infer()` to synthesize all findings into a markdown research report, then stores it in **principal-scoped** memory via `motet.memory.store(..., scope_type="principal")` with topic-derived tags (`research`, `deep-research`, …). The command returns `memory_id` and `memory_store_status` so callers can verify persistence (not just trust narrative text).

## Tools

### recall_research
Custom bundle tool (`@motet.tool`) that retrieves previously completed research reports from **principal-scoped** memory (same scope synthesize writes). Prefers `recall_principal(query=..., tags=..., min_relevance=0.8)`, then falls back to tagged hybrid recall with the `deep-research` tag. Topic matching is done by the memory manager (query coverage, head-biased) — the tool only formats rows. Completes the research lifecycle — the workflow writes to memory, this tool reads from it across conversations.

## Workflow

The `research` workflow chains all four top-level steps:

```yaml
plan_queries → gather_sources → analyze_sources → synthesize
```

Registered as `deep-research.research` after deploy. See [How to use](#how-to-use) for chat and CLI examples.

## Testing

The bundle's commands are unit-tested without a running Motet stack, using the SDK's `MockMotetContext` to inject mocked LLM, tool, and memory resources:

```bash
pytest tests/unit/bundles/test_deep_research_bundle.py -q
```

```python
from unittest.mock import Mock
from motet_sdk.testing import MockMotetContext

models = Mock(infer=Mock(return_value={"content": '["query one", "query two"]'}))
result = plan_queries(PlanQueriesData(topic="rust async", num_queries=2), MockMotetContext(models=models))

assert result["queries"] == ["query one", "query two"]
assert result["planning_status"] == "planned"
```

Bundle modules aren't on the default `PYTHONPATH`, so the tests load them by file path under their canonical package names (`bundle.deep-research.commands.*`) via `tests/unit/bundles/_deep_research_test_loader.py` — the same package layout the worker's bundle loader builds, which keeps relative imports like `from .search_source import search_source` working. Copy that loader when testing your own bundle.

## Troubleshooting

| Symptom | Where to look |
|---|---|
| Report says "No verified findings were extracted" | `synthesize` returns `memory_store_status: skipped_no_findings`. Check `analyze_sources.analyzed` — pages fetched but empty means the sites blocked the browser; raise `num_queries` / `max_results_per_query` or pick a narrower topic. |
| Only one query was searched | `plan_queries` returned `planning_status: fallback_topic_only`, meaning the model didn't emit a JSON array. Retry, or use a model that follows JSON instructions. |
| `recall_research` finds nothing after a successful run | Confirm `synthesize` returned `memory_store_status: stored` with a `memory_id`. Recall is topic-filtered, so use words from the original topic. |
| Sources list is empty | `search_source` returns `web_search_path` (`ddgs` or the native provider path) and `error` on failure — run it standalone to see which backend answered. |

## Configuration

| Parameter | Default | Description |
|---|---|---|
| `topic` | *(required)* | Research topic |
| `num_queries` | 5 | Search queries to generate |
| `max_results_per_query` | 5 | Results per search query |
| `max_pages` | 8 | Pages to fetch and analyze |
| `max_chars` | 4000 | Content chars per page for LLM |
| `fetch_timeout` | 30 | Browser timeout per page (seconds) |
| `provider` | openai | LLM provider |
| `model_name` | gpt-4o-mini | LLM model |
| `store_in_memory` | true | Persist report in memory |
