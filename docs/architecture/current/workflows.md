# Workflows

A workflow is a **declared DAG** of commands: steps, dependencies, skip/retry. Use it when the shape of the work is known in advance. When the next step depends on what the model just learned, use the agent loop instead.

Every step is a distributed command (`core.tool_execution`, `core.agent_loop`, `core.agent_turn`, another `workflow_execution`, …). There is no separate “tool step” vs “module step.”

```python
workflow = Workflow(
    workflow_id="lead_qualification",
    steps={
        "analyze_email": WorkflowStep(...),
        "check_crm": WorkflowStep(
            command_type="core.tool_execution",
            command_data={"tool_name": "my_crm.crm_query", ...},
            dependencies=["analyze_email"],
        ),
        "score_lead": WorkflowStep(
            command_type="core.agent_loop",
            command_data={"input": "Score this lead", ...},
            dependencies=["check_crm"],
        ),
    },
)
```

Independent steps run in parallel. Context flows between steps. Workflows can nest.

`skip_condition` is `operator:path:literal` only — no numeric comparison. Emit a boolean on the previous step and branch on that.

## Runs

A **template** is the workflow. A **run** (`workflow_run_id`) is one execution. Runs may pause, resume, or cancel. Checkpoints persist mid-graph (`PAUSED`). A child that suspends (handback / wait) can return `status=suspended` without failing the parent.

From a command: `motet.workflows.list()`, `get(id)`, `run(id, context=...)`. Those delegate to `workflow_execution`.

A workflow step that needs a loop over a prompt uses `command_type="core.agent_loop"` (no registry agent, no turn hooks). Use `core.agent_turn` on a step only for a full chat turn. `isolate_conversation` on a step gives the child its own conversation and separately attributable cost.

## Model-visible name

The LLM sees `workflow_<id>` (underscore, no dots, no wire-format transform). `core.tool_call` can invoke that name. Internally the system strips the `workflow_` prefix to get the `workflow_id`.

Users can also author workflows into a durable catalog (builder + API). Tenant-scoped catalog rows are the live store.

## Paths

- Package: `motet/core/workflow/`
- Command: `core.workflow_execution`
- Helpers: `motet.workflows`
- Onboarding: `docs/developer_onboarding/11-workflow-system.md`, `17-building-workflows.md`
