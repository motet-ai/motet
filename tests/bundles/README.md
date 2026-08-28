# Bundle Test Fixtures — Bundle Deployment

This directory holds source files for test bundles used to verify the full
Motet Bundle Deployment pipeline (fetch → validate → publish → reload →
execute).

## Directory Layout

```
tests/bundles/
├── README.md ← this file
├── setup_repos.sh ← initialise local git repos from sources
├── calculator/ ← richer test bundle (command + tool + workflow)
│ ├── manifest.yaml
│ ├── commands/
│ │ └── calculate.py
│ ├── tools/
│ │ └── math_tool.py
│ └── workflows/
│ └── multi_step_calc.yaml
├── agent-configured/ ← config-only bundle (agent registry entries)
│ ├── manifest.yaml
│ └── config/
│ └── agents.yaml
└── bad-lint/ ← intentionally broken (negative lint tests)
 ├── manifest.yaml
 └── commands/
 └── bad_command.py
```

## Bundle Summaries

### `calculator`
- **Command**: `calculator.calculate` — performs add / subtract / multiply / divide.
- **Tool**: `calculator.math_tool` — wraps `calculate` for LLM use.
- **Workflow**: `calculator.multi_step_calc` — two-step workflow: add A+B, then multiply by C.
- **Purpose**: Validates command + tool + *workflow* loading, namespacing, and execution.

### `bad-lint`
- **Command**: `bad-lint.broken_command` — contains a deliberate syntax error.
- **Purpose**: Negative test — `POST /api/v1/deploy/validate` must stream `lint_error` events
 for this bundle and must **not** register anything.

### `agent-configured`
- **Agent config**: `agents/agents.yaml` defines `agent-configured.support` (alias: `helpdesk`).
- **Purpose**: Validates bundle-driven agent registry integration during reload/unload.

## SDK Examples

`hello-world` and `celebs` live in `motet-sdk/examples/bundles/` as SDK-facing
examples and remain consumable by `tests/bundles/setup_repos.sh`.

## Running Locally

### 1. Initialise local git repos (one-time, or after changing sources)

```bash
cd /path/to/imf
bash tests/bundles/setup_repos.sh
```

This creates `tests/bundles/.repos/<name>/` for each bundle source. The
`.repos/` directory is gitignored.

### 2. Validate a bundle (SSE stream)

```bash
curl -s -N -X POST http://localhost:8000/api/v1/deploy/validate \
 -H 'Content-Type: application/json' \
 -d '{"repo_url":"file:///$(pwd)/tests/bundles/.repos/hello-world","ref":"main","path":"."}'
```

### 3. Deploy a bundle

```bash
curl -s -X POST http://localhost:8000/api/v1/deploy \
 -H 'Content-Type: application/json' \
 -d '{"repo_url":"file:///$(pwd)/tests/bundles/.repos/hello-world","ref":"main","path":"."}'
```

### 4. Run the deployed command

```bash
# Via the Motet UI — start a conversation and ask the assistant:
# "Run the hello-world.hello_world command with name Alice"
#
# Or via the commands API:
curl -s -X POST http://localhost:8000/api/v1/commands \
 -H 'Content-Type: application/json' \
 -d '{"command_type":"hello-world.hello_world","data":{"name":"Alice","shout":false}}'
```

## Integration Tests

See `tests/integration/test_bundle_deployment.py` for the full automated test
suite that uses these fixtures via Docker Compose.
