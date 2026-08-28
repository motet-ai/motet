# Distributed Orchestration Package

This package implements the **distributed orchestration system** for the AI stack with **distributed command execution**, **worker coordination**, and **intelligent routing**.

## Overview

The distributed orchestration architecture:

1. **Fully Distributed**: All operations execute as distributed commands across Celery workers
2. **Worker Coordination**: Intelligent routing based on worker capabilities and state
3. **Command Pattern**: Unified command system for all AI operations
4. **Event-Driven**: Real-time event streaming and worker-to-worker communication
5. **Fault Tolerant**: Circuit breakers, retries, and graceful degradation
6. **Principal-Based**: Consistent identity and tenant context across all commands
7. **State-Aware**: Dynamic routing based on worker state and capabilities

## Package layout

| Path | Responsibility |
|------|----------------|
| `orchestrator.py` | `DistributedOrchestrator` — entry point that turns a request into a turn |
| `turn/` | The turn lifecycle: `agent_turn` / `resume_agent_turn` commands, phase commands (memory_reset, prepare_context, finalize_turn, page_context), Turn Runtime (`runtime/` package, persist, in-process `start` / `continue_after_budget`, private resume re-entry), and helpers (`gate.py` turn gate, `prepare.py`, `hooks.py`, `complete.py`, `outcome.py`, `budget_continue.py` for issue #188 Continue — shared TurnCheckpoint rehydrate with fresh-budget policy) |
| `context/` | context preparation providers |
| `scheduling/` | Scheduled and recurring command execution |

The command **framework** and the **built-in command library** are not here —
they live in [`motet/core/commands/`](../commands/README.md). Workflow definition,
registry, and execution live in [`motet/core/workflow/`](../workflow/README.md) as a
peer of this package (not nested under turns). Orchestration is a consumer of those
frameworks like any other package. What stays here is the turn lifecycle, which is
orchestration's actual job.

## Core Components

### **Distributed Orchestrator**
The main orchestrator coordinates all AI operations through distributed commands:

```python
from motet.core.orchestration import DistributedOrchestrator

orchestrator = DistributedOrchestrator
response = await orchestrator.run(stack, messages)
```

### **Distributed Command System**
All operations are implemented as distributed commands. The commands themselves
live in [`motet/core/commands/`](../commands/README.md) — `builtin/tool.py`,
`builtin/memory.py`, `builtin/model.py`, `builtin/workflow.py`
and the rest — and orchestration composes them into turns.

### **Context Preparation Providers**
The `prepare_context` distributed command uses the provider pipeline in
`motet.core.orchestration.context` to keep context assembly modular. Providers
run in deterministic order for conversation history replay, memory recall,
artifact content parts, the disabled RAG extension point, and token budgeting.

### **Worker Coordination**
- **State-Aware Routing** - Routes commands based on worker capabilities
- **Load Balancing** - Distributes commands across available workers
- **Circuit Breakers** - Fault tolerance and graceful degradation
- **Event Streaming** - Real-time communication between workers

## Distributed Command Examples

### Agent Turn Execution
Chat and scheduled turns run `agent_turn`:

```python
from motet.core.orchestration.turn import agent_turn
from motet.core.commands.command_data_classes import AgentTurnData
from motet.core.workers import global_invoker

command = agent_turn(
    data=AgentTurnData(messages=messages),
    command_id="turn-123",
    task_id=distributed_context.task_id,
    conversation_id=distributed_context.conversation_id,
)

# Execute across distributed workers (sync invoker; use asyncio.to_thread in async code)
global_invoker.initialize
result = global_invoker.execute_command(command)
```

### Tool Execution Command
Distributed tool execution with parameter enhancement:

```python
from motet.core.commands.builtin.tool import tool_execution, ToolExecutionData
from motet.core.workers import global_invoker

# Create distributed tool command (decorator-based API)
command = tool_execution(data=ToolExecutionData(tool_name="core.http_get",
 parameters={"url": "https://api.github.com/users/octocat"},),
 command_id="tool-456",
 task_id=distributed_context.task_id,
 conversation_id=distributed_context.conversation_id or "default",)

# Execute on appropriate worker
global_invoker.initialize
result = global_invoker.execute_command(command)
```

### Memory Operations
Distributed memory management (decorator-based API):

```python
from motet.core.commands.builtin.memory import memory_store
from motet.core.commands.command_data_classes import MemoryStoreData
from motet.core.workers import global_invoker

# Store memory across distributed workers
command = memory_store(data=MemoryStoreData(content="Important conversation context",
 tags=["conversation", "important"],
 type="note",),
 command_id="memory-789",
 task_id=distributed_context.task_id,
 conversation_id=distributed_context.conversation_id,)

global_invoker.initialize
result = global_invoker.execute_command(command)
```

## Core Architecture

### DistributedOrchestrator

The main orchestrator coordinates all operations through distributed commands:

```python
from motet.core.orchestration import DistributedOrchestrator

# Initialize distributed orchestrator
orchestrator = DistributedOrchestrator

# Process messages with distributed execution
response = await orchestrator.run(stack, messages)

# Stream events for real-time interaction
async for event in orchestrator.stream_events(stack, messages):
 print(f"Event: {event}")

# Stream text tokens
async for token in orchestrator.stream(stack, messages):
 print(token, end="")
```

**Key Features:**
- **Distributed-Only**: All operations execute as distributed commands
- **Worker Coordination**: Intelligent routing and load balancing
- **Event Streaming**: Real-time event emission for UI integration
- **State Management**: Task lifecycle and worker state tracking
- **Circuit Breakers**: Built-in fault tolerance

### Distributed Command Pattern

All operations encapsulated as distributed commands with principal context (decorator-based API):

```python
from motet.core.orchestration.commands import DistributedCommandContext
from motet.core.commands.builtin.model import model_inference
from motet.core.commands.command_data_classes import ModelInferenceData
from motet.core.workers import global_invoker

# Create distributed command context
context = DistributedCommandContext(task_id="task-123",
 principal_id="principal-456",
 conversation_id="conv-789",
 tenant_id="tenant-abc")

# Create and execute distributed command
command = model_inference(data=ModelInferenceData(messages=messages,
 model_settings={"provider": "openai", "model_name": "gpt-4o", "temperature": 0.7},),
 command_id="cmd-123",
 task_id=context.task_id,
 conversation_id=context.conversation_id,)

# Execute across distributed workers (use asyncio.to_thread in async code)
global_invoker.initialize
result = global_invoker.execute_command(command)

# Commands are automatically tracked and audited
```

### Circuit Breaker Pattern

Provides resilient service calls with automatic fallback:

```python
from motet.core.orchestration import (ResilientServiceCaller, CircuitBreakerConfig, DefaultValueFallback)

# Create resilient caller
caller = ResilientServiceCaller

# Register fallback strategy
fallback = DefaultValueFallback("Service temporarily unavailable")
caller.register_fallback("model_service", fallback)

# Make resilient call
result = await caller.call_service("model_service",
 lambda: some_external_service_call,
 circuit_config=CircuitBreakerConfig(failure_threshold=3))
```

### Workflow Pattern

Workflow definition, registry, and execution live in the peer package
[`motet/core/workflow/`](../workflow/README.md) (`motet.core.workflow`). Turns may
invoke workflows as tools; workflows do not require a turn. See that README and
 for the current API (`Workflow`, `WorkflowStep`, `WorkflowRegistry`,
`WorkflowExecutor`).

### Observer Pattern

Module-specific observers for event-driven communication:

```python
from motet.core.orchestration import (global_event_bus, MemoryModuleObserver, Event, EventPriority)

# Subscribe observers
memory_observer = MemoryModuleObserver(memory_manager)
global_event_bus.subscribe(memory_observer)

# Publish events
event = Event(event_type="task_completed",
 source="orchestrator",
 data={"task_id": "task-123", "result": "success"},
 priority=EventPriority.HIGH)
await global_event_bus.publish(event)
```

### State Machine Pattern

Enhanced task lifecycle management with explicit states and transitions:

```python
from motet.core.orchestration import (TaskStateMachine, TaskContext, StateEvent, EventType)

# Create task context
context = TaskContext(task_id="task-123",
 task_type="conversation",
 input_data={"messages": messages})

# Create and use state machine
state_machine = TaskStateMachine
state_machine.context = context

# Handle events
start_event = StateEvent(EventType.START_TASK, "task-123")
await state_machine.handle_event(start_event)

# Check state
info = state_machine.get_state_info
print(f"Current state: {info['current_state']}")
```

## Configuration

### OrchestrationConfig

Current distributed orchestrator configuration:

```python
config = OrchestrationConfig(enable_observers=True,)
```

## Advanced Features

### Workflow Templates

Built-in and bundle-registered workflow templates live in
[`motet/core/workflow/`](../workflow/README.md) (`WorkflowRegistry`, `builtins.py`).
Register custom workflows with `WorkflowRegistry.register(...)` and execute via
the `workflow_execution` command or `WorkflowExecutor`.

### Custom State Handlers

Implement custom state behavior:

```python
from motet.core.orchestration import StateHandler, TaskState

class CustomPlanningHandler(StateHandler):
 async def enter_state(self, context, event):
 # Custom planning logic
 context.add_intermediate_result("custom_planning", True)

 async def handle_event(self, context, event):
 # Custom event handling
 if event.event_type == EventType.CUSTOM_EVENT:
 return TaskState.EXECUTING
 return None

# Register custom handler
state_machine.set_state_handler(TaskState.PLANNING, CustomPlanningHandler)
```

### Circuit Breaker Fallbacks

Implement custom fallback strategies:

```python
from motet.core.orchestration import FallbackStrategy

class ModelFallbackStrategy(FallbackStrategy):
 async def execute(self, service_name, original_error, *args, **kwargs):
 # Use simpler model or cached response
 return await fallback_model.complete(*args, **kwargs)

caller.register_fallback("primary_model", ModelFallbackStrategy)
```

## Monitoring and Observability

### Statistics and Metrics

Runtime state is exposed through streaming events emitted during turn execution:

```python
# Stream execution events for a turn
async for event in orchestrator.stream_events(stack, messages):
 print(event)
```

### Event Monitoring

Monitor system events:

```python
# Get recent events
recent_events = global_event_bus.get_recent_events(limit=100)

# Get observer statistics
observer_stats = global_event_bus.get_stats["observers"]
```

## Using the Orchestrator

The orchestrator provides streaming and aggregated response interfaces:

```python
orchestrator = DistributedOrchestrator(config=OrchestrationConfig)
await orchestrator.initialize(stack)

# Aggregated response (collects tokens into a single Response)
response = await orchestrator.run(stack, messages)

# Stream tokens for real-time interaction
async for token in orchestrator.stream(stack, messages):
 print(token, end="")

# Stream full event dicts (turn state, reasoning, tool calls, tokens, end)
async for event in orchestrator.stream_events(stack, messages):
 print(event)
```

Terminal events: the stream ends on `end` (turn complete), `error`, or
`suspended`.

## Best Practices

1. **Use Circuit Breakers** for all external service calls
2. **Implement Fallback Strategies** for critical services
3. **Design Workflows** for complex multi-step operations
4. **Monitor Events** for system health and performance
5. **Track Commands** for audit and debugging
6. **Handle State Transitions** explicitly for predictable behavior

## Performance Considerations

- Circuit breakers reduce cascade failures
- Command pattern adds slight overhead but provides audit trails
- Workflow engine enables parallel execution where possible
- Observer pattern allows loose coupling between modules
- State machines provide predictable error recovery

## Error Handling

The enhanced orchestrator provides multiple layers of error handling:

1. **Circuit Breakers** prevent cascade failures
2. **Command Pattern** tracks failures with retry capabilities
3. **State Machine** handles errors with explicit transitions
4. **Observers** can react to and analyze failures
5. **Workflows** can continue despite step failures (configurable)

## Future Enhancements

- Integration with Temporal.io for durable workflows
- Advanced resource-aware scheduling
- Machine learning-based failure prediction
- Auto-scaling based on load patterns
- Distributed execution across multiple nodes
 - Formal HealthCheckable protocol across subsystems