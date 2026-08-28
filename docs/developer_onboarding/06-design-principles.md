# Design Principles

Six decisions shape how Motet is built. Each one buys something and costs something, and knowing the cost is what tells you when to follow the principle and when to step around it.

## Commands are the unit of work

A command is a `@motet.command` function that runs on a worker. Model inference, tool calls, memory writes, and your own code are all reached the same way.

This is what makes horizontal scaling an operational change rather than a rewrite, because no command body knows how many workers exist. It contains failures to the command that was running, and it means you learn one authoring pattern instead of a different one for each subsystem.

What it costs is a queue hop — milliseconds to tens of milliseconds, plus wait time under load. For a tight loop of cheap operations that is the wrong trade, and the right answer is a plain function call. The principle is that work crossing a subsystem boundary should be a command, not that every helper function must be.

## Components talk through events

Components publish events rather than calling each other. The orchestrator does not know a UI exists; it emits `command_started`, and whatever is listening reacts.

This is why you can attach a live UI without touching orchestration code, and why observability is a subscriber rather than instrumentation threaded through call sites.

The cost is indirection. A stack trace does not span an event boundary, so when something does not happen, you are looking for a subscriber that never fired rather than a call that returned wrong. Correlation IDs are how you get the thread back — they are on every event for exactly this reason.

## Failures are expected

Workers die mid-command, providers rate-limit, and MCP servers hang. The runtime treats these as normal rather than exceptional.

Commands carry a timeout set on the decorator and retries set at the call site, because the right retry count depends on who is calling rather than on the command itself:

```python
# The decorator sets the per-command timeout
@motet.command(timeout_seconds=60)
def my_command(data: MyData, motet: MotetContext):
    ...

# Retries are a property of the call, not the definition.
# max_retries defaults to 3; override it at the call site:
result = motet.do(my_command, data=MyData(...), max_retries=5)
```

Circuit breakers sit in front of external services and in worker routing, so a failing dependency sheds load instead of consuming every worker in retry loops. Workers report readiness continuously, and routing skips the ones that stop answering.

The cost is that retries make non-idempotent commands dangerous. A command that charges a card or sends mail should be written to tolerate running twice, because eventually it will.

## Observable by default, traced by choice

Structured logs carry correlation IDs, so one identifier follows a request across every worker it touches. Prometheus metrics are collected and exposed without configuration.

Distributed tracing is the exception and is **off** by default (`otel_enabled`, set through `MOTET_OTEL_ENABLED`). Turn it on when you have a collector to send spans to; until then it is cost without benefit. This is worth knowing before you go looking for traces that were never emitted.

The trap here is thinking the built-ins cover domain visibility. Logging that a command completed is automatic; logging *what it decided and why* is yours to write. Use `structlog` with the correlation ID already in context.

## Security is structural

Principal and tenant identity come from a verified JWT and travel with every command, so a command five hops deep still knows who asked. It cannot be set by a caller and cannot be forged downstream, which is why `motet.tenant_id` is trustworthy in a way that a parameter would not be.

Redis keys are namespaced by tenant, so isolation is physical rather than a filter someone might forget.

Be precise about the limit. This is scoping and defense in depth on a shared fleet, not a hardened boundary between hostile tenants — several enforcement filters ship off by default, and bundle visibility is not fully isolated. [Security & Multi-Tenancy](./22-security-multi-tenancy.md) is the page to read before you rely on it for untrusted tenants.

## One authoring pattern

A decorator, a Pydantic model for input, and a `MotetContext` for everything else. That covers commands, tools, and bundles alike:

```python
@motet.command()
def my_command(data: MyData, motet: MotetContext) -> Dict[str, Any]:
    tools = motet.tools
    memory = motet.memory
    vault = motet.vault

    result = motet.do(other_command, data=OtherData(...))

    return {"result": result}
```

The value is not that this is less typing. It is that resources arrive through one object with one lifecycle, so there is no separate client to construct, configure, and pass down — and testing a command means calling a function with a mock context.

The cost is a real constraint: input must be a Pydantic model and return must be serializable. You cannot pass an open file handle or a database cursor between commands. Pass a reference and reopen it on the other side.

## Applying these

When you add something, the useful questions are whether it crosses a subsystem boundary (then it is a command), whether failing twice is safe (retries are on by default), whether the interesting decision is logged (the built-ins will not do this for you), and whether it needs identity (it is already there).

Principles are guidelines. Deviate when performance genuinely requires it, when an external system dictates the shape, or when you are exploring something new — and write down why, so the next person reads a decision rather than an accident.

## Patterns to avoid

**Doing distributed work locally.** Calling a provider SDK directly inside a command skips routing, cost tracking, and budget enforcement, and the spend becomes invisible:

```python
# ❌ WRONG: bypasses routing, cost tracking, and budgets
def process_data(data):
    result = model.infer(data)
    return result

# ✅ CORRECT: goes through the runtime
@motet.command()
def process_data(data: ProcessData, motet: MotetContext):
    result = motet.do(model_inference, data=ModelData(...))
    return result
```

**Swallowing exceptions.** A bare `except` that logs nothing turns a failure into a silent wrong answer, which is the most expensive kind of bug in a distributed system. Log with context and re-raise:

```python
# ✅ CORRECT: the failure stays visible
@motet.command()
def my_command(data: MyData, motet: MotetContext):
    try:
        return risky_operation()
    except Exception as e:
        logger.error("command_failed", error=str(e), exc_info=True)
        raise
```

**Re-deriving identity.** `motet.principal_id` and `motet.tenant_id` are already present and verified. Accepting either as a field on your input model creates a parameter a caller can lie about.

## Next steps

- **[Distributed Command System](./07-distributed-command-system.md)** — how commands actually execute
- **[Worker System & Routing](./08-worker-system-routing.md)** — how they find a worker
- **[Building Your First Command](./15-building-your-first-command.md)** — the hands-on version

## Navigation

- **[← Back to Documentation Home](./00-landing-page.md)**

---

**Last Updated**: 2026-08-21
