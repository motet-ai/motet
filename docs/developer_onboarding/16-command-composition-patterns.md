# Command Composition Patterns

Command composition is the art of building complex operations from simple commands. This section covers patterns for sequential, parallel, and conditional execution, along with best practices and anti-patterns.

## Overview

**When to use what**: Use **resource helpers** (`motet.tools.execute`, `motet.memory.store`, `motet.conversations.list`, `motet.models.infer`, etc.) for a **single operation**—they delegate to the right command and are the preferred API for one-off tool runs, memory ops, agent turns, and so on. Use the **composition helpers** below when you are **chaining or combining multiple commands** (sequential, parallel, or conditional).

Motet provides these helpers for command composition:

- **motet.do()**: Sequential execution with automatic unwrapping
- **motet.join()**: Parallel execution with automatic unwrapping
- **motet.apply()**: Apply command to multiple inputs
- **motet.maybe()**: Optional error handling
- **motet.dispatch()**: Fire-and-forget execution

## Sequential Execution

### Pattern: motet.do()

Execute commands one after another, using results from previous commands.

```python
@motet.command()
def sequential_workflow(data: WorkflowData, motet: MotetContext) -> Dict[str, Any]:
    """Execute commands sequentially."""
    from motet_sdk import CommandExecutionError
    
    try:
        # Step 1: Extract data
        extracted = motet.do(extract_data, data=ExtractData(source=data.source))
        
        # Step 2: Process data (uses result from step 1)
        processed = motet.do(
            process_data,
            data=ProcessData(
                input_data=extracted,  # Unwrapped payload from step 1
                format=data.format
            )
        )
        
        # Step 3: Store results (uses result from step 2)
        stored = motet.do(
            store_data,
            data=StoreData(
                data=processed["result"],
                destination=data.destination
            )
        )
        
        return {
            "extracted": extracted,
            "processed": processed,
            "stored": stored
        }
    except CommandExecutionError as e:
        logger.error("Sequential workflow failed", error=str(e))
        raise
```

### Benefits

- **Simple**: Easy to understand and maintain
- **Error Handling**: Fail fast on errors
- **Context Preservation**: Results automatically available to next step

## Parallel Execution

### Pattern: motet.join()

Execute multiple commands in parallel and wait for all to complete.

```python
@motet.command()
def parallel_workflow(data: ParallelData, motet: MotetContext) -> Dict[str, Any]:
    """Execute commands in parallel."""
    from motet_sdk import GatherExecutionError
    
    try:
        # Execute all commands in parallel
        results = motet.join([
            (scrape_reviews, ProductData(product_id=data.product_id)),
            (scrape_pricing, ProductData(product_id=data.product_id)),
            (scrape_specs, ProductData(product_id=data.product_id)),
            (scrape_images, ProductData(product_id=data.product_id))
        ])
        
        # Results are already unwrapped
        reviews, pricing, specs, images = results
        
        # Aggregate results
        return {
            "reviews": reviews,
            "pricing": pricing,
            "specs": specs,
            "images": images,
            "aggregated": {
                "total_reviews": len(reviews.get("reviews", [])),
                "price": pricing.get("price"),
                "spec_count": len(specs.get("specs", []))
            }
        }
    except GatherExecutionError as e:
        # e.partial_results is already unwrapped: domain data or {_error: True, ...}
        logger.error(
            "Parallel execution failed",
            error=str(e),
            partial_results=e.partial_results
        )
        raise
```

### Result ordering

`motet.join()` returns results in **submission order**, not completion order:
`results[i]` always corresponds to the command you passed at position `i`, and
there is always one entry per input. That is what makes the positional unpacking
above (`reviews, pricing, specs, images = results`) safe even though the commands
finish at different times on different workers.

### Benefits

- **Performance**: Faster than sequential execution
- **Efficiency**: Better resource utilization
- **Partial Results**: Can handle partial failures

### When to Use

- Independent operations
- I/O-bound tasks
- Multiple data sources
- Performance-critical paths

## Apply to Multiple Inputs

### Pattern: motet.apply()

Apply the same command to multiple inputs in parallel.

```python
@motet.command()
def batch_processing(data: BatchData, motet: MotetContext) -> Dict[str, Any]:
    """Process multiple items in parallel."""
    from motet_sdk import ApplyExecutionError
    
    try:
        # Process 100 documents in parallel
        results = motet.apply(
            extract_text,
            inputs=[{"file": f"doc{i}.pdf"} for i in range(100)],
            command_template={"format": "markdown"},
            batch_size=10  # Optional: limit concurrent execution
        )
        
        # Results are already unwrapped
        return {
            "documents": results,
            "count": len(results),
            "success_rate": len(results) / 100
        }
    except ApplyExecutionError as e:
        logger.error(
            "Batch processing failed",
            total_inputs=e.total_inputs,
            failed=e.failed
        )
        raise
```

### Benefits

- **Scalability**: Process many items efficiently
- **Batch Control**: Limit concurrency with batch_size
- **Partial Success**: Get successful results even if some fail

## Optional Error Handling

### Pattern: motet.maybe()

Execute a command but handle errors gracefully without failing.

```python
@motet.command()
def optional_operation(data: OptionalData, motet: MotetContext) -> Dict[str, Any]:
    """Handle optional operations gracefully."""
    
    # Try to get cached data
    cached_data, error = motet.maybe(
        get_cache,
        data=CacheData(key=data.key)
    )
    
    if error:
        logger.info("Cache miss, computing fresh data", error=error.get("message"))
        # Compute fresh data
        cached_data = compute_fresh_data(data.key)
    else:
        logger.info("Cache hit, using cached data")
    
    return {"data": cached_data}
```

### Benefits

- **Graceful Degradation**: Continue even if optional step fails
- **Error Information**: Get error details without exception
- **Flexibility**: Choose how to handle failures

## Fire-and-Forget

### Pattern: motet.dispatch()

Execute commands without waiting for results.

```python
@motet.command()
def trigger_background_tasks(data: TriggerData, motet: MotetContext) -> Dict[str, Any]:
    """Trigger background tasks without waiting."""
    
    # Dispatch background tasks
    task_ids = motet.dispatch([
        (send_email, EmailData(to="user@example.com", subject="Welcome")),
        (update_analytics, AnalyticsData(event="user_registered")),
        (update_cache, CacheData(key="user_list"))
    ])
    
    # Return immediately (tasks run in background)
    return {
        "status": "triggered",
        "task_ids": task_ids
    }
```

### Benefits

- **Non-Blocking**: Don't wait for background tasks
- **Performance**: Faster response times
- **Reliability**: Tasks execute even if caller fails

## When to Use Composition vs Workflows

### Use Command Composition When:

- **Simple Operations**: 2-5 steps
- **Programmatic Control**: Need complex conditional logic
- **Dynamic Logic**: Logic determined at runtime
- **Performance**: Need fine-grained performance control

### Use Workflows When:

- **Multi-Step Processes**: 5+ steps
- **Declarative Definition**: Want declarative specification
- **Dependency Management**: Need automatic dependency resolution
- **Reusability**: Want reusable templates
- **LLM Discovery**: Want LLM to discover workflows

## Best Practices

### 1. Use Concise Helpers

```python
# Use motet.do() for sequential execution (automatic unwrapping)
result = motet.do(command, data=CommandData(...))
```

### 2. Handle Errors Properly

```python
# ✅ CORRECT: Catch specific exceptions
try:
    result = motet.do(command, data=data)
except CommandExecutionError as e:
    logger.error("Command failed", error=str(e))
    raise
```

### 3. Use Parallel Execution for Independent Operations

```python
# ✅ CORRECT: Parallel for independent operations
results = motet.join([
    (command1, data1),
    (command2, data2),
    (command3, data3)
])
```

### 4. Limit Concurrency When Needed

```python
# ✅ CORRECT: Limit concurrency for resource-intensive operations
results = motet.apply(
    heavy_command,
    inputs=large_list,
    batch_size=5  # Limit to 5 concurrent
)
```

### 5. Single Error Handling for Sequential Steps

```python
try:
    result1 = motet.do(command1, data1)
    result2 = motet.do(command2, data2)
    result3 = motet.do(command3, data3)
except CommandExecutionError as e:
    logger.error("Workflow failed", error=str(e))
    raise
```

### 6. Use Parallel Execution for Independent Operations

```python
results = motet.join([
    (command1, data1),
    (command2, data2),
    (command3, data3)
])
```

## Common Patterns

### Pattern 1: Extract-Transform-Load

```python
@motet.command()
def etl_pipeline(data: ETLData, motet: MotetContext) -> Dict[str, Any]:
    """ETL pipeline pattern."""
    # Extract
    raw_data = motet.do(extract_data, data=ExtractData(source=data.source))
    
    # Transform
    transformed = motet.do(
        transform_data,
        data=TransformData(input_data=raw_data)
    )
    
    # Load
    loaded = motet.do(
        load_data,
        data=LoadData(data=transformed["result"], destination=data.destination)
    )
    
    return {"status": "complete", "loaded": loaded}
```

### Pattern 2: Fan-Out Fan-In

```python
@motet.command()
def fan_out_fan_in(data: FanData, motet: MotetContext) -> Dict[str, Any]:
    """Fan-out fan-in pattern."""
    # Fan-out: Process multiple items in parallel
    results = motet.apply(
        process_item,
        inputs=data.items
    )
    
    # Fan-in: Aggregate results
    aggregated = motet.do(
        aggregate_results,
        data=AggregateData(results=results)
    )
    
    return {"aggregated": aggregated}
```

### Pattern 3: Retry with Fallback

```python
@motet.command()
def retry_with_fallback(data: RetryData, motet: MotetContext) -> Dict[str, Any]:
    """Retry with fallback pattern."""
    # Try primary operation
    result, error = motet.maybe(primary_operation, data=PrimaryData(...))
    
    if error:
        # Fallback to secondary operation
        logger.warning("Primary operation failed, using fallback", error=error)
        result = motet.do(fallback_operation, data=FallbackData(...))
    
    return {"result": result}
```

## Next Steps

Now that you understand command composition:

- **[Building Workflows](./17-building-workflows.md)** - Learn workflow orchestration
- **[Testing Strategies](./18-testing-strategies.md)** - Learn testing best practices
- **[Best Practices](./27-best-practices.md)** - Learn from experience

## Navigation

- **[← Back to Documentation Home](./00-landing-page.md)** - Main documentation hub

---

**Last Updated**: 2026-02-13
