# Best Practices

Conventions worth following, and the reason behind each. Where a rule has a real cost, that is stated too.

## Commands

Use the decorator, annotate both parameters, and make the data model a `BaseCommandData`. These travel together: the decorator reads the type hints to build the command, and the Pydantic model is what makes the data serializable across a queue and validated on arrival.

```python
# ✅ Types are load-bearing, not decoration
from motet.core.commands.base_command_data import BaseCommandData
from pydantic import Field

class MyData(BaseCommandData):
    value: str = Field(..., min_length=1, max_length=100)
    optional: int = Field(default=0, ge=0, le=100)

@motet.command()
def my_command(data: MyData, motet: MotetContext) -> Dict[str, Any]:
    return {"result": process(data.value)}

# ❌ Untyped: no validation, and the decorator cannot infer the data model
@motet.command()
def my_command(data, motet):
    return {"result": data["value"]}
```

A plain `Dict[str, Any]` is the trap, because it fails at the far end of a queue on another machine rather than at the call site.

### Write descriptions that can be discovered

Tool and command descriptions — and `Field(..., description="...")` on data models — are indexed for semantic search, so they are how users' natural-language requests find your code. Write them task-oriented. See [Tool Ecosystem — Descriptions are indexed for discovery](./21-tool-ecosystem.md#descriptions-are-indexed-for-discovery).

### Design for at-least-once delivery

Commands may be retried after timeouts, worker restarts, reconnects, or transient failures. Treat execution as **at-least-once**, never exactly-once. This is the item on this page most likely to cause a real incident, because the failure is invisible in testing and shows up as duplicate charges or duplicate emails in production.

- Your command may run more than once for the same logical request.
- Side effects (writes, external API calls, notifications) should be idempotent where practical.
- Use stable dedupe keys (`command_id`, `task_id`, or a domain idempotency key) for operations that must not apply twice.

```python
# ✅ Idempotent write with a dedupe key
@motet.command()
def create_invoice(data: CreateInvoiceData, motet: MotetContext) -> Dict[str, Any]:
    existing = db.get_invoice_by_idempotency_key(data.idempotency_key)
    if existing:
        return {"invoice_id": existing.id, "deduped": True}

    invoice = db.create_invoice(
        customer_id=data.customer_id,
        amount=data.amount,
        idempotency_key=data.idempotency_key,
    )
    return {"invoice_id": invoice.id, "deduped": False}
```

```python
# ❌ Duplicate side effect on retry
@motet.command()
def send_welcome_email(data: WelcomeEmailData, motet: MotetContext) -> Dict[str, Any]:
    email_provider.send(to=data.email, template="welcome")  # can send twice
    return {"sent": True}
```

Practical checklist: deterministic IDs or unique constraints for created records, idempotency keys checked before side effects, provider-supported idempotency keys on external calls, and returning the prior result for a duplicate rather than failing.

## Composition and parallelism

Independent work should not run in a loop. A sequential loop costs the sum of every item; `apply` dispatches concurrently, so wall time falls to roughly the slowest item per batch.

```python
# ❌ Sequential when the items are independent
for item in data.items:
    results.append(motet.do(process_item, data=ItemData(item=item)))

# ✅ Parallel, with a bound on concurrency
results = motet.apply(
    process_item,
    inputs=[{"item": item} for item in data.items],
    batch_size=10
)
```

`inputs` is a list of **dicts**, not command data models — each dict merges with `command_template` to build one command's data.

The ceiling is `batch_size` and the number of free workers, so measure rather than assuming a multiplier. `batch_size` exists for the case where unbounded fan-out would exhaust a downstream resource — a rate-limited API or a connection pool — and there the smaller batch is faster overall.

Use `motet.join()` for a handful of *different* commands in parallel, `motet.apply()` for the *same* command over many inputs, and `motet.maybe()` when a failure is acceptable:

```python
# Cache lookups should not fail the command
result, error = motet.maybe(get_cache, data=CacheData(key="expensive_op"))
if error:
    result = expensive_operation()
    motet.do(store_cache, data=StoreCacheData(key="expensive_op", value=result))
return result
```

### Reach for workflows when steps outgrow a function

Chained `motet.do()` calls are fine for two or three steps. Past that, a workflow gives you dependency ordering, per-step retries, and state that survives a worker restart — none of which you get from a Python function that dies with its worker.

```python
workflow = Workflow(
    workflow_id="complex_process",
    steps={
        "step1": WorkflowStep(step_id="step1", name="Step 1", ...),
        "step2": WorkflowStep(step_id="step2", name="Step 2", dependencies=["step1"], ...),
        "step3": WorkflowStep(step_id="step3", name="Step 3", dependencies=["step2"], ...),
    }
)
```

## Memory tiers

Tier is selected by tag. Working memory (`wm`) is for scratch data within a turn; long-term (`ltm`) is for knowledge that should outlive the conversation and be semantically searchable. Putting scratch data in `ltm` pollutes recall for every later query, which is the real cost.

```python
motet.memory.store(content="temp calculation", tags=["wm"])
motet.memory.store(content="customer prefers email contact", tags=["ltm"])
```

The tags are configurable (`memory_working_tag`, `memory_short_term_tag`), so treat `wm`/`stm`/`ltm` as the defaults rather than hard constants.

## Errors and logging

Log with context and re-raise. A bare `except` that swallows the exception is worse than no handler, because the command reports success.

```python
try:
    result = risky_operation()
    return {"result": result}
except Exception as e:
    logger.error(
        "operation_failed",
        error=str(e),
        command_id=motet.command_id,
        exc_info=True
    )
    raise
```

Include `command_id` and `task_id` in log events. In a distributed system this is what lets you reconstruct one request across several workers; without it you have lines from many interleaved requests.

Raise `CommandExecutionError` when you want the failure to carry a type across the wire:

```python
from motet_sdk import CommandExecutionError

raise CommandExecutionError(message="Operation failed", error_type="ValueError")
```

## Security

Take identity from the context, never from the payload. `motet.principal_id` and `motet.tenant_id` come from a verified JWT; the same fields in a request body are attacker-controlled.

```python
# ✅ Verified
tenant_id = motet.tenant_id

# ❌ Spoofable
tenant_id = request.json["tenant_id"]
```

Tenant scoping on memory is automatic and has no override parameter, so the thing to avoid is querying a *foreign* store — your own database — without scoping it yourself:

```python
# ❌ Your own tables are not scoped for you
return database.query(data.query)

# ✅ Either use motet.memory, or scope the query with motet.tenant_id
return database.query(data.query, tenant_id=motet.tenant_id)
```

Roles are not on `MotetContext`. Enforce them at the API boundary where the token is verified, and see [Security & Multi-Tenancy](./22-security-multi-tenancy.md) for the full picture.

## Testing

Call `__wrapped__` to test the function directly, bypassing dispatch. `MockMotetContext` from `motet_sdk.testing` supplies the context:

```python
from motet_sdk.testing import MockMotetContext

def test_my_command():
    result = my_command.__wrapped__(data=MyData(value="test"), motet=MockMotetContext())
    assert result["result"] == "success"

def test_error_handling():
    mock = MockMotetContext()
    mock.tools.execute = Mock(side_effect=Exception("Error"))
    with pytest.raises(Exception):
        my_command.__wrapped__(data=MyData(value="test"), motet=mock)
```

Test the failure paths specifically. Most command bugs are in the error branch, which never runs in a happy-path test.

Integration tests need the real stack and run in Docker, never against a local interpreter:

```bash
docker-compose -f tests/docker-compose.test.yml run --rm test-runner
```

## Code quality

Formatting and typing are enforced by tooling, so make it part of the loop rather than a review comment:

```bash
black motet/ && isort motet/ && flake8 motet/ && mypy motet/
```

Name commands for what they do to what (`analyze_customer_feedback`, not `process`), since command names appear in traces, logs, and the tool list an LLM chooses from — a vague name costs you at every one of those.

Docstrings should carry the argument contract, what comes back, what it raises, and an example:

```python
@motet.command()
def analyze_text(data: TextAnalysisData, motet: MotetContext) -> Dict[str, Any]:
    """
    Analyze text for sentiment, entities, and key phrases.

    Args:
        data: Text to analyze plus optional model selection
        motet: Motet context for resource access

    Returns:
        Dict with sentiment, entities, and phrases

    Raises:
        ValueError: When text is empty

    Example:
        result = motet.do(analyze_text, data=TextAnalysisData(text="..."))
    """
```

When you change code, check the README in its directory — the repository convention is that both land in the same commit.

## Next steps

- **[Troubleshooting Guide](./30-troubleshooting-guide.md)** — specific failures
- **[API Reference](./28-api-reference.md)** — quick reference
- **[Configuration Reference](./29-configuration-reference.md)** — config guide

## Navigation

- **[← Back to Documentation Home](./00-landing-page.md)**

---

**Last Updated**: 2026-08-21
