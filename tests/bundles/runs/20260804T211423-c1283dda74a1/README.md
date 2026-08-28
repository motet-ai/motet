# basic-skill-example

A minimal Motet bundle whose only job is to make end-to-end skill execution
testable. Use it to verify (Path A skills) and (skill script
runners) are wired correctly on whatever environment you are setting up.

## What's in the bundle

```
basic-skill-example/
├── manifest.yaml
└── skills/
 ├── basic-script-skill/ # hermetic per-call runner
 │ ├── SKILL.md # Path A skill manifest
 │ ├── runners.yaml # lifetime: ephemeral (default)
 │ └── scripts/
 │ └── echo_payload.py
 └── warm-counter-skill/ # Slice B stateful runner
 ├── SKILL.md
 ├── runners.yaml # lifetime: stateful
 └── scripts/
 └── counter.py # module-level state survives between calls
```

When this bundle is deployed, the platform registers:

1. A **skill** named `basic-skill-example.basic-script-skill` (the SKILL.md).
2. A **runner-driven tool**
 `basic-skill-example.basic-script-skill.echo` that the agent can call
 directly. Its parameters (`text`) come straight from `runners.yaml`'s
 `args` block; under the hood it dispatches to the built-in
 `core.worker_exec` tool against the bundled script.
3. A **stateful-lifetime skill** `basic-skill-example.warm-counter-skill` and
 its runner-tool `basic-skill-example.warm-counter-skill.counter`. This
 one is wired through 's `WorkspaceContainerManager`: the first
 call lazily spins up a per-conversation container, imports
 `counter.py` once, and reuses it for every subsequent call so
 `_count` and `_history` accumulate. Different conversations get
 different containers, so state never leaks across tenants.

## Three ways to test the skill end-to-end

Pick the depth that matches how much of Motet you want to exercise.

### 1. Fastest — direct dispatch, no API, no Docker

Verifies the parser, runtime registration, and `core.worker_exec` plumbing
all work. Requires only a Python venv with Motet installed:

```bash
cd /path/to/imf
MOTET_PLUGIN_ROOT="$(pwd)/tests/bundles" \
MOTET_EXEC_BACKEND=subprocess \
MOTET_WORKER_EXEC_CWD_ALLOWLIST="$(pwd)/tests/bundles" \
.venv/bin/python -c "
from pathlib import Path
from motet.core.skills.runtime import register_runners_for_skill
from motet.core.tools import registry as tool_registry

names = register_runners_for_skill(bundle_id='basic-skill-example',
 skill_name='basic-script-skill',
 skill_dir=Path('tests/bundles/basic-skill-example/skills/basic-script-skill').resolve,)
tool = tool_registry.get(names[0])
print(tool.func({'text': 'hello, runner'}))
"
```

Expected:

```
{
 'returncode': 0,
 'stdout': '{"ok": true, "message": "hello, runner", "source": "basic-skill-example.basic-script-skill"}\n',
 ...
 'runner': 'echo',
 'runner_image_stack': 'python-minimal',
 'runner_lifetime': 'ephemeral'
}
```

If you see that, the runner-tool surface works. The skill script ran,
returned the JSON envelope, and the runtime stamped the runner metadata
back onto the observation.

### 2. Mid — deploy the bundle into a local Motet stack via the CLI

Exercises validate → publish → reload into a worker. Requires a running
local stack (Postgres, Redis, the API container) — the easiest way is
`motet-cli local up`.

```bash
motet-cli local up
motet-cli bundles deploy./tests/bundles/basic-skill-example
motet-cli bundles list # confirm the bundle is published
motet-cli tools list | grep echo # the runner-tool should appear as
 # basic-skill-example.basic-script-skill.echo
```

Then have the agent invoke it (any `motet-cli agent` call that gets the
LLM to choose the tool, e.g.):

```bash
motet-cli agent run \
 --message "Use the basic-script-skill echo runner to round-trip the text 'sticky'."
```

The agent will (a) discover the runner-tool from the registry, (b) call it
with `{"text": "sticky"}`, and (c) include the JSON output in its reply.

### 2b. Stateful lifetime — verify state survives across runner calls

The `warm-counter-skill` is the canary for Slice B. It proves
the platform actually held a per-conversation container open and reused
the supervisor's loaded module across calls — module-level globals
should accumulate within a single conversation and reset between
conversations.

Prerequisites (in addition to step 2's stack):

```bash
# Required so the worker creates workspace containers instead of silently
# downgrading to per-call subprocess execution.
export MOTET_WORKSPACE_CONTAINER_ENABLED=true

# The worker process needs to be able to talk to the local Docker daemon
# (default socket path; override with MOTET_DOCKER_SOCKET if non-default).
ls -l /var/run/docker.sock

motet-cli local up
motet-cli bundles deploy./tests/bundles/basic-skill-example
```

Then drive the agent and watch the counter climb **within** a single
conversation:

```bash
motet-cli agent run --conversation-id warm-demo \
 --message "Call the warm-counter-skill counter runner with label='one'."

motet-cli agent run --conversation-id warm-demo \
 --message "Call it again with label='two', then again with label='three'."
```

Expected: the third response includes `count=3` and
`history=["one","two","three"]`. If the count keeps resetting to `1`,
the runtime silently downgraded — re-check the env vars above and
confirm the worker container has Docker socket access.

Different `--conversation-id` values are isolated by design:

```bash
motet-cli agent run --conversation-id warm-fresh \
 --message "Call the warm-counter-skill counter runner with label='solo'."
```

…will report `count=1` no matter how many times `warm-demo` was called,
because each conversation gets its own container.

### 3. Deepest — full UI walk-through

Open the ops dashboard at `http://localhost:5173`, navigate to:

- **Bundles** → `basic-skill-example` should appear with version `0.1.0`
 and the `python-minimal` image stack.
- **Tools** → filter by `basic-skill-example` to see the runner-tool with
 its synthesized JSON Schema and `image_stack=python-minimal lifetime=ephemeral`
 metadata in the description.
- **Skills** → `basic-skill-example.basic-script-skill` listed with its
 SKILL.md description.

Then chat with the agent (Demo Chat or your own LLM client) and ask it to
"run the basic skill echo runner with text='hello'". You should see the
tool call rendered as a streamed event with the runner output.

## Adding new runners

`runners.yaml` is the single declarative surface — add another entry under
`runners:` and the platform picks it up on the next deploy. The supported
fields are pinned in `motet/core/skills/runners.py`;
the schema today:

| Field | Required | Notes |
|---|---|---|
| `name` | ✓ | Lowercase slug, becomes the third tool-name segment |
| `description` | ✓ | Shown to the LLM in tool catalogs |
| `script` | ✓ | Bundle-relative path under the skill directory |
| `interpreter` | | One of `python` / `python3` / `bash` / `sh` / `node` (default `python3`) |
| `image_stack` | | Platform image stack id (default `python-minimal`) |
| `lifetime` | | `ephemeral` / `workspace` / `stateful`. `workspace` runs each call in a per-conversation container with shared `/scratch`; `stateful` keeps a long-lived in-container Python supervisor so module-level globals persist across calls. |
| `timeout_seconds` | | 1 to 3600 |
| `network` | | `none` / `restricted` / `inherit` |
| `credentials` | | List of credential names (Slice E owns the resolver) |
| `args` | | Mapping of `name -> { type, description, default, required }` |

Each declared `args` entry becomes a flag the runtime appends to the
script invocation as `--{name}=value` (or bare `--{name}` for booleans).
Authors needing positional argv or shell features should call
`core.worker_exec` directly.

## Troubleshooting

- **Lint says my script is missing**: the script path is bundle-relative
 to the *skill directory* — `script: scripts/echo.py` resolves to
 `skills/<skill>/scripts/echo.py`. Anything outside the skill directory
 fails lint by design.
- **Stateful runner counter resets between calls**: the runtime silently
 downgraded. Common causes:
 (a) `MOTET_WORKSPACE_CONTAINER_ENABLED` is not set to `true` on the
 worker process; (b) the worker can't reach `/var/run/docker.sock`;
 (c) the request is missing `tenant_id` / `conversation_id` /
 `image_stack` because it didn't go through the agent loop. Calls
 outside an active conversation context fall back to per-call.
- **Stateful runner returns `transport_error: true`**: the manager could
 reach Docker but the bootstrap pipeline failed — typically a missing
 image (run `docker pull python:3.11-slim`) or the supervisor not
 writing its readiness marker within
 `MOTET_WORKSPACE_CONTAINER_WARM_BOOTSTRAP_TIMEOUT` seconds.
- **Tool not appearing in `motet-cli tools list`**: confirm the bundle
 re-deployed (`motet-cli bundles list --version`); runner-tools follow
 the same prune+reload semantics as `tools/*.py`-defined tools.
