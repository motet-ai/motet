# Workflow Builder Eval Scenarios

## Date Created
2026-08-07

## Purpose

Lightweight eval set for tasks that should be **cheaper / more reliable as a
registered `user.*` workflow** than as a free-form multi-turn tool loop.

Each scenario lists: user ask, expected builder outcome, and success checks.
Automated structural coverage lives in `tests/unit/core/test_workflow_builder.py`
and `tests/unit/core/test_workflow_builder_eval_scenarios.py`.

## Scenarios

### E1 — Validate → execute → register → call
1. Author a 1–2 step `core.tool_execution` workflow (e.g. math_eval).
2. `mode=validate` succeeds with `user.<owner>.<local>`.
3. `mode=execute` with required inputs succeeds (or dry-prepares in unit tests).
4. `mode=register` persists; `workflow_user.<owner>.<local>` is callable via
   `core.tool_call` / agent loop.
5. `mode=export` returns bare-id bundle YAML.

### E2 — Reject privilege escalation
1. YAML step uses `core.memory_store` or other non-allowlisted command.
2. `validate` / `register` fail with `command_not_allowed`.

### E3 — Ownership
1. Principal A registers `user.acme.brief`.
2. Principal B cannot `unregister` / `replace` / `export` (ownership_denied).
3. Principal A can unregister.

### E4 — Cross-worker durability
**Unit-covered** (fake Redis): persist → hydrate → delete, orphan-id skip,
`apply_user_workflow_sync`, builder `persist=True` — see
`tests/unit/core/test_user_workflow_catalog.py`.

**Still manual / multi-worker:**
1. Register via API or tool on worker W1.
2. Redis has `{tenant}:user_wf:user.…` and id in `{tenant}:user_wf:index`.
3. Worker W2 (or restart) hydrates and can execute `workflow_user.…`.
4. Unregister removes Redis key, registry entry, and discovery doc.

### E5 — Prefer workflow over tool loop (product eval)
Task: “search the web for X and put a short brief in a doc” when Google MCP
tools are available.
- **Baseline**: agentic loop with 4+ tool calls, re-plans each turn.
- **Target**: builder produces a 3-step DAG once; subsequent asks call
  `workflow_user.<owner>.competitor_brief` with `{topic}`.
- Score: fewer model turns after register, same factual completeness.

### E6 — Repair invalid YAML using docs_read
1. Agent is given a broken workflow YAML (e.g. `required_inputs` as a schema
   object, or `steps` as a list).
2. `core.workflow_builder` `mode=validate` fails.
3. Agent calls `core.docs_read` with `doc_id=11-workflow-system` and
   `section='YAML structure'` (not guesswork from the tool description).
4. Repaired YAML validates; optional `mode=execute` / `register`.

**Unit-covered:** `test_e6_docs_read_yaml_contract_not_guessing` in
`tests/unit/core/test_workflow_builder_eval_scenarios.py` (contract text is in
the docs page; builder description points at `core.docs_read`).
**Still model-eval:** whether the agent actually calls `docs_read` instead of
inventing YAML.
