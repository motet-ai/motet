# Commands / envelope

Commands are the only unit of work. A command is created with serializable data, routed to a worker that advertises the required capabilities, executed, and wrapped in one envelope.

## Write a command

Prefer `@motet.command` (namespaced decorator). `@distributed_command` is a supported alias.

```python
from motet import motet
from motet.core.commands.decorator import MotetContext
from pydantic import BaseModel, Field

class MyCommandData(BaseModel):
    input_value: str = Field(..., description="Input description")

@motet.command(
    timeout_seconds=60,
    required_capabilities=[WorkerCapability.TOOL_EXECUTION],
)
def my_command(data: MyCommandData, motet: MotetContext) -> dict:
    return {"result": motet.tools.execute("core.web_search", {"query": "Motet"})}
```

- Command functions return **domain data** or **raise**. They do not build a response envelope.
- The decorator always wraps `BaseCommandResponse`.
- Keep class-based `DistributedCommand` for infrastructure primitives (`Gather`, `Dispatch`, `Map`) and for orchestration that is still a class (`agent_turn`, workflow execution).

## Envelope

One wrap, one unwrap:

```text
command function     →  domain value T, or raise
decorator / Gather / Map / Dispatch  →  always BaseCommandResponse[T]
Redis JSON           →  model_dump of that object
do / join / apply / maybe  →  T, or CommandExecutionError
```

Unwrap is `BaseCommandResponse.model_validate`. A domain payload that happens to contain `status: "completed"` (workflows) is **data**, not a failed envelope.

`motet.add_warning()` and `motet.last_metadata` are the author surface for warnings and metadata. Do not return `create_response` / `create_error` from command bodies.

## Public composition

| Helper | Role |
|---|---|
| `motet.do(cmd, data)` | Sequential; unwrap or raise |
| `motet.join([...])` | Different commands in parallel; unwrap all |
| `motet.apply(cmd, inputs)` | Same command, many inputs; unwrap |
| `motet.maybe(cmd, data)` | Optional: `(data, error)` |
| `motet.dispatch([...])` | Fire-and-forget; command ids |

`call` / `gather` / `map` are **transport** used by those helpers. Do not call them from command bodies.

All sub-operations go through `global_invoker` (or `motet.do` / helpers, which use it). Commands compose other commands; they do not reach around the invoker.

## MotetContext

Resource access: `motet.memory`, `motet.tools`, `motet.vault`, `motet.redis`, `motet.event_bus`, `motet.artifact_store`, `motet.stack`.

Helpers (single operations that delegate to the matching command): `motet.models`, `motet.agents`, `motet.workflows`, `motet.schedules`, `motet.commands`, `motet.conversations`.

There is no `motet.llm` or `motet.agent`. Model work is `model_inference` / `model_stream`. Bundle authors import from `motet_sdk`; the runtime injects implementations in `bundle_reload.py`.

Identity on the context: `command_id`, `task_id`, `conversation_id`, `tenant_id`, `principal_id`.

## Paths

- Decorator and context: `motet/core/commands/decorator.py`, `motet/core/commands/motet_context.py`
- Envelope: `motet/core/commands/response_models.py`
- Invoker: `motet/core/workers/command_invoker.py` (`global_invoker`)
- SDK contract: `motet-sdk/src/motet_sdk/context.py`, `__init__.py`
